"""Project isolation, Japanese/space paths, cancellation, crash/retry and cache tests."""
import io
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
import pytest
from PIL import Image
from seethrough_rig import integration
from seethrough_rig import production_projects as projects
from seethrough_rig import production_environment as environment
from seethrough_rig import production_export as exporter
from seethrough_rig.production_io import (Cancelled, ProductionError, atomic_json, child_env, lock, read_json, run_child, sha)
from test_integration_package import COMMIT, export_fixture, image_bytes, model

@pytest.fixture
def project(tmp_path):
    storage = tmp_path / '日本語 projects with spaces'
    source = tmp_path / '元イラスト one.png'
    source.write_bytes(image_bytes())
    ident = str(uuid.uuid4())
    state = projects.create_project(source, ident, 'owner-1', 'character-1', storage)
    return storage, source, ident, Path(state['project_path'])

def test_create_durable_identity_and_no_cross_character_reuse(project):
    storage, source, ident, p = project
    assert projects.status(ident, storage)['source_sha256'] == sha(source.read_bytes())
    assert all((p / d).is_dir() for d in ('input', 'recipe', 'material', 'history', 'work', 'logs', 'preview'))
    assert 'warp_check' in (p / 'agent-instructions.md').read_text(encoding='utf-8')
    assert projects.create_project(source, ident, 'owner-1', 'character-1', storage)['reused']
    with pytest.raises(ProductionError, match='different'):
        projects.create_project(source, ident, 'owner-2', 'character-2', storage)
    second = str(uuid.uuid4())
    created = projects.create_project(source, second, 'owner-1', 'character-2', storage)
    assert created['project_path'] != str(p)
    assert not list((Path(created['project_path']) / 'history').iterdir())

def test_original_source_preserved_and_internal_source_change_blocks_resume(project):
    storage, source, ident, p = project
    raw = source.read_bytes()
    projects.cancel(ident, storage)
    (p / 'input/source-original').write_bytes(b'changed')
    with pytest.raises(ProductionError, match='source changed'):
        projects.run_project(ident, storage, resume=True)
    assert source.read_bytes() == raw
    assert (p / 'cancel.request').exists()  # Invalid source cannot clear a cancellation.

def test_no_environment_returns_real_needs_input(project, monkeypatch):
    storage, source, ident, p = project
    monkeypatch.setattr(projects, 'doctor', lambda repo: {'ready': False, 'build_ready': True,
                        'inference': {'root': None, 'python': None, 'models': None}, 'problems': ['Local weights missing']})
    before = source.read_bytes()
    result = projects.run_project(ident, storage)
    assert result['status'] == 'needs_input' and result['stage'] == 'environment'
    assert result['output_package'] is None and not result['inference_performed']
    roles = {d['role'] for d in result['deficits']}
    assert {'recipe', 'eye_closed', 'eye_half', 'mouth_half', 'happy', 'brow_clear'} <= roles
    assert source.read_bytes() == before

def test_cancel_resume_and_crash_reconciliation(project, monkeypatch):
    storage, _, ident, p = project
    monkeypatch.setattr(projects, 'doctor', lambda repo: {'ready': False, 'build_ready': True,
                        'inference': {}, 'problems': ['environment missing']})
    assert projects.cancel(ident, storage)['cancel_requested']
    assert projects.run_project(ident, storage)['status'] == 'cancelled'
    assert projects.run_project(ident, storage, resume=True)['status'] == 'needs_input'
    assert not (p / 'cancel.request').exists()
    state = read_json(p / 'project.json')
    state['status'] = 'running'
    atomic_json(p / 'project.json', state)
    assert projects.status(ident, storage)['status'] == 'interrupted'
    assert projects.run_project(ident, storage, resume=True)['status'] == 'needs_input'

def test_project_lock_and_global_gpu_lock_release_after_failure(project):
    storage, _, ident, p = project
    with lock(p / 'operation.lock'):
        with pytest.raises(ProductionError) as info:
            projects.run_project(ident, storage)
        assert info.value.code == 'busy'
    with lock(p / 'operation.lock'):
        pass
    with lock(storage / '.gpu.lock'):
        with pytest.raises(ProductionError, match='running'):
            with lock(storage / '.gpu.lock'):
                pass
    with lock(storage / '.gpu.lock'):
        pass

def test_decomposition_hash_cache_and_crop_deficits(project, monkeypatch):
    storage, _, ident, p = project
    calls = []
    monkeypatch.setattr(projects, 'doctor', lambda repo: {'ready': True, 'build_ready': True,
                        'inference': {}, 'problems': []})
    def fake_stage(command, cwd, log, project, env=None):
        calls.append(command)
        work = Path(command[command.index('--work') + 1])
        work.mkdir(parents=True)
        (work / 'source.png').write_bytes(image_bytes())
        passes = {}
        for name in ('full', 'full1024', 'head'):
            d = work / 'passes' / name
            d.mkdir(parents=True)
            atomic_json(d / 'info.json', {'frame_size': [2, 2], 'parts': {}})
            passes[name] = {'dir': str(d), 'kind': 'head' if name == 'head' else 'full',
                            'box': [0, 0, 2, 2], 'resolution': 1280, 'seed': 42}
        atomic_json(work / 'decompose.json', {'passes': passes, 'source_size': [2, 2]})
    monkeypatch.setattr(projects, 'run_child', fake_stage)
    first = projects.run_project(ident, storage)
    assert first['status'] == 'needs_input' and first['stage'] == 'artwork'
    assert first['inference_performed'] and len(calls) == 1
    assert next(d for d in first['deficits'] if d['role'] == 'eye_closed')['crop'] == [0, 0, 2, 2]
    assert projects.run_project(ident, storage, resume=True)['status'] == 'needs_input'
    assert len(calls) == 1
    work = p / first['decomposition_cache']['directory']
    (work / 'source.png').write_bytes(b'tampered')
    projects.run_project(ident, storage, resume=True)
    assert len(calls) == 2  # Content mismatch cannot falsely reuse cached work.

def test_approved_existing_model_adoption_and_immutable_retry(project, export_fixture, monkeypatch):
    storage, _, ident, p = project
    with pytest.raises(ProductionError) as e:
        projects.adopt_project(ident, export_fixture, COMMIT, root=storage)
    assert e.value.code == 'approval_required'
    adopted = projects.adopt_project(ident, export_fixture, COMMIT, approved=True, root=storage)
    assert adopted['adoption']['new_inference'] is False
    assert adopted['inference_performed'] is False
    monkeypatch.setattr(projects, 'run_child', lambda *a, **k: pytest.fail('Adoption must not run inference/build'))
    result = projects.run_project(ident, storage)
    assert result['status'] == 'completed' and result['artistic_quality_approved'] is False
    package = Path(result['output_package'])
    before = package.read_bytes()
    again = projects.run_project(ident, storage, resume=True, export_only=True)
    assert again['revision'] == result['revision'] and package.read_bytes() == before
    assert len(list((p / 'history').glob('*.zip'))) == 1
    snap = p / adopted['adoption']['model_dir']
    (snap / 'model.json').write_bytes(b'{}')
    with pytest.raises(ProductionError, match='snapshot changed'):
        projects.run_project(ident, storage, resume=True)
    assert package.read_bytes() == before
    assert projects.status(ident, storage)['output_package'] is None

def test_recipe_scope_rejects_other_project_artwork(project):
    storage, _, ident, p = project
    outside = storage / 'other.png'
    outside.write_bytes(image_bytes())
    atomic_json(p / 'recipe/rig.json', {'layers': {'face': {'image': '../../other.png'}}})
    with pytest.raises(ProductionError, match='escapes'):
        projects.scoped_recipe(p, p / 'work')
    atomic_json(p / 'recipe/rig.json', {'extends': 'rig.json'})
    with pytest.raises(Exception, match='Cyclic'):
        projects.scoped_recipe(p, p / 'work')

@pytest.mark.parametrize('ident', ['../other', 'C:/temp', 'not-a-uuid', '00000000000000000000000000000000'])
def test_invalid_project_ids(ident, tmp_path):
    with pytest.raises(ProductionError):
        projects.project_path(ident, tmp_path)

def test_subprocess_environment_does_not_expose_host_secrets(monkeypatch):
    monkeypatch.setenv('AOITALK_API_KEY', 'must-not-propagate')
    monkeypatch.setenv('OPENAI_API_KEY', 'must-not-propagate')
    monkeypatch.setenv('HF_TOKEN', 'must-not-propagate')
    monkeypatch.setenv('PYTHONPATH', 'untrusted')
    env = child_env()
    assert not {'AOITALK_API_KEY', 'OPENAI_API_KEY', 'HF_TOKEN', 'PYTHONPATH'} & env.keys()
    assert env['HF_HUB_OFFLINE'] == '1' and env['PIP_NO_INDEX'] == '1'

def test_actual_child_cancel_and_retry_japanese_space_path(project):
    storage, _, ident, p = project
    signal = p / 'cancel.request'
    timer = threading.Timer(0.4, lambda: signal.write_text('cancel', encoding='utf-8'))
    timer.start()
    try:
        with pytest.raises(Cancelled):
            run_child([sys.executable, '-c', 'import time; time.sleep(20)'], p, p / 'logs/取消 log.txt', p)
    finally:
        timer.join()
    signal.unlink()
    run_child([sys.executable, '-c', 'print("retry complete")'], p, p / 'logs/再開 log.txt', p)
    assert 'retry complete' in (p / 'logs/再開 log.txt').read_text(encoding='utf-8')
    assert (p / 'input/source-original').exists()

def test_json_envelope_and_argument_errors(capsys, monkeypatch):
    monkeypatch.setattr(environment, 'doctor', lambda: {'status': 'needs_input', 'installed': True, 'ready': False})
    assert integration.main(['doctor', '--json']) == 0
    data = json.loads(capsys.readouterr().out)
    assert data['identity'] == 'seethrough-rig' and data['protocol_version'] == 1 and data['ok']
    assert not data['result']['ready']
    assert integration.main(['project-create', '--json']) == 2
    data = json.loads(capsys.readouterr().out)
    assert data['error']['code'] == 'invalid_arguments' and not data['ok']

def test_logs_redirect_to_stderr(capsys, monkeypatch):
    def logged():
        print('human stage log')
        return {'status': 'ready'}
    monkeypatch.setattr(environment, 'doctor', logged)
    integration.main(['--json', 'doctor'])
    out = capsys.readouterr()
    assert json.loads(out.out)['ok'] and out.err.strip() == 'human stage log'

def test_doctor_explicit_invalid_path_does_not_fallback(tmp_path, monkeypatch):
    repo = tmp_path / 'tool'
    repo.mkdir()
    monkeypatch.setenv('SEETHROUGH_ROOT', str(repo / 'explicit nonexistent'))
    monkeypatch.setattr(environment, 'KNOWN', {k: repo for k in ('root', 'python', 'models')})
    result = environment.doctor(repo)
    assert result['inference']['root'] == str((repo / 'explicit nonexistent').resolve())
    assert not result['ready'] and not result['installed']

def test_saved_config_precedence_and_no_inference_import(tmp_path, monkeypatch):
    repo = tmp_path / 'tool'
    (repo / '.local').mkdir(parents=True)
    saved = repo / 'saved inference'
    atomic_json(repo / '.local/production-environment.json', {'root': str(saved)})
    monkeypatch.delenv('SEETHROUGH_ROOT', raising=False)
    assert environment.resolve_environment(repo)['root'] == saved.resolve()
    before = set(sys.modules)
    result = environment.doctor(repo)
    assert not result['ready'] and not result['downloads_performed']
    assert not {'torch', 'diffusers', 'transformers'} & (set(sys.modules) - before)

def test_explicit_prepare_uses_fixed_lightweight_index_and_existing_venv(tmp_path, monkeypatch):
    missing = {'dependencies': {k: False for k in environment.DEPENDENCIES}, 'installed': True, 'ready': False}
    ready = {'dependencies': {k: True for k in environment.DEPENDENCIES}, 'installed': True, 'ready': False}
    calls = []
    monkeypatch.setattr(environment, 'doctor', lambda repo: ready if calls else missing)
    monkeypatch.setattr(sys, 'prefix', str(tmp_path / '.venv'))
    monkeypatch.setattr(sys, 'base_prefix', str(tmp_path / 'base'))
    monkeypatch.setattr(environment.importlib.util, 'find_spec', lambda name: object())
    monkeypatch.setattr(environment, 'run_child', lambda *args, **kwargs: calls.append((args, kwargs)))
    result = environment.prepare_environment(tmp_path)
    assert result['prepared'] and result['weights_downloaded'] is False
    assert result['ready'] is False  # Lightweight preparation does not prove inference readiness.
    command = calls[0][0][0]
    assert command[0] == sys.executable and command[1:4] == ['-m', 'pip', '--isolated']
    assert command[command.index('--index-url') + 1] == 'https://pypi.org/simple'
    assert '--only-binary=:all:' in command
    assert set(command[-4:]) == set(environment.DEPENDENCIES.values())
    assert str(tmp_path / '.local/production-pip-cache') in command
    assert not any('torch' in arg or 'diffusers' in arg or 'transformers' in arg for arg in command)


def test_unpinned_code_install_and_update_do_not_run_subprocess(tmp_path, monkeypatch):
    monkeypatch.setattr(environment, 'doctor', lambda repo: {'dependencies': {}, 'installed': False, 'ready': False})
    monkeypatch.setattr(environment, 'run_child', lambda *a, **k: pytest.fail('Unpinned code operation must not execute'))
    for operation in ('install', 'update'):
        result = environment.prepare_environment(tmp_path, operation)
        assert result['status'] == 'needs_input' and result['changed'] is False
        assert result['fixed_source'] == 'https://github.com/ttttdiva/SeeThroughRig.git'


def test_editor_save_produces_new_revision_without_changing_adopted_assets(project, export_fixture):
    from seethrough_rig import production_editor as editor
    storage, _, ident, p = project
    adopted = projects.adopt_project(ident, export_fixture, COMMIT, approved=True, root=storage)
    snapshot = p / adopted['adoption']['model_dir']
    original_model = (snapshot / 'model.json').read_bytes()
    first = projects.run_project(ident, storage)
    original_package = Path(first['output_package']).read_bytes()
    incoming = p / 'requests/settings.json'
    incoming.parent.mkdir()
    atomic_json(incoming, {'rig': {'head': {'bob': 4.125}}})
    result = editor.editor_save(ident, incoming, storage)
    assert result['saved'] and result['active_asset_changed'] is False
    assert (snapshot / 'model.json').read_bytes() == original_model
    assert not (snapshot / 'settings.json').exists()
    info = editor.editor_info(ident, storage)
    assert info['settings_present'] and info['files']['settings.json'].replace('\\', '/') == 'material/settings.json'
    second = projects.run_project(ident, storage, export_only=True)
    assert second['revision'] != first['revision']
    assert Path(first['output_package']).read_bytes() == original_package
    assert projects.run_project(ident, storage, export_only=True)['revision'] == second['revision']


def test_editor_rejects_foreign_settings_file(project, export_fixture, tmp_path):
    from seethrough_rig import production_editor as editor
    storage, _, ident, p = project
    projects.adopt_project(ident, export_fixture, COMMIT, approved=True, root=storage)
    outside = tmp_path / 'outside.json'
    atomic_json(outside, {'rig': {}})
    with pytest.raises(ProductionError, match='escapes'):
        editor.editor_save(ident, outside, storage)
    assert not (p / 'material/settings.json').exists()


def test_editor_rejects_executable_or_external_settings_without_mutating_state(project, export_fixture):
    from seethrough_rig import production_editor as editor
    from seethrough_rig.package_validate import ValidationError
    storage, _, ident, p = project
    projects.adopt_project(ident, export_fixture, COMMIT, approved=True, root=storage)
    before = (p / 'project.json').read_bytes()
    incoming = p / 'requests/settings.json'
    incoming.parent.mkdir()
    atomic_json(incoming, {'script': 'https://untrusted.invalid/exec.js'})
    with pytest.raises(ValidationError):
        editor.editor_save(ident, incoming, storage)
    assert (p / 'project.json').read_bytes() == before
    assert not (p / 'material/settings.json').exists()
