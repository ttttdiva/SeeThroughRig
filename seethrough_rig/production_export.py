"""Immutable data-only export with verified Git provenance and actual previews."""
from __future__ import annotations
import io
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from .package_validate import (COMMIT, HEX64, PROFILE, MAX_FILE, apply_settings, json_bytes, check_profile,
                               require, safe_name, validate_model, validate_package)
from .production_io import ProductionError, git_output, inside, no_links, read_file, sha, child_env
from .playback_settings import package_playback_settings

BASELINE_COMMIT = '46c0037779fb5e59715030925d66abdc6c09ca12'
BASELINE_MODEL = 'a34f9f4ec773c499537639f2d84316aa11b7b50f8a3bdad73f9b227b720de1db'
BASELINE_SOURCE = '33b0b9b2de84b7a86333595c1eefb496c11799b001503ac1bcf1182c38585f8a'

def verified_commit(repo, commit):
    require(isinstance(commit, str) and bool(COMMIT.fullmatch(commit)), 'Full Git commit SHA required')
    actual = git_output(repo, ['rev-parse', '--verify', commit + '^{commit}']).decode('ascii').strip()
    require(actual == commit.lower(), 'Git commit mismatch')
    return actual

def verified_model_core_ref(repo, payload, model, source_sha256, source_commit=None):
    """Resolve the playback engine from collected model identity, not adoption provenance."""
    if sha(payload['model.json']) != BASELINE_MODEL:
        if source_commit == BASELINE_COMMIT:
            raise ProductionError('provenance_mismatch', 'Baseline commit requires verified historical baseline model bytes')
        return 'canonical'
    require(source_sha256.lower() == BASELINE_SOURCE, 'Baseline source hash mismatch')
    # A matching model hash alone cannot authorize unrelated atlas/source bytes.
    historical = git_output(repo, ['show', BASELINE_COMMIT + ':web/models/aoi/model.json'])
    require(sha(historical) == BASELINE_MODEL, 'Historical model verification failed')
    for atlas in model['atlases']:
        historical_atlas = git_output(repo, ['show', BASELINE_COMMIT + ':web/models/aoi/' + atlas['file']])
        require(sha(historical_atlas) == sha(payload[atlas['file']]), 'Baseline atlas verification failed')
    historical_source = git_output(repo, ['show', BASELINE_COMMIT + ':assets/aoi/original.png'])
    require(sha(historical_source) == BASELINE_SOURCE, 'Baseline source verification failed')
    return BASELINE_COMMIT

def collect_model(model_dir, preview=None, settings=None, source_sha256=None):
    model_dir = no_links(model_dir).resolve()
    payload = {'model.json': read_file(inside(model_dir, model_dir / 'model.json'))}
    model = json_bytes(payload['model.json'])
    validate_model(model)
    from PIL import Image
    for atlas in model['atlases']:
        name = safe_name(atlas['file'])
        raw = read_file(inside(model_dir, model_dir / name))
        with Image.open(io.BytesIO(raw)) as img:
            require(img.format in ('PNG', 'WEBP') and img.size == (atlas['width'], atlas['height']) and img.width * img.height <= 64_000_000, 'Invalid atlas image')
            img.load()
        payload[name] = raw
    settings_path = Path(settings) if settings is not None else model_dir / 'settings.json'
    if settings is not None or settings_path.exists():
        raw = read_file(settings_path)
        validate_model(apply_settings(model, json_bytes(raw)))
        payload['settings.json'] = raw  # Preserve every tuning field and original bytes.
    package_playback_settings(model, json_bytes(payload['settings.json']) if 'settings.json' in payload else None)
    preview_path = Path(preview) if preview is not None else model_dir / 'rest_composite.png'
    if not preview_path.exists():
        raise ProductionError('preview_required', 'Actual model preview required: supply --preview or model-dir/rest_composite.png')
    raw = read_file(preview_path)
    if source_sha256 and sha(raw) == source_sha256.lower():
        raise ProductionError('preview_required', 'Original source cannot substitute for actual model preview')
    with Image.open(io.BytesIO(raw)) as img:
        require(img.width <= 8192 and img.height <= 8192 and img.width * img.height <= 64_000_000 and img.format in ('PNG', 'WEBP', 'JPEG'), 'Invalid preview image')
        img.load()
        buffer = io.BytesIO()
        img.convert('RGBA').save(buffer, format='PNG')
        payload['preview.png'] = buffer.getvalue()
    return payload, model, {'kind': 'explicit_model_preview' if preview is not None else 'build_rest_composite',
                            'input_sha256': sha(raw), 'artistic_quality_approved': False}

def runtime_capabilities(repo, model, settings, core_ref='canonical'):
    """Ask canonical87 behavior probes; no authored runtime implementation copied."""
    node = shutil.which('node')
    if not node:
        raise ProductionError('runtime_unavailable', 'Installed Node.js required to probe actual runtime capabilities during export')
    script = '''import fs from 'node:fs';
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const {decodeModel, applySettings} = await import(input.modelModule);
const {deriveCapabilities} = await import(input.runtimeModule);
const model = applySettings(decodeModel(input.model), input.settings);
console.log(JSON.stringify(deriveCapabilities(model, input.coreRef)));'''
    data = {'model': model, 'settings': settings, 'coreRef': core_ref,
            'modelModule': (repo / 'web/js/core/model.js').resolve().as_uri(),
            'runtimeModule': (repo / 'web/js/runtime/index.js').resolve().as_uri()}
    try:
        proc = subprocess.run([node, '--input-type=module', '-e', script], input=json.dumps(data).encode('utf-8'),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=child_env(), timeout=60)
        if proc.returncode:
            raise ProductionError('runtime_unavailable', 'Canonical runtime capability probe failed (no package emitted)')
        caps = json_bytes(proc.stdout)
        require(isinstance(caps, dict) and isinstance(caps.get('parameters'), list) and type(caps.get('headYaw')) is bool, 'Invalid runtime capability response')
        return caps
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProductionError('runtime_unavailable', 'Canonical runtime capability probe unavailable') from exc

def export_package(model_dir, output, project_id, revision, source_sha256, source_commit,
                   profile='comparison-v1', preview=None, settings=None, name=None, repo=None, provenance=None,
                   wind=None, wind_blow=None):
    repo = Path(repo) if repo else Path(__file__).resolve().parents[1]
    require(bool(HEX64.fullmatch(source_sha256)), '64 digit source SHA256 required')
    require(profile in ('comparison-v1', 'conversation-v1'), 'Unsupported profile')
    for value in (project_id, revision):
        require(isinstance(value, str) and 0 < len(value) <= 200, 'Project/revision required')
    commit = verified_commit(repo, source_commit)
    output = no_links(Path(output))
    if output.exists():
        raise ProductionError('output_exists', 'Refusing to replace an existing package')
    payload, model, preview_info = collect_model(Path(model_dir), preview, settings, source_sha256)
    caps = validate_model(apply_settings(model, json_bytes(payload['settings.json']))) if 'settings.json' in payload else validate_model(model)
    prov = dict(provenance or {})
    # Caller cannot override verified fields.
    prov.update({'commit': commit, 'commit_verified': True, 'model_sha256': sha(payload['model.json']),
                 'preview': preview_info, 'artifact_files': {n: sha(b) for n, b in payload.items()}})
    runtime = {'abi': 1, 'version': '0.1.0'}
    core_ref = verified_model_core_ref(repo, payload, model, source_sha256, commit)
    if core_ref != 'canonical':
        runtime['core_ref'] = core_ref
    # Generated/adopted artifacts have no fabricated committed model origin.
    prov['model_commit'] = core_ref if core_ref != 'canonical' else None
    caps = runtime_capabilities(repo, model, json_bytes(payload['settings.json']) if 'settings.json' in payload else None, core_ref)
    # playback profile: conversation-v1 may carry the breeze and the wind blowing
    profile_entry = {'id': profile, **PROFILE}
    if wind is not None or wind_blow:
        require(profile == 'conversation-v1', 'Wind settings require the conversation-v1 profile')
    if wind is not None:
        profile_entry['wind'] = float(wind)
    if wind_blow:
        require('core_ref' not in runtime, 'WindBlow requires the current runtime core')
        profile_entry['windBlow'] = float(wind_blow)
    check_profile(profile_entry, runtime.get('core_ref'))
    playback_settings = package_playback_settings(model,
        json_bytes(payload['settings.json']) if 'settings.json' in payload else None, profile_entry,
        wind=wind, wind_blow=wind_blow)
    manifest = {'schema_version': 1, 'model_format': 'seethrough-rig', 'model_format_version': 1,
                'runtime': runtime, 'project_id': project_id, 'revision': revision,
                'name': name or model.get('name') or project_id, 'provenance': prov,
                'source_sha256': source_sha256.lower(), 'model': 'model.json',
                'settings': 'settings.json' if 'settings.json' in payload else None, 'preview': 'preview.png',
                'profile': profile_entry, 'playbackSettings': playback_settings, 'capabilities': caps,
                'files': [{'path': n, 'sha256': sha(b), 'size': len(b)} for n, b in sorted(payload.items())]}
    # Validate all manifest fields too, before publishing the package.
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix='.export-', suffix='.zip', dir=output.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        with zipfile.ZipFile(temp, 'w', compression=zipfile.ZIP_STORED) as z:
            for n, raw in [('manifest.json', (json.dumps(manifest, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')), *sorted(payload.items())]:
                require(len(raw) <= MAX_FILE, 'Export file too large')
                info = zipfile.ZipInfo(n, date_time=(2026, 1, 1, 0, 0, 0))
                info.external_attr = 0o100644 << 16
                z.writestr(info, raw)
        checked = validate_package(temp)
        # Refuse overwrite even if another exporter won the race.
        os.link(temp, output)
        return {'package_path': str(output.resolve()), 'package_sha256': sha(read_file(output, 256 * 1024 * 1024)),
                'manifest': checked['manifest'], 'capabilities': caps,
                'artistic_quality_approved': False}
    finally:
        temp.unlink(missing_ok=True)
