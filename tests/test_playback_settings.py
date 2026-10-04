"""Model-bound playback defaults, strict shared schema and immutable export."""
import copy
import json
import subprocess
import shutil
from pathlib import Path

import pytest

from seethrough_rig import production_editor as editor
from seethrough_rig import production_export as exporter
from seethrough_rig import production_projects as projects
from seethrough_rig.package_validate import ValidationError, validate_model, validate_package
from seethrough_rig.playback_settings import (DEFAULTS, DEFINITION, package_playback_settings,
    resolve_playback_settings, validate_playback_settings)
from seethrough_rig.production_io import atomic_json, read_json, sha
from test_integration_package import COMMIT, SOURCE, archive, export_fixture, model, package_files
from test_integration_projects import project

ROOT = Path(__file__).resolve().parents[1]


def test_python_js_defaults_and_schema_match():
    script = "import {DEFAULT_PLAYBACK_SETTINGS,PLAYBACK_SETTINGS_DEFINITION} from './web/js/runtime/index.js'; console.log(JSON.stringify([DEFAULT_PLAYBACK_SETTINGS,PLAYBACK_SETTINGS_DEFINITION]));"
    actual = json.loads(subprocess.check_output(['node', '--input-type=module', '-e', script], cwd=ROOT))
    assert actual == [DEFAULTS, DEFINITION]
    assert resolve_playback_settings() == DEFAULTS


@pytest.mark.parametrize('value', [None, [], {}, {'version': 2}, {'version': True},
    {'version': 1, 'unknown': {}}, {'version': 1, 'blink': {'unexpected': 1}},
    {'version': 1, 'blink': {'enabled': 1}}, {'version': 1, 'wind': {'wind': True}},
    {'version': 1, 'wind': {'wind': -0.1}}, {'version': 1, 'wind': {'windBlow': 2}},
    {'version': 1, 'blink': {'intervalMode': 'sometimes'}},
    {'version': 1, 'blink': {'closeDuration': float('nan')}},
    {'version': 1, 'blink': {'intervalMin': 9, 'intervalMax': 2}},
    {'version': 1, 'gaze': {'intervalMin': 9, 'intervalMax': 2}}])
def test_invalid_persisted_playback_block(value):
    with pytest.raises(ValidationError, match='playbackSettings'):
        validate_playback_settings(value)
        resolve_playback_settings(value)


def test_partial_layer_constraints_and_precedence():
    fixed = {'version': 1, 'blink': {'intervalMode': 'fixed', 'intervalMin': 12, 'intervalMax': 2}}
    assert resolve_playback_settings(fixed)['blink']['intervalMin'] == 12
    assert resolve_playback_settings(fixed, {'version': 1, 'blink': {'intervalMin': 15, 'intervalMax': 2}})['blink']['intervalMin'] == 15
    with pytest.raises(ValidationError):
        resolve_playback_settings(fixed, {'version': 1, 'blink': {'intervalMode': 'random'}})
    defaults = package_playback_settings(
        {'playbackSettings': {'version': 1, 'wind': {'wind': .2}, 'blink': {'holdDuration': .2}}},
        {'playbackSettings': {'version': 1, 'wind': {'wind': .4}}},
        {'wind': .1}, wind=.8)
    assert defaults['wind']['wind'] == .8 and defaults['blink']['holdDuration'] == .2
    assert DEFAULTS['wind']['wind'] == 0


def test_old_packages_and_model_defaults_remain_valid(tmp_path, model):
    files, manifest = package_files(model)
    assert validate_package(archive(tmp_path / 'old.zip', files, manifest))['valid']
    model['playbackSettings'] = {'version': 1, 'breath': {'period': 5.4}}
    validate_model(model)
    files['model.json'] = json.dumps(model).encode()
    manifest['playbackSettings'] = resolve_playback_settings(model['playbackSettings'])
    assert validate_package(archive(tmp_path / 'new.zip', files, manifest))['valid']
    manifest['playbackSettings']['version'] = 2
    with pytest.raises(ValidationError, match='version'):
        validate_package(archive(tmp_path / 'unknown.zip', files, manifest))


def test_manifest_defaults_are_full_and_declared_settings_are_validated(tmp_path, model):
    files, manifest = package_files(model)
    manifest['playbackSettings'] = {'version': 1, 'wind': {'wind': .5}}
    with pytest.raises(ValidationError, match='missing group'):
        validate_package(archive(tmp_path / 'partial.zip', files, manifest))
    files['settings.json'] = json.dumps({'playbackSettings': {'version': 9}}).encode()
    manifest['settings'] = 'settings.json'
    manifest['playbackSettings'] = resolve_playback_settings()
    with pytest.raises(ValidationError, match='version'):
        validate_package(archive(tmp_path / 'bad-settings.zip', files, manifest))


def test_export_keeps_authored_bytes_and_explicit_wind_precedence(export_fixture, tmp_path):
    tuning = {'version': 1, 'wind': {'wind': .2}, 'blink': {'intervalMode': 'fixed', 'intervalMin': 8}}
    raw = (json.dumps({'playbackSettings': tuning, 'extraSafeTuning': [1, 2]}) + '\n').encode()
    (export_fixture / 'settings.json').write_bytes(raw)
    result = exporter.export_package(export_fixture, tmp_path / 'conversation.zip', 'p', 'r', SOURCE, COMMIT,
                                    profile='conversation-v1', wind=.7)
    assert result['manifest']['playbackSettings']['wind']['wind'] == .7
    assert result['manifest']['playbackSettings']['blink']['intervalMin'] == 8
    assert (export_fixture / 'settings.json').read_bytes() == raw
    comparison = exporter.export_package(export_fixture, tmp_path / 'comparison.zip', 'p', 'r2', SOURCE, COMMIT)
    assert comparison['manifest']['profile']['id'] == 'comparison-v1'
    assert comparison['manifest']['profile']['wind'] == 0


def test_project_defaults_save_export_reload_and_new_revision(project, export_fixture):
    storage, _, ident, directory = project
    adopted = projects.adopt_project(ident, export_fixture, COMMIT, approved=True, root=storage)
    snapshot = directory / adopted['adoption']['model_dir']
    before = projects.tree_hashes(snapshot)
    first = projects.run_project(ident, storage, export_only=True)
    manifest = validate_package(first['output_package'])['manifest']
    assert manifest['profile']['id'] == 'conversation-v1'
    assert manifest['playbackSettings'] == DEFAULTS
    request = directory / 'requests/tuning.json'
    request.parent.mkdir()
    atomic_json(request, {'playbackSettings': {'version': 1, 'blink': {'holdDuration': .33}}})
    editor.editor_save(ident, request, storage)
    info = editor.editor_info(ident, storage)
    saved = json.loads((directory / info['files']['settings.json']).read_text())
    assert saved['playbackSettings']['blink']['holdDuration'] == .33
    second = projects.run_project(ident, storage, export_only=True)
    assert first['revision'] != second['revision']
    assert validate_package(second['output_package'])['manifest']['playbackSettings']['blink']['holdDuration'] == .33
    assert projects.run_project(ident, storage, export_only=True)['revision'] == second['revision']
    assert projects.tree_hashes(snapshot) == before
    original = (directory / 'material/settings.json').read_bytes()
    atomic_json(request, {'playbackSettings': {'version': 1, 'blink': {'holdDuration': -1}}})
    with pytest.raises(ValidationError):
        editor.editor_save(ident, request, storage)
    assert (directory / 'material/settings.json').read_bytes() == original


def test_settings_only_edits_reuse_legacy_build_cache_but_artwork_changes_do_not():
    inputs = {'recipe': {}, 'material': {'art.png': 'art-hash'}, 'decomposition': {}, 'builder': 'builder-hash'}
    legacy = {**inputs, 'material': {**inputs['material'], 'settings.json': 'previous-settings-hash'}}
    record = {'key': sha(json.dumps(legacy, sort_keys=True).encode()), 'files': {'settings.json': 'previous-settings-hash'}}
    assert projects.build_cache_matches(inputs, record)
    changed = copy.deepcopy(inputs)
    changed['material']['art.png'] = 'new-art-hash'
    assert not projects.build_cache_matches(changed, record)
    new_record = {'key': sha(json.dumps(inputs, sort_keys=True).encode()), 'files': {}}
    assert projects.build_cache_matches(inputs, new_record)


def test_generated_project_playback_edit_exports_without_rebuild(project, export_fixture, monkeypatch):
    storage, _, ident, directory = project
    env = {'ready': False, 'build_ready': False, 'inference': {}, 'problems': ['Inference deliberately unavailable']}
    monkeypatch.setattr(projects, 'doctor', lambda _: env)
    monkeypatch.setattr(projects, 'deficits', lambda *args: [])
    monkeypatch.setattr(projects, 'run_child', lambda *args, **kwargs: pytest.fail('Playback edits must not rebuild or infer'))
    monkeypatch.setattr(projects, 'git_output', lambda *args: (COMMIT + '\n').encode())
    recipe = {'restore': {'enabled': True}, 'rig': {'head': {'mode': 'rigid'}, 'body': {'rigid': True}},
              'layers': {'face': {}}, 'order': ['face'], 'expressions': {'box': [0, 0, 2, 2], 'images': {key: key + '.png' for key in projects.ART}}}
    monkeypatch.setattr(projects, 'scoped_recipe', lambda *args: recipe)
    work = directory / 'work/decomposition'; work.mkdir()
    atomic_json(work / 'decompose.json', {})
    build = directory / 'work/build'; shutil.copytree(export_fixture, build)
    old_settings = {'playbackSettings': {'version': 1, 'blink': {'holdDuration': .12}}}
    atomic_json(build / 'settings.json', old_settings)
    atomic_json(directory / 'material/settings.json', old_settings)
    state = read_json(directory / 'project.json')
    cache = {'directory': 'work/decomposition', 'files': projects.tree_hashes(work), 'key': projects.workflow_key(state, env)}
    legacy_inputs = {'recipe': recipe, 'material': projects.tree_hashes(directory / 'material'),
                     'decomposition': cache['files'], 'builder': sha((ROOT / 'seethrough_rig/build.py').read_bytes())}
    build_cache = {'directory': 'work/build', 'files': projects.tree_hashes(build),
                   'key': sha(json.dumps(legacy_inputs, sort_keys=True).encode())}
    projects.save(directory, state, decomposition_cache=cache, build_cache=build_cache)
    before = projects.tree_hashes(build)
    request = directory / 'requests/playback.json'; request.parent.mkdir()
    atomic_json(request, {'playbackSettings': {'version': 1, 'blink': {'holdDuration': .4}}})
    saved = editor.editor_save(ident, request, storage)
    assert saved['settings_sha256'] == sha((directory / 'material/settings.json').read_bytes())
    result = projects.run_project(ident, storage, export_only=True)
    assert result['status'] == 'completed'
    assert validate_package(result['output_package'])['manifest']['playbackSettings']['blink']['holdDuration'] == .4
    assert projects.tree_hashes(build) == before
