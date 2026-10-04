"""Durable isolated production projects; 41 owns agent orchestration."""
from __future__ import annotations
import io
import json
import os
import sys
import time
import uuid
from pathlib import Path
from .package_validate import MAX_TOTAL, PROFILE, json_bytes, require, safe_name, validate_package
from .production_io import (Cancelled, ProductionError, atomic_bytes, atomic_json, checkpoint, git_output,
                            inside, lock, no_links, read_file, read_json, run_child, sha)
from .production_environment import doctor
from .production_export import collect_model, export_package, verified_commit
from .playback_settings import package_playback_settings

REPO = Path(__file__).resolve().parents[1]
ART = ('eye_closed', 'eye_half', 'mouth_half', 'mouth_open', 'happy', 'surprised', 'worried', 'shy', 'brow_clear')

def projects_root(value=None):
    return no_links(Path(value or os.environ.get('SEETHROUGH_PROJECTS_ROOT') or REPO / 'projects')).resolve()

def project_path(project_id, root=None):
    try:
        ident = str(uuid.UUID(str(project_id)))
    except ValueError as exc:
        raise ProductionError('invalid_project', 'Canonical UUID project ID required') from exc
    if ident != str(project_id).lower():
        raise ProductionError('invalid_project', 'Canonical UUID project ID required')
    return inside(projects_root(root), projects_root(root) / ident)

def load_project(project_id, root=None):
    p = project_path(project_id, root)
    state = read_json(p / 'project.json')
    require(isinstance(state, dict) and state.get('identity') == 'seethrough-rig-project' and state.get('protocol_version') == 1 and state.get('project_id') == p.name, 'Invalid project identity')
    for key in ('owner', 'character'):
        require(isinstance(state.get(key), str) and state[key], 'Invalid project ownership')
    if sha(read_file(p / 'input/source-original')) != state['source_sha256'] or sha(read_file(p / 'input/source.png')) != state['input_sha256']:
        raise ProductionError('source_changed', 'Project source changed; create a new project instead of reusing cached stages')
    return p, state

def save(p, state, **updates):
    state.update(updates)
    state['updated_at'] = time.time()
    atomic_json(p / 'project.json', state)
    return state

def instructions(p, state):
    return f'''# See-through Rig production procedure
Project: {state['project_id']}. Scope: this project directory ONLY.
Owner/character are immutable identity fields. Never access other project, global recipes,
assets/aoi, work/aoi or model sources. 41 invokes its existing agent harness; 87 never
starts an agent or selects a model. Do not disclose source/artwork to an external image
service without the host approval gateway. Use the host-selected existing agent configuration; no specific agent vendor or model is required.

1. Run python -m seethrough_rig.integration project-run --project {state['project_id']} --projects-root "{p.parent}" --json.
   It performs local offline full1280 + full1024 + source-relative square head1280
   decomposition, records boxes/density/seeds/source hash and returns concrete deficits.
   Inspect full pass layers, alternate resolution missing parts and head crop. Keep
   every experiment in this project. Retry seeds only by explicitly changing project
   recipe/workflow configuration; no reuse from another source.
2. Author recipe/rig.json using the EXISTING 87 builder recipe schema. Paths relative
   to that file, contained here. Runner replaces work with verified hash-cached local
   decomposition. Select layers per pass (1280 can omit clothing/ribbons; compare1024).
   Repair ribbons/headwear and occluded hair; remove hallucinated facial outlines.
   Restore visible original pixels (restore.enabled=true), retain inpainting only
   where hidden. Lift lashes over front hair; detach cords and patch cloth underneath.
   Split hair along drawn strand boundaries with overlap; conservative physics and
   roots/bend lengths, panel split skirt. Estimate/edit landmarks and mesh spacing.
3. Supply aligned head-crop artwork in material/: eye_closed, eye_half, mouth_half,
   mouth_open, happy, surprised, worried, shy, brow_clear. Include all paths in
   expressions.images and an explicit square source-coordinate expressions.box.
   Inspect image alignment, line/color preservation, extraction edges and occlusion.
   Do not compress eyes/mouth to fake blinking/speech. Use discrete single-image
   switches, blink-before-expression, iris clip, only one visible variant each frame.
4. Use rig.head.mode=rigid and rig.body.rigid=true for original approved workflow.
   No unsupported yaw claim: rigid AngleX has no authored shape. Avoid rejected
   inferred/fitted angle-keyform workflow. Copy no authoring implementation into41.
   Include order, layers, expression_presets, views, restoration/cleanup/bundles/strands
   appropriate to THIS artwork. Settings may be saved as material/settings.json;
   exporter preserves every field including strands/visibility unknown safe data.
5. Run project-run again: existing87 decompose/build, strict data validation, immutable
   export. rest_composite.png from build is actual preview; source is never fallback.
   Structural export is not artistic approval. Inspect editor poses and animation,
   expressions, speech stop/mouth closure, clipping, strand gaps. Use existing87
   warp_check (5% strain, overlap gap, single variant over13poses +600/360frames)
   and rigid_check through host-scoped editor/runtime. Host performs runtime/video
   QA and reports evidence; do not write passed:true as substitute for verification.
6. Missing artwork/repair/recipe is needs_input. Report role, crop, expected location
   and concrete recovery. No synthetic completion. Approved baseline adoption only
   via project-adopt --approved --source-commit FULL_SHA --model-dir HOST_APPROVED_PATH;
   it records cache reuse and does not claim new inference or new artistic approval.
   project-cancel cooperates during subprocesses; project-resume checks source hashes.
'''

def create_project(source, project_id, owner, character, root=None):
    p = project_path(project_id, root)
    for value in (owner, character):
        require(isinstance(value, str) and 0 < len(value) <= 200 and not any(ord(x) < 32 for x in value), 'Owner/character required')
    raw = read_file(Path(source))
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as im:
        require(im.format in ('PNG', 'JPEG', 'WEBP') and im.width <= 8192 and im.height <= 8192 and im.width * im.height <= 40_000_000, 'Unsupported source dimensions/format')
        im.load()
        size = list(im.size)
        b = io.BytesIO()
        im.convert('RGBA').save(b, format='PNG')
        normalized = b.getvalue()
    p.parent.mkdir(parents=True, exist_ok=True)
    with lock(p.parent / '.create.lock'):
        if p.exists():
            old_p, old = load_project(project_id, root)
            if old['owner'] == owner and old['character'] == character and old['source_sha256'] == sha(raw):
                return {**status(project_id, root), 'reused': True}
            raise ProductionError('project_exists', 'Project ID already belongs to a different input/owner/character')
        staging = p.parent / ('.create-' + uuid.uuid4().hex)
        staging.mkdir()
        for name in ('input', 'recipe', 'material', 'history', 'work', 'logs', 'preview'):
            (staging / name).mkdir()
        state = {'identity': 'seethrough-rig-project', 'protocol_version': 1, 'project_id': p.name,
                 'owner': owner, 'character': character, 'source_sha256': sha(raw), 'input_sha256': sha(normalized),
                 'source_size': size, 'status': 'created', 'stage': 'input', 'revision': None,
                 'output_package': None, 'problems': [], 'deficits': [], 'created_at': time.time(),
                 'inference_performed': False, 'artistic_quality_approved': False}
        atomic_bytes(staging / 'input/source-original', raw)
        atomic_bytes(staging / 'input/source.png', normalized)
        atomic_json(staging / 'project.json', state)
        atomic_json(staging / 'material/requirements.json', {'expression_roles': list(ART), 'source_size': size,
                     'repairs': 'Inspect missing full-pass parts, ribbons/headwear and occluded hair; repair only approved project material'})
        atomic_bytes(staging / 'agent-instructions.md', instructions(p, state).encode('utf-8'))
        os.rename(staging, p)
    return {**status(project_id, root), 'reused': False}

def status(project_id, root=None):
    p, state = load_project(project_id, root)
    result = dict(state)
    result['project_path'] = str(p)
    result['agent_instructions'] = str(p / 'agent-instructions.md')
    if (p / 'cancel.request').exists():
        result['cancel_requested'] = True
        if state['status'] != 'running':
            result['status'] = 'cancelled'
    if state['status'] == 'running':
        try:
            with lock(p / 'operation.lock'):
                result['status'] = 'interrupted'
                result['problems'] = ['Previous process stopped; project-resume revalidates hashes before retry']
        except ProductionError as exc:
            if exc.code != 'busy':
                raise
    if state.get('output_package'):
        package = inside(p, Path(state['output_package']))
        checked = validate_package(package)
        require(checked['manifest']['project_id'] == p.name and checked['manifest']['source_sha256'] == state['source_sha256'], 'Project package identity mismatch')
    return result

def cancel(project_id, root=None):
    p, state = load_project(project_id, root)
    atomic_bytes(p / 'cancel.request', b'cancel\n')
    # Do not race the running writer by editing project.json here.
    return {**status(project_id, root), 'cancel_requested': True}

def tree_hashes(directory, project=None):
    result = {}
    if not directory.exists():
        return result
    files = sorted(directory.rglob('*'))
    require(len(files) <= 10000, 'Project file count limit')
    for file in files:
        no_links(file)
        if file.is_file() and file.suffix.lower() not in ('.log', '.psd'):
            if project:
                checkpoint(project)
            result[str(file.relative_to(directory)).replace('\\', '/')] = sha(read_file(file))
    return result

def cache_valid(directory, record, project):
    if not record or not directory.exists():
        return False
    try:
        return tree_hashes(directory, project) == record['files']
    except Cancelled:
        raise
    except (OSError, ProductionError, KeyError):
        return False

def authored_recipe(p):
    seen = set()
    def merge(a, b):
        for k, v in b.items():
            if isinstance(v, dict) and isinstance(a.get(k), dict):
                merge(a[k], v)
            else:
                a[k] = v
        return a
    def load(file):
        file = inside(p, file)
        require(file not in seen and len(seen) < 16, 'Cyclic/deep recipe inheritance')
        seen.add(file)
        data = read_json(file)
        require(isinstance(data, dict), 'Recipe must be object')
        if 'extends' in data:
            ext = data.pop('extends')
            require(isinstance(ext, str), 'Invalid recipe extension')
            data = merge(load(file.parent / ext), data)
        return data
    return load(p / 'recipe/rig.json')

def scoped_recipe(p, work):
    recipe = authored_recipe(p)
    # Existing builder interprets inherited paths from the leaf recipe's directory.
    recipe['work'] = str(inside(p, work))
    if recipe.get('keyforms'):
        raise ProductionError('unsupported_workflow', 'Fitted/angle keyforms are excluded from approved rigid production workflow')
    for tag, spec in recipe.get('layers', {}).items():
        safe_name(tag + '.png')
        if 'image' in spec:
            spec['image'] = str(inside(p, p / 'recipe' / spec['image']))
            read_file(Path(spec['image']))
    images = recipe.get('expressions', {}).get('images', {})
    for key, value in images.items():
        images[key] = str(inside(p, p / 'recipe' / value))
        read_file(Path(images[key]))
    meta = read_json(work / 'decompose.json')
    for pas in meta.get('passes', {}).values():
        directory = inside(p, Path(pas['dir']))
        info = read_json(directory / 'info.json')
        require(info.get('frame_size') and info['frame_size'][0] > 0, 'Invalid inference frame')
        for tag in info.get('parts', {}):
            safe_name(tag + '.png')
            for suffix in ('.png', '_depth.png'):
                file = inside(p, directory / (tag + suffix))
                if file.exists():
                    read_file(file)
    box = recipe.get('expressions', {}).get('box')
    require(isinstance(box, list) and len(box) == 4 and all(type(x) in (int, float) for x in box), 'Invalid expression crop box')
    require(0 <= box[0] < box[2] <= meta['source_size'][0] and 0 <= box[1] < box[3] <= meta['source_size'][1] and abs((box[2]-box[0])-(box[3]-box[1])) <= 1, 'Expression artwork requires source-aligned square head crop')
    from PIL import Image
    image_sizes = set()
    for value in images.values():
        with Image.open(value) as img:
            require(img.width <= 8192 and img.height <= 8192 and img.width * img.height <= 40_000_000, 'Expression image size limit')
            img.load()
            image_sizes.add(img.size)
    require(len(image_sizes) == 1, 'Expression artworks must have matching head-crop dimensions')
    # Prevent builder silently skipping a chosen missing tag.
    for tag, spec in recipe.get('layers', {}).items():
        if 'image' in spec:
            continue
        require(spec.get('pass') in meta.get('passes', {}), 'Unknown chosen decomposition pass')
        directory = inside(p, Path(meta['passes'][spec['pass']]['dir']))
        require((directory / (spec.get('tag', tag) + '.png')).is_file(), 'Chosen decomposition layer missing; repair or select alternate pass: ' + tag)
    return recipe

def deficits(p, work):
    result = []
    crop = None
    meta_file = work / 'decompose.json'
    if meta_file.exists():
        crop = read_json(meta_file).get('passes', {}).get('head', {}).get('box')
    recipe_path = p / 'recipe/rig.json'
    if not recipe_path.exists():
        result.append({'role': 'recipe', 'path': 'recipe/rig.json', 'reason': 'Author per-layer pass selection, restoration, repairs, rigid rig, strands and expressions for this source'})
        images = {}
    else:
        r = authored_recipe(p)
        require(isinstance(r.get('expressions', {}), dict), 'Invalid expression recipe')
        images = r.get('expressions', {}).get('images', {})
        require(isinstance(images, dict), 'Invalid expression images')
        if not (r.get('restore', {}).get('enabled') is True and r.get('rig', {}).get('head', {}).get('mode') == 'rigid' and r.get('rig', {}).get('body', {}).get('rigid') is True):
            result.append({'role': 'workflow_recipe', 'path': 'recipe/rig.json', 'reason': 'Enable visible restoration and rigid head/body for approved source-preserving workflow'})
        if not (r.get('layers') and r.get('order') and r.get('expressions', {}).get('box')):
            result.append({'role': 'recipe_bindings', 'path': 'recipe/rig.json', 'reason': 'Supply per-layer pass selection, drawing order and source-coordinate expression crop'})
    for role in ART:
        value = images.get(role)
        target = inside(p, p / 'recipe' / value) if isinstance(value, str) else None
        if not target or not target.is_file():
            result.append({'role': role, 'path': 'material/' + role + '.png', 'crop': crop,
                           'reason': 'Source-aligned actual artwork required; populate expressions.images'})
    return result

def workflow_key(state, env):
    config = {'source': state['input_sha256'], 'full': 1280, 'extra_full': [1024], 'head': 1280,
              'seed': 42, 'steps': 30, 'inference': env['inference'],
              'decompose_module': sha(read_file(REPO / 'seethrough_rig/decompose.py'))}
    return sha(json.dumps(config, sort_keys=True).encode())


def build_cache_matches(inputs, record):
    """Tuning is applied at playback/export, never an input to mesh generation.

    Old cache keys included material/settings.json. Reconstruct that old key from
    the immutable cached settings hash, so an editor save doesn't rebuild artwork.
    The caller must still verify every cached output byte before reusing it.
    """
    if record.get('key') == sha(json.dumps(inputs, sort_keys=True).encode()):
        return True
    legacy_material = dict(inputs['material'])
    previous_settings = record.get('files', {}).get('settings.json')
    if previous_settings:
        legacy_material['settings.json'] = previous_settings
    legacy = {**inputs, 'material': legacy_material}
    return record.get('key') == sha(json.dumps(legacy, sort_keys=True).encode())

def run_project(project_id, root=None, resume=False, export_only=False):
    p, state = load_project(project_id, root)
    with lock(p / 'operation.lock'):
        p, state = load_project(project_id, root)
        if resume:
            no_links(p / 'cancel.request').unlink(missing_ok=True)
        try:
            checkpoint(p)
            save(p, state, status='running', stage='reconcile', problems=[], deficits=[],
                 previous_revision=state.get('revision'), revision=None, output_package=None)
            if state.get('adoption'):
                model_dir = inside(p, p / state['adoption']['model_dir'])
                if tree_hashes(model_dir, p) != state['adoption']['files']:
                    raise ProductionError('adoption_changed', 'Adopted model snapshot changed; explicit re-adoption required')
                model_commit = state['adoption']['commit']
                preview = model_dir / 'preview.png'
                provenance = {'production': 'adopted_existing_model', 'new_inference': False, 'approval': 'explicit_host_approval'}
            else:
                env = doctor(REPO)
                key = workflow_key(state, env)
                cache = state.get('decomposition_cache', {})
                work = inside(p, p / cache['directory']) if cache.get('key') == key and cache.get('directory') else p / 'work' / ('inference-' + key[:16])
                if not cache_valid(work, cache, p):
                    if export_only or not env['ready']:
                        return save(p, state, status='needs_input', stage='environment', problems=env['problems'] or ['Run project-run to decompose source first'], deficits=deficits(p, work))
                    work = p / 'work' / ('inference-' + key[:16] + '-' + uuid.uuid4().hex[:8])
                    save(p, state, stage='decompose')
                    with lock(projects_root(root) / '.gpu.lock'):
                        run_child([sys.executable, '-m', 'seethrough_rig.cli', 'decompose', p / 'input/source.png',
                                   '--work', work, '--extra-full', '1024', '--force'], REPO, p / 'logs/decompose.log', p,
                                  {**{ 'SEETHROUGH_' + k.upper(): v for k, v in env['inference'].items() if v},
                                   'PYTHONPATH': str(REPO)})
                    cache = {'key': key, 'directory': str(work.relative_to(p)), 'files': tree_hashes(work, p)}
                    require('decompose.json' in cache['files'], 'Missing decomposition outputs')
                    save(p, state, decomposition_cache=cache, inference_performed=True)
                missing = deficits(p, work)
                if missing:
                    return save(p, state, status='needs_input', stage='artwork', deficits=missing, problems=['Recipe/aligned artwork requires project agent or user input'])
                recipe = scoped_recipe(p, work)
                require(recipe.get('restore', {}).get('enabled') is True and recipe.get('rig', {}).get('head', {}).get('mode') == 'rigid' and recipe.get('rig', {}).get('body', {}).get('rigid') is True, 'Approved workflow requires visible restoration and rigid head/body')
                require(recipe.get('layers') and recipe.get('order') and recipe.get('expressions', {}).get('box'), 'Incomplete production recipe')
                for role in ART:
                    require(role in recipe.get('expressions', {}).get('images', {}), 'Missing artwork role: ' + role)
                material = tree_hashes(p / 'material', p)
                material.pop('settings.json', None)
                inputs = {'recipe': recipe, 'material': material, 'decomposition': cache['files'],
                          'builder': sha(read_file(REPO / 'seethrough_rig/build.py'))}
                build_key = sha(json.dumps(inputs, sort_keys=True).encode())
                bc = state.get('build_cache', {})
                model_dir = inside(p, p / bc['directory']) if build_cache_matches(inputs, bc) and bc.get('directory') else p / 'work' / ('build-' + build_key[:16])
                if not cache_valid(model_dir, bc, p):
                    if export_only:
                        return save(p, state, status='needs_input', stage='build', problems=['Run project-run for current recipe/materials'])
                    if not env['build_ready']:
                        return save(p, state, status='needs_input', stage='environment', problems=env['problems'])
                    model_dir = p / 'work' / ('build-' + build_key[:16] + '-' + uuid.uuid4().hex[:8])
                    model_dir.mkdir(parents=True)
                    normalized = model_dir / 'recipe.json'
                    atomic_json(normalized, recipe)
                    save(p, state, stage='build')
                    run_child([sys.executable, '-m', 'seethrough_rig.cli', 'build', normalized, '--out', model_dir], REPO, p / 'logs/build.log', p, {'PYTHONPATH': str(REPO)})
                    if (p / 'material/settings.json').exists():
                        atomic_bytes(model_dir / 'settings.json', read_file(p / 'material/settings.json'))
                    bc = {'key': build_key, 'directory': str(model_dir.relative_to(p)), 'files': tree_hashes(model_dir, p)}
                    save(p, state, build_cache=bc)
                model_commit = git_output(REPO, ['rev-parse', 'HEAD']).decode().strip()
                preview = None
                provenance = {'production': 'project_workflow', 'new_inference': state['inference_performed'],
                              'decomposition_key': key, 'build_key': build_key}
            checkpoint(p)
            save(p, state, stage='validate_export')
            # Stable content revision includes settings+preview and source; no stale mtime cache.
            tuning = p / 'material/settings.json'
            tuning = tuning if tuning.is_file() else None
            payload, model, _ = collect_model(model_dir, preview=preview, settings=tuning, source_sha256=state['source_sha256'])
            # Production models are conversation packages. Comparison is only an
            # explicitly requested low-level export and retains its fixed motion.
            profile = {'id': 'conversation-v1', **PROFILE}
            playback_settings = package_playback_settings(model,
                json_bytes(payload['settings.json']) if 'settings.json' in payload else None, profile)
            revision = sha(json.dumps({'files': {n: sha(b) for n, b in payload.items()}, 'source': state['source_sha256'], 'commit': model_commit, 'provenance': provenance,
                                      'profile': profile, 'playbackSettings': playback_settings}, sort_keys=True).encode())[:32]
            output = inside(p, p / 'history' / (revision + '.zip'))
            if output.exists():
                checked = validate_package(output)
                require(checked['manifest']['project_id'] == p.name and checked['manifest']['source_sha256'] == state['source_sha256'] and {x['path']: x['sha256'] for x in checked['manifest']['files']} == {n: sha(b) for n, b in payload.items()}, 'Existing immutable revision differs')
                require(checked['manifest']['profile'] == profile and checked['manifest'].get('playbackSettings') == playback_settings, 'Existing immutable playback defaults differ')
                result = {'package_path': str(output), 'manifest': checked['manifest']}
            else:
                result = export_package(model_dir, output, p.name, revision, state['source_sha256'], model_commit,
                                        profile='conversation-v1', preview=preview, settings=tuning, repo=REPO, provenance=provenance)
            checkpoint(p)
            return save(p, state, status='completed', stage='export', revision=revision, output_package=result['package_path'],
                        problems=[], deficits=[], artistic_quality_approved=False)
        except Cancelled:
            return save(p, state, status='cancelled', problems=['Production cancelled; resume retains inputs and revalidates cache'])
        except Exception as exc:
            save(p, state, status='failed', problems=[str(exc)])
            raise

def adopt_project(project_id, model_dir, source_commit, approved=False, preview=None, settings=None, root=None):
    if not approved:
        raise ProductionError('approval_required', 'Host approval of existing model adoption required (--approved)')
    p, state = load_project(project_id, root)
    with lock(p / 'operation.lock'):
        checkpoint(p)
        commit = verified_commit(REPO, source_commit)
        payload, _, preview_info = collect_model(Path(model_dir), preview, settings, state['source_sha256'])
        dest = p / 'material' / ('adopted-' + uuid.uuid4().hex)
        dest.mkdir()
        for name, raw in payload.items():
            atomic_bytes(dest / name, raw, exclusive=True)
        adoption = {'model_dir': str(dest.relative_to(p)), 'files': tree_hashes(dest, p), 'commit': commit,
                    'new_inference': False, 'approval': 'explicit_host_approval', 'preview': preview_info}
        save(p, state, adoption=adoption, status='created', stage='adopted', output_package=None, revision=None,
             problems=[], deficits=[], inference_performed=False, artistic_quality_approved=False)
    return status(project_id, root)
