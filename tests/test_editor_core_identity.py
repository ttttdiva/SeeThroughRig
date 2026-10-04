"""Editor/export engine identity regressions using verified historical Git data."""
import json
import subprocess
import uuid
from pathlib import Path

import pytest
from PIL import Image

from seethrough_rig import production_editor as editor
from seethrough_rig import production_export as exporter
from seethrough_rig import production_projects as projects
from seethrough_rig.package_validate import ValidationError, validate_package
from seethrough_rig.production_io import ProductionError, atomic_json
from test_integration_package import COMMIT, export_fixture, model
from test_integration_projects import project

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def historical_assets(tmp_path_factory):
    def historical(path):
        result = subprocess.run(['git', '-C', str(ROOT), 'show', exporter.BASELINE_COMMIT + ':' + path],
                                capture_output=True, timeout=20)
        if result.returncode:
            pytest.skip('Historical assets are absent; real-data editor/export regression was not performed')
        return result.stdout

    directory = tmp_path_factory.mktemp('historical-editor')
    model_dir = directory / 'model'
    model_dir.mkdir()
    (model_dir / 'model.json').write_bytes(historical('web/models/aoi/model.json'))
    data = json.loads((model_dir / 'model.json').read_bytes())
    for atlas in data['atlases']:
        (model_dir / atlas['file']).write_bytes(historical('web/models/aoi/' + atlas['file']))
    source = directory / 'source.png'
    source.write_bytes(historical('assets/aoi/original.png'))
    # Structural export test only. This synthetic preview is not visual quality evidence.
    Image.new('RGBA', (2, 2), 'white').save(model_dir / 'rest_composite.png')
    return model_dir, source


def runtime_snapshots(model_path, settings_path, info, manifest):
    """Instantiate the actual two engines with mock GL; no browser/native rendering."""
    script = '''import fs from 'node:fs';
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const {RigRuntime} = await import(input.runtimeModule);
const model = JSON.parse(fs.readFileSync(input.modelPath));
model.images = model.atlases.map(() => ({}));
const settings = JSON.parse(fs.readFileSync(input.settingsPath));
const gl = new Proxy({}, { get(target, key) {
  if (key.startsWith('create')) return () => ({});
  if (key === 'getShaderParameter' || key === 'getProgramParameter') return () => true;
  if (key === 'getAttribLocation') return () => 0;
  if (key === 'getUniformLocation') return () => ({});
  if (key.toUpperCase() === key) return 1;
  return () => {};
}});
const snapshot = options => {
  const runtime = RigRuntime.fromData({width:100,height:100}, model, {gl,settings,...options});
  try { return {capabilities:runtime.capabilities, settings:runtime.getPlaybackSettings()}; }
  finally { runtime.dispose(); }
};
console.log(JSON.stringify({editor:snapshot({coreRef:input.coreRef}),
  exported:snapshot({manifest:input.manifest,profile:input.manifest.profile})}));'''
    data = {'runtimeModule': (ROOT / 'web/js/runtime/index.js').as_uri(), 'modelPath': str(model_path),
            'settingsPath': str(settings_path), 'coreRef': info['core_ref'], 'manifest': manifest}
    return json.loads(subprocess.check_output(['node', '--input-type=module', '-e', script],
                                             input=json.dumps(data).encode(), cwd=ROOT, timeout=60))


def test_current_commit_adoption_editor_save_export_keeps_historical_core_and_capabilities(historical_assets, tmp_path):
    model_dir, source = historical_assets
    original_source = source.read_bytes()
    current_commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip()
    assert current_commit != exporter.BASELINE_COMMIT
    storage = tmp_path / 'projects'
    ident = str(uuid.uuid4())
    state = projects.create_project(source, ident, 'owner', 'character', storage)
    directory = Path(state['project_path'])
    adopted = projects.adopt_project(ident, model_dir, current_commit, approved=True, root=storage)
    snapshot = directory / adopted['adoption']['model_dir']
    original_files = projects.tree_hashes(snapshot)
    assert adopted['adoption']['commit'] == current_commit
    info = editor.editor_info(ident, storage)
    assert info['core_ref'] == exporter.BASELINE_COMMIT

    request = directory / 'requests/settings.json'
    request.parent.mkdir()
    settings = {'playbackSettings': {'version': 1, 'blink': {'enabled': False}}}
    atomic_json(request, settings)
    editor.editor_save(ident, request, storage)
    info = editor.editor_info(ident, storage)
    assert info['core_ref'] == exporter.BASELINE_COMMIT
    result = projects.run_project(ident, storage, export_only=True)
    assert result['status'] == 'completed'
    manifest = validate_package(result['output_package'])['manifest']
    assert manifest['runtime']['core_ref'] == info['core_ref']
    assert manifest['provenance']['commit'] == current_commit
    assert manifest['provenance']['model_commit'] == exporter.BASELINE_COMMIT
    assert manifest['playbackSettings']['blink']['enabled'] is False
    for entry in manifest['files']:
        if entry['path'] in info['hashes']:
            assert entry['sha256'] == info['hashes'][entry['path']]

    actual = runtime_snapshots(directory / info['files']['model.json'], directory / info['files']['settings.json'], info, manifest)
    assert actual['editor'] == actual['exported']
    assert actual['editor']['capabilities']['playbackSettings'] == []
    assert actual['editor']['settings']['blink']['enabled'] is True
    assert actual['editor']['capabilities'] == manifest['capabilities']
    assert projects.tree_hashes(snapshot) == original_files
    assert source.read_bytes() == original_source


def test_editor_and_export_keep_other_adopted_models_canonical(project, export_fixture):
    storage, _, ident, _ = project
    projects.adopt_project(ident, export_fixture, COMMIT, approved=True, root=storage)
    assert editor.editor_info(ident, storage)['core_ref'] == 'canonical'
    result = projects.run_project(ident, storage, export_only=True)
    manifest = validate_package(result['output_package'])['manifest']
    assert manifest['runtime'].get('core_ref', 'canonical') == 'canonical'
    assert manifest['provenance']['model_commit'] is None


def test_shared_identity_rejects_unverified_historical_atlas_or_source(historical_assets):
    model_dir, _ = historical_assets
    payload, data, _ = exporter.collect_model(model_dir, source_sha256=exporter.BASELINE_SOURCE)
    assert exporter.verified_model_core_ref(ROOT, payload, data, exporter.BASELINE_SOURCE) == exporter.BASELINE_COMMIT
    with pytest.raises(ValidationError, match='Baseline source hash mismatch'):
        exporter.verified_model_core_ref(ROOT, payload, data, '0' * 64)
    tampered = {**payload, data['atlases'][0]['file']: b'different bytes'}
    with pytest.raises(ValidationError, match='Baseline atlas verification failed'):
        exporter.verified_model_core_ref(ROOT, tampered, data, exporter.BASELINE_SOURCE)


def test_baseline_provenance_cannot_pin_an_unrelated_model():
    with pytest.raises(ProductionError, match='Baseline commit requires verified historical baseline model bytes'):
        exporter.verified_model_core_ref(ROOT, {'model.json': b'{}'}, {}, '0' * 64, exporter.BASELINE_COMMIT)
    assert exporter.verified_model_core_ref(ROOT, {'model.json': b'{}'}, {}, '0' * 64) == 'canonical'
