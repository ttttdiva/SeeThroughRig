"""Bounded, data-only ABI1 package validation (stdlib; never extracts archives).

Reference for backend implementers, not a replacement for their trust boundary.
"""
from __future__ import annotations
import base64
import binascii
import hashlib
import json
import math
import re
import stat
import struct
import unicodedata
import zipfile
import zlib
from pathlib import Path
from .playback_settings import validate_playback_settings, resolve_playback_settings, profile_playback_settings

MAX_FILES = 128
MAX_FILE = 64 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
MAX_JSON = 32 * 1024 * 1024
MAX_DEPTH = 32
MAX_ITEMS = 2_000_000
MAX_VERTICES = 250_000
HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
COMMIT = re.compile(r"^[0-9a-fA-F]{40}$")
BAD_KEYS = {"__proto__", "prototype", "constructor"}
RESERVED = {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)} | {p + i for p in ('COM', 'LPT') for i in ('¹', '²', '³')}
PROFILE = {"seed": 3, "wind": 0, "idle": True, "breath": True, "blink": True, "saccade": True, "lookAt": False, "follow": 0.6}

def _unit(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def check_profile(profile, core_ref=None):
    """comparison-v1 is the fixed approved profile (no breeze, no wind). A
    conversation-v1 profile may set the breeze (wind) and the wind blowing hair,
    ribbon cords and clothes (windBlow, current core only), each 0..1."""
    require(isinstance(profile, dict) and profile.get('id') in ('comparison-v1', 'conversation-v1'), "invalid profile")
    require(set(profile) <= {'id', 'windBlow', *PROFILE}, "invalid profile")
    fixed = {k: v for k, v in PROFILE.items() if k != 'wind'}
    require(all(type(profile.get(k)) is type(v) and profile[k] == v for k, v in fixed.items()), "invalid profile")
    wind, blow = profile.get('wind'), profile.get('windBlow', 0)
    require(_unit(wind) and _unit(blow), "invalid profile wind")
    if profile['id'] == 'comparison-v1':
        require(wind == 0 and blow == 0 and 'windBlow' not in profile, "comparison profile has no wind")
    if blow:
        require(core_ref in (None, 'canonical'), "windBlow requires the current runtime core")


class ValidationError(ValueError):
    code = "invalid_package"

def require(condition, message):
    if not condition:
        raise ValidationError(message)

def safe_name(name: str) -> str:
    require(isinstance(name, str) and 0 < len(name) <= 120, "invalid file name")
    require(unicodedata.normalize("NFC", name) == name, "noncanonical filename")
    require(not any(c in name for c in '/\\:<>"|?*') and not any(ord(c) < 32 or ord(c) == 127 for c in name), "unsafe flat filename")
    require(name not in (".", "..") and not name.endswith((".", " ")), "Windows filename alias")
    require(name.split('.')[0].upper() not in RESERVED, "Windows device filename")
    require(Path(name).suffix.lower() in (".json", ".png", ".webp"), "unsupported resource type")
    return name

def json_bytes(raw: bytes):
    require(len(raw) <= MAX_JSON, "JSON too large")
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result and key not in BAD_KEYS, "duplicate or prototype JSON key")
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                           parse_constant=lambda x: (_ for _ in ()).throw(ValidationError("nonfinite JSON")))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValidationError("invalid JSON") from exc
    budget = [MAX_ITEMS]
    def walk(v, depth):
        budget[0] -= 1
        require(budget[0] >= 0 and depth <= MAX_DEPTH, "JSON complexity limit")
        if isinstance(v, dict):
            for k, x in v.items():
                require(len(k) < 256 and k not in BAD_KEYS, "invalid JSON key")
                walk(x, depth + 1)
        elif isinstance(v, list):
            for x in v:
                walk(x, depth + 1)
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            require(math.isfinite(v) and abs(v) <= 1e12, "invalid numeric value")
        elif isinstance(v, str):
            require(len(v) <= MAX_JSON, "string too large")
            require(not re.search(r"(?:https?|file|data|javascript|ftp)://|^data:|^javascript:", v, re.I), "external or executable URL")
    walk(value, 0)
    return value

def typed_array(value, dtype=None, count=None):
    if isinstance(value, list):
        require(len(value) <= MAX_VERTICES * 6, "array too large")
        require(all(type(x) in (int, float) and math.isfinite(x) and abs(x) <= 1e9 for x in value), "invalid array number")
        values = value
        if dtype in ('u16', 'u32', 'index'):
            require(all(type(x) is int and x >= 0 for x in values), "invalid integer array")
    else:
        require(isinstance(value, dict) and set(value) == {'b64', 'dtype'}, "invalid typed array")
        dt = value['dtype']
        require(dt in ('f32', 'u16', 'u32') and (dtype in (None, 'index') or dt == dtype), "unsupported array dtype")
        require(dtype != 'index' or dt in ('u16', 'u32'), "indices require unsigned integer dtype")
        require(isinstance(value['b64'], str) and len(value['b64']) <= MAX_VERTICES * 32, "array too large")
        try:
            raw = base64.b64decode(value['b64'], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValidationError("invalid base64") from exc
        fmt, size = {'f32': ('f', 4), 'u16': ('H', 2), 'u32': ('I', 4)}[dt]
        require(len(raw) % size == 0, "misaligned typed array")
        values = [x[0] for x in struct.iter_unpack('<' + fmt, raw)]
        require(all(math.isfinite(x) and abs(x) <= 1e9 for x in values), "nonfinite or excessive typed number")
    require(count is None or len(values) == count, "typed array length mismatch")
    return values

def _ids(items, label):
    require(isinstance(items, list) and len(items) <= 4096, f"invalid {label}")
    ids = set()
    for obj in items:
        require(isinstance(obj, dict), f"invalid {label} entry")
        ident = obj.get('id')
        require(isinstance(ident, str) and 0 < len(ident) <= 160 and ident not in BAD_KEYS and ident not in ids, f"duplicate/invalid {label} id")
        ids.add(ident)
    return ids

def validate_model(model: dict) -> dict:
    require(isinstance(model, dict), 'Model must be object')
    require(set(model) <= {'version', 'name', 'canvas', 'atlases', 'rig', 'drawables', 'chains', 'strands', 'expressions', 'views', 'playbackSettings'}, 'Unsupported model field')
    if 'playbackSettings' in model:
        validate_playback_settings(model['playbackSettings'])
    require(isinstance(model, dict) and type(model.get('version')) is int and model['version'] == 1, "unknown model version")
    canvas = model.get('canvas', {})
    require(isinstance(canvas, dict), "invalid canvas")
    for key in ('width', 'height'):
        require(type(canvas.get(key)) in (int, float) and 0 < canvas[key] <= 16384, "invalid canvas dimensions")
    atlases = model.get('atlases')
    require(isinstance(atlases, list) and 0 < len(atlases) <= 32, "invalid atlases")
    names = set()
    for a in atlases:
        require(isinstance(a, dict), "invalid atlas")
        name = safe_name(a.get('file'))
        require(Path(name).suffix.lower() in ('.png', '.webp') and name.casefold() not in names and name not in ('preview.png',), "duplicate or invalid atlas")
        names.add(name.casefold())
        for key in ('width', 'height'):
            require(type(a.get(key)) is int and 0 < a[key] <= 8192, "invalid atlas dimensions")
    drawables = model.get('drawables')
    dids = _ids(drawables, 'drawables')
    require(bool(drawables), "empty model")
    strands = model.get('strands', [])
    sids = _ids(strands, 'strands')
    chains = model.get('chains', [])
    cids = _ids(chains, 'chains')
    expressions = model.get('expressions', [])
    _ids(expressions, 'expressions')
    total_vertices = 0
    for d in drawables:
        n = d.get('vertexCount')
        require(type(n) is int and 3 <= n <= MAX_VERTICES, "invalid vertex count")
        total_vertices += n
        require(total_vertices <= 1_000_000, "model vertex budget")
        require(type(d.get('atlas')) is int and 0 <= d['atlas'] < len(atlases), "atlas index out of range")
        require(type(d.get('order')) in (int, float), "missing drawable order")
        typed_array(d.get('positions'), 'f32', n * 2)
        uv = typed_array(d.get('uvs'), 'f32', n * 2)
        require(all(-1e-5 <= x <= 1.00001 for x in uv), "UV outside atlas")
        ix = typed_array(d.get('indices'), 'index')
        require(len(ix) > 0 and len(ix) % 3 == 0 and all(type(x) is int and 0 <= x < n for x in ix), "invalid triangle index")
        attrs = d.get('attrs', {})
        require(isinstance(attrs, dict), "invalid attrs")
        sub = d.get('sub', [])
        require(isinstance(sub, list) and len(sub) <= 256, 'invalid sub-piece bindings')
        _ids(sub, 'sub-piece')
        strand_refs = d.get('strandIds', [])
        require(isinstance(strand_refs, list) and len(strand_refs) <= 4096
                and all(isinstance(x, str) and x in sids for x in strand_refs)
                and len(set(strand_refs)) == len(strand_refs), 'unknown/duplicate strand reference')
        if sub:
            require('sw' in attrs, 'missing sub-piece weights')
        if strand_refs:
            require('stw' in attrs and 'sts' in attrs, 'missing strand weights')
        for key, a in attrs.items():
            # Canonical deform.js indexes these as a per-vertex matrix, not a scalar.
            columns = len(sub) if key == 'sw' else len(strand_refs) if key in ('stw', 'sts') else 1
            require(n * columns <= MAX_VERTICES * 6, 'attribute matrix budget')
            typed_array(a, 'f32', n * columns)
        require(isinstance(d.get('keyforms', {}), dict), 'Invalid keyform map')
        require(isinstance(d.get('deformers', []), list), 'Invalid deformers')
        for a in d.get('keyforms', {}).values():
            typed_array(a, 'f32', n * 2)
        for df in d.get('deformers', []):
            require(isinstance(df, dict) and df.get('type') in ('blink', 'iris', 'brow', 'mouthOpen', 'mouthForm', 'chain'), "unsupported deformer")
            if df.get('type') == 'chain':
                require(df.get('chain') in cids, 'unknown deformer chain')
            for k in ('t', 'w'):
                if k in df:
                    typed_array(df[k], 'f32', n)
        clip = d.get('clip', [])
        if isinstance(clip, str):
            clip = [clip]
        require(isinstance(clip, list) and all(x in dids and x != d['id'] for x in clip), "unknown clipping mask")
        require(isinstance(d.get('strandIds', []), list) and all(x in sids for x in d.get('strandIds', [])), "unknown strand reference")
        for k in ('chainIds',):
            require(all(x in cids for x in d.get(k, [])), "unknown chain reference")
    clip_graph = {d['id']: ([d['clip']] if isinstance(d.get('clip'), str) else d.get('clip', [])) for d in drawables}
    pending = set(dids)
    while pending:
        removable = {ident for ident in pending if not any(x in pending for x in clip_graph[ident])}
        require(removable, 'Cyclic clipping references')
        pending -= removable
    for d in drawables:
        opacity = d.get('opacity', [])
        require(isinstance(opacity, list) or type(opacity) in (int, float), 'Invalid opacity')
        if isinstance(opacity, list):
            for op in opacity:
                require(isinstance(op, dict), 'Invalid opacity binding')
                if 'param' in op:
                    require(isinstance(op['param'], str) and op['param'] not in BAD_KEYS and isinstance(op.get('keys'), list) and len(op['keys']) >= 2, 'Invalid opacity parameter')
                    require(all(isinstance(k, list) and len(k) == 2 and all(type(x) in (int, float) for x in k) for k in op['keys']), 'Invalid opacity keyframes')
                else:
                    require(isinstance(op.get('oneMinusSum'), list) and all(isinstance(x, str) and x not in BAD_KEYS for x in op['oneMinusSum']), 'Invalid opacity sum')
    for s in strands:
        require(isinstance(s.get('pts'), list) and 2 <= len(s['pts']) <= 4096, "invalid strand points")
        require(all(isinstance(p, list) and len(p) == 2 and all(type(x) in (int, float) for x in p) for p in s['pts']), "invalid strand point")
        if 'layer' in s:
            require(s['layer'] in dids, "unknown strand layer")
    for c in chains:
        for a in c.get('attrs', {}).values():
            typed_array(a, 'f32')
        for a in c.get('keyforms', {}).values():
            typed_array(a, 'f32')
    # Derive support from actual bindings; settings are applied before calling this.
    rig = model.get('rig', {})
    require(isinstance(rig, dict), "invalid rig")
    require(all(isinstance(rig.get(k, {}), dict) for k in ('head', 'body', 'eyes', 'brows')), 'Invalid rig block')
    require(isinstance(model.get('views', {}), dict), 'Invalid views')
    for expr in expressions:
        require(isinstance(expr.get('params', {}), dict) and all(type(x) in (int, float) for x in expr.get('params', {}).values()), 'Invalid expression parameters')
    bound = set()
    def bindings(o):
        if isinstance(o, dict):
            if isinstance(o.get('param'), str):
                bound.add(o['param'])
            for x in o.values():
                bindings(x)
        elif isinstance(o, list):
            for x in o:
                bindings(x)
    bindings(drawables)
    bound_strands = {ident for d in drawables if 'stw' in d.get('attrs', {}) and any(x > 0 for x in typed_array(d['attrs']['stw'], 'f32')) for ident in d.get('strandIds', [])}
    bound_chains = {df['chain'] for d in drawables for df in d.get('deformers', []) if df.get('type') == 'chain' and 'w' in df and any(x > 0 for x in typed_array(df['w'], 'f32'))}
    physics = any(s['id'] in bound_strands and not s.get('physics', {}).get('static') for s in strands) or bool(cids & bound_chains)
    return {'expressions': [e['id'] for e in expressions], 'views': list(model.get('views', {})),
            'blink': bool({'EyeLOpen', 'EyeROpen'} & bound), 'mouth': 'MouthOpenY' in bound,
            'gaze': any(df.get('type') == 'iris' for d in drawables for df in d.get('deformers', [])),
            'physics': physics, 'headYaw': False}

def apply_settings(model, settings):
    """Pure data copy with canonical settings merge for capability derivation."""
    import copy
    model = copy.deepcopy(model)
    require(isinstance(settings, dict), "settings must be object")
    if 'playbackSettings' in settings:
        validate_playback_settings(settings['playbackSettings'])
    def merge(dst, src):
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                merge(dst[k], v)
            else:
                dst[k] = copy.deepcopy(v)
    if 'rig' in settings:
        require(isinstance(settings['rig'], dict), "invalid settings rig")
        merge(model.setdefault('rig', {}), settings['rig'])
    if 'chains' in settings:
        require(isinstance(settings['chains'], dict), "invalid settings chains")
        for c in model.get('chains', []):
            if c['id'] in settings['chains']:
                require(isinstance(settings['chains'][c['id']], dict), "invalid chain settings")
                merge(c, settings['chains'][c['id']])
    if 'expressions' in settings:
        model['expressions'] = copy.deepcopy(settings['expressions'])
    if 'playbackSettings' in settings:
        merge(model.setdefault('playbackSettings', {'version': 1}), settings['playbackSettings'])
    return model

def image_dimensions(raw, name):
    """Validate bounded PNG chunks/CRC or RIFF frame header; no image allocations."""
    if name.lower().endswith('.png'):
        require(raw.startswith(b'\x89PNG\r\n\x1a\n'), "invalid PNG signature")
        pos, dims, end, data = 8, None, False, False
        decoder, expanded, expected, idat_ended = zlib.decompressobj(), 0, None, False
        while pos < len(raw):
            require(pos + 12 <= len(raw), "truncated PNG")
            n = int.from_bytes(raw[pos:pos+4], 'big')
            tag = raw[pos+4:pos+8]
            require(n <= MAX_FILE and pos + 12 + n <= len(raw), "invalid PNG chunk")
            chunk = raw[pos+8:pos+8+n]
            require(binascii.crc32(tag + chunk) & 0xffffffff == int.from_bytes(raw[pos+8+n:pos+12+n], 'big'), "PNG CRC mismatch")
            if tag == b'IHDR':
                require(dims is None and pos == 8 and n == 13, "invalid PNG header")
                dims = struct.unpack('>II', chunk[:8])
                require(all(0 < x <= 8192 for x in dims) and dims[0] * dims[1] <= 64_000_000, 'PNG dimensions limit')
                depth, color, compression, filtering, interlace = chunk[8:]
                channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color)
                require(channels and depth in (1, 2, 4, 8, 16) and compression == 0 and filtering == 0 and interlace == 0, 'Unsupported PNG encoding')
                require(depth == 8 or depth == 16 or color in (0, 3), 'Invalid PNG bit depth')
                expected = (((dims[0] * channels * depth + 7) // 8) + 1) * dims[1]
                require(expected <= MAX_TOTAL, 'PNG expansion limit')
            if tag == b'IDAT':
                require(dims is not None and not idat_ended, 'Invalid PNG data order')
                data = True
                try:
                    expanded += len(decoder.decompress(chunk, expected - expanded + 1))
                except zlib.error as exc:
                    raise ValidationError('Invalid PNG compressed data') from exc
                require(expanded <= expected and not decoder.unconsumed_tail and not decoder.unused_data, 'PNG expansion/trailing data limit')
            elif data:
                idat_ended = True
            if tag == b'IEND':
                require(n == 0 and pos+12 == len(raw), "trailing PNG data")
                end = True
            pos += n + 12
        require(dims and end and data and decoder.eof and expanded == expected, "incomplete PNG")
    else:
        require(raw[:4] == b'RIFF' and raw[8:12] == b'WEBP' and len(raw) >= 30 and int.from_bytes(raw[4:8], 'little') + 8 == len(raw), "invalid WebP")
        offset, frames = 12, 0
        while offset < len(raw):
            require(offset + 8 <= len(raw), 'Truncated WebP chunk')
            size = int.from_bytes(raw[offset+4:offset+8], 'little')
            require(offset + 8 + size <= len(raw), 'Invalid WebP chunk size')
            if raw[offset:offset+4] in (b'VP8 ', b'VP8L'):
                frames += 1
            require(raw[offset:offset+4] not in (b'ANIM', b'ANMF'), 'Animated WebP unsupported')
            offset += 8 + size + (size % 2)
        require(offset == len(raw) and frames == 1, 'Incomplete WebP frame')
        kind = raw[12:16]
        if kind == b'VP8X':
            require(len(raw) >= 30 and not raw[20] & 2, "animated WebP unsupported")
            dims = (int.from_bytes(raw[24:27], 'little') + 1, int.from_bytes(raw[27:30], 'little') + 1)
        elif kind == b'VP8L':
            require(raw[20] == 0x2f, "invalid lossless WebP")
            bits = int.from_bytes(raw[21:25], 'little')
            dims = ((bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1)
        else:
            require(kind == b'VP8 ' and raw[23:26] == b'\x9d\x01\x2a', "invalid WebP frame")
            dims = (int.from_bytes(raw[26:28], 'little') & 0x3fff, int.from_bytes(raw[28:30], 'little') & 0x3fff)
    require(all(0 < n <= 8192 for n in dims) and dims[0] * dims[1] <= 64_000_000, "image dimensions limit")
    return dims

def validate_package(package: Path | str) -> dict:
    """Return manifest and recomputed capabilities or raise ValidationError."""
    try:
        require(Path(package).stat().st_size <= MAX_TOTAL, "archive too large")
        with zipfile.ZipFile(package) as z:
            infos = z.infolist()
            require(3 <= len(infos) <= MAX_FILES, "archive member count")
            names, total = set(), 0
            for i in infos:
                safe_name(i.filename)
                require(i.filename.casefold() not in names, "duplicate archive member")
                names.add(i.filename.casefold())
                require(not i.is_dir() and not i.flag_bits & 1 and i.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED), "unsupported archive member")
                mode = i.external_attr >> 16
                require(stat.S_IFMT(mode) in (0, stat.S_IFREG) and not (i.external_attr & 0x400), "symlink/reparse archive member")
                require(i.file_size <= MAX_FILE and i.file_size <= max(1, i.compress_size) * 200, "archive size/compression limit")
                total += i.file_size
                require(total <= MAX_TOTAL, "archive expanded size limit")
            require('manifest.json' in z.namelist(), "missing manifest")
            manifest = json_bytes(z.read('manifest.json'))
            require(isinstance(manifest, dict), "invalid manifest")
            require(type(manifest.get('schema_version')) is int and manifest['schema_version'] == 1 and manifest.get('model_format') == 'seethrough-rig' and type(manifest.get('model_format_version')) is int and manifest['model_format_version'] == 1, "unknown package format")
            runtime = manifest.get('runtime', {})
            require(isinstance(runtime, dict) and type(runtime.get('abi')) is int and runtime['abi'] == 1 and runtime.get('version') == '0.1.0', "unsupported runtime ABI/version")
            ref = runtime.get('core_ref', manifest.get('provenance', {}).get('core_ref') if isinstance(manifest.get('provenance'), dict) else None)
            require(ref in (None, 'canonical', '46c0037779fb5e59715030925d66abdc6c09ca12'), 'unsupported runtime core_ref')
            if 'core_ref' in runtime and isinstance(manifest.get('provenance'), dict) and 'core_ref' in manifest['provenance']:
                require(runtime['core_ref'] == manifest['provenance']['core_ref'], 'conflicting runtime core refs')
            require(manifest.get('model') == 'model.json' and manifest.get('preview') == 'preview.png' and manifest.get('settings') in (None, 'settings.json'), "invalid manifest resources")
            for k in ('project_id', 'revision', 'name'):
                require(isinstance(manifest.get(k), str) and 0 < len(manifest[k]) <= 200, "invalid manifest identity")
            require(bool(HEX64.fullmatch(str(manifest.get('source_sha256', '')))), "invalid source hash")
            require(isinstance(manifest.get('provenance'), dict) and bool(COMMIT.fullmatch(str(manifest['provenance'].get('commit', '')))), "invalid source commit")
            profile = manifest.get('profile', {})
            check_profile(profile, ref)
            if 'playbackSettings' in manifest:
                validate_playback_settings(manifest['playbackSettings'], complete=True)
            rows = manifest.get('files')
            require(isinstance(rows, list), "missing inventory")
            inventory, payloads = {}, {}
            for row in rows:
                require(isinstance(row, dict), "invalid inventory row")
                name = safe_name(row.get('path'))
                require(name != 'manifest.json' and name not in inventory and type(row.get('size')) is int and row['size'] >= 0 and bool(HEX64.fullmatch(str(row.get('sha256', '')))), "invalid inventory")
                inventory[name] = row
            require(set(inventory) == set(z.namelist()) - {'manifest.json'}, "unlisted/missing resource")
            for name, row in inventory.items():
                raw = z.read(name)
                require(len(raw) == row['size'] and hashlib.sha256(raw).hexdigest() == row['sha256'].lower(), "resource hash/size mismatch")
                payloads[name] = raw
            require('model.json' in payloads and 'preview.png' in payloads, "missing model/preview")
            model = json_bytes(payloads['model.json'])
            caps = validate_model(model)
            needed = {'model.json', 'preview.png'} | {a['file'] for a in model['atlases']}
            settings = {}
            if manifest.get('settings'):
                require('settings.json' in payloads, "missing declared settings")
                settings = json_bytes(payloads['settings.json'])
                caps = validate_model(apply_settings(model, settings))
                needed.add('settings.json')
            resolve_playback_settings(profile_playback_settings(profile), model.get('playbackSettings'),
                                      settings.get('playbackSettings'), manifest.get('playbackSettings'))
            for name in payloads:
                if name not in needed:
                    require(name.startswith('motion_') and name.endswith('.json'), "unexpected data resource")
                    motion = json_bytes(payloads[name])
                    require(isinstance(motion, dict), "invalid motion data")
            require(needed <= set(payloads), "missing atlas")
            for a in model['atlases']:
                require(image_dimensions(payloads[a['file']], a['file']) == (a['width'], a['height']), "atlas dimension mismatch")
            image_dimensions(payloads['preview.png'], 'preview.png')
            require(isinstance(manifest.get('capabilities'), dict), "invalid capabilities")
            return {'manifest': manifest, 'capabilities': caps, 'files': list(inventory), 'valid': True}
    except ValidationError:
        raise
    except (OSError, zipfile.BadZipFile, KeyError, TypeError, ValueError, RuntimeError, AttributeError, OverflowError, RecursionError, struct.error) as exc:
        raise ValidationError('malformed package: ' + str(exc)) from exc
