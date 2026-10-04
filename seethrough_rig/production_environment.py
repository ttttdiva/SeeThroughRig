"""Portable readiness and explicit lightweight environment preparation."""
from __future__ import annotations
import importlib
import importlib.util
import os
import shutil
import sys
from pathlib import Path
from .production_io import ProductionError, lock, no_links, read_json, run_child

DEPENDENCIES = {'numpy': 'numpy>=1.26', 'PIL': 'pillow>=10', 'cv2': 'opencv-python-headless>=4.9', 'scipy': 'scipy>=1.11'}
KNOWN = {'root': Path('F:/AI/sources/see-through'), 'python': Path('F:/AI/runtimes/see-through/Scripts/python.exe'), 'models': Path('F:/AI/models/see-through')}
VARIABLES = {'root': 'SEETHROUGH_ROOT', 'python': 'SEETHROUGH_PYTHON', 'models': 'SEETHROUGH_MODELS'}
FIXED_SOURCE = 'https://github.com/ttttdiva/SeeThroughRig.git'

def resolve_environment(repo):
    config_path = repo / '.local' / 'production-environment.json'
    saved = read_json(config_path) if config_path.exists() else {}
    if not isinstance(saved, dict):
        raise ProductionError('invalid_configuration', 'Environment settings must be object')
    result = {}
    for key, var in VARIABLES.items():
        value = os.environ.get(var)
        if value is None:
            value = saved.get(key)
        if value is None and KNOWN[key].exists():
            value = str(KNOWN[key])
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ProductionError('invalid_configuration', 'Invalid explicit inference path: ' + key)
        result[key] = no_links(Path(value)).resolve() if value else None
    return result

def doctor(repo=None):
    repo = Path(repo) if repo else Path(__file__).resolve().parents[1]
    paths = resolve_environment(repo)
    dependencies = {name: importlib.util.find_spec(name) is not None for name in DEPENDENCIES}
    problems = []
    for key, p in paths.items():
        if p is None or not (p.is_file() if key == 'python' else p.is_dir()):
            problems.append('Configure local inference ' + key + ' (' + VARIABLES[key] + ')')
    script = paths['root'] / 'inference/scripts/inference_psd_quantized.py' if paths['root'] else None
    if script is None or not script.is_file():
        problems.append('Official See-through inference script missing')
    material = {}
    for key in ('layerdiff', 'depth'):
        base = paths['models'] / 'models' / key if paths['models'] else None
        present = bool(base and base.is_dir() and any(base.rglob('*.safetensors')))
        material[key] = {'present': present, 'path': str(base) if base else None}
        if not present:
            problems.append('Existing local ' + key + ' weights required; never downloaded automatically')
    marker = repo / 'seethrough-production.json'
    installed = marker.is_file() and (repo / 'seethrough_rig/integration.py').is_file() and Path(sys.executable).is_file()
    if installed:
        identity = read_json(marker)
        installed = identity.get('identity') == 'seethrough-rig' and identity.get('protocol_version') == 1
    for name, present in dependencies.items():
        if not present:
            problems.append('Missing lightweight dependency: ' + name)
    ready = installed and all(dependencies.values()) and not problems
    return {'version': '0.1.0', 'protocol_version': 1, 'root': str(repo.resolve()), 'python': str(Path(sys.executable).resolve()),
            'inference': {k: str(p) if p else None for k, p in paths.items()}, 'dependencies': dependencies,
            'settings': {'path': str(repo / '.local/production-environment.json'), 'present': (repo / '.local/production-environment.json').is_file()},
            'materials': material, 'installed': bool(installed), 'ready': bool(ready),
            'build_ready': all(dependencies.values()), 'export_ready': dependencies['PIL'] and bool(shutil.which('node')), 'runtime_node': shutil.which('node'),
            'status': 'ready' if ready else 'needs_input', 'problems': problems,
            'managed': False, 'can_manage': True, 'downloads_performed': False}

def prepare_environment(repo=None, operation='prepare'):
    """Explicit41admin operation. Fixed dependency source; no arbitrary URLs, venv deletion or inference weights."""
    repo = no_links(Path(repo) if repo else Path(__file__).resolve().parents[1])
    with lock(repo / '.local/production-prepare.lock'):
        before = doctor(repo)
        if operation in ('install', 'update'):
            # No integration-containing trusted release revision supplied in main contract.
            return {**before, 'operation': operation, 'status': 'needs_input', 'fixed_source': FIXED_SOURCE,
                    'problems': ['Trusted pinned integration release revision is required for managed ' + operation],
                    'changed': False}
        missing = [requirement for mod, requirement in DEPENDENCIES.items() if not before['dependencies'][mod]]
        if not missing:
            return {**before, 'operation': operation, 'changed': False, 'prepared': True}
        if sys.prefix == sys.base_prefix:
            raise ProductionError('venv_required', 'Use the existing tool venv; system pip is prohibited')
        if shutil.disk_usage(repo).free < 1024 * 1024 * 1024:
            raise ProductionError('disk_space', 'At least 1 GiB free space required for lightweight dependencies and temporary wheels')
        log = repo / '.local/production-prepare.log'
        if importlib.util.find_spec('pip') is None:
            # Explicit preparation only; ensurepip uses bundled wheels, not network.
            run_child([sys.executable, '-m', 'ensurepip', '--default-pip'], repo, log)
        wheels = no_links(repo / '.local/production-wheels')
        cache = no_links(repo / '.local/production-pip-cache')
        offline = wheels.is_dir() and any(wheels.glob('*.whl'))
        command = [sys.executable, '-m', 'pip', '--isolated', '--disable-pip-version-check',
                   '--no-input', '--cache-dir', str(cache), 'install', '--only-binary=:all:']
        if offline:
            command.extend(['--no-index', '--find-links', str(wheels)])
        else:
            # Fixed official index and source-controlled lightweight requirements.
            # No URL/requirement is accepted from an avatar package or user input.
            command.extend(['--index-url', 'https://pypi.org/simple'])
        command.extend(missing)
        run_child(command, repo, log, env={'PIP_NO_INDEX': '1' if offline else '0'})
        importlib.invalidate_caches()
        after = doctor(repo)
        return {**after, 'operation': operation, 'changed': True,
                'prepared': all(after['dependencies'].values()),
                'downloads_performed': not offline, 'weights_downloaded': False,
                'lightweight_cache': str(cache)}
