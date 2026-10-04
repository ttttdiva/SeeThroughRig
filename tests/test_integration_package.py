"""Adversarial archive/model tests; no inference/download/network required."""
import base64
import copy
import hashlib
import io
import json
import struct
import zipfile
from pathlib import Path
import pytest
from PIL import Image
from seethrough_rig.package_validate import (PROFILE, ValidationError, json_bytes, safe_name, typed_array,
                                           validate_model, validate_package)
from seethrough_rig import production_export as exporter

COMMIT = 'a' * 40
SOURCE = 'b' * 64

def typed(values, dtype='f32'):
    fmt = {'f32': 'f', 'u16': 'H', 'u32': 'I'}[dtype]
    return {'dtype': dtype, 'b64': base64.b64encode(struct.pack('<' + fmt * len(values), *values)).decode()}

def image_bytes(size=(2, 2)):
    b = io.BytesIO()
    Image.new('RGBA', size, (120, 70, 20, 255)).save(b, format='PNG')
    return b.getvalue()

@pytest.fixture
def model():
    return {'version': 1, 'name': '日本語 model', 'canvas': {'width': 2, 'height': 2},
            'atlases': [{'file': 'atlas.png', 'width': 2, 'height': 2}],
            'rig': {'head': {'mode': 'rigid'}, 'body': {'rigid': True}},
            'drawables': [{'id': 'face', 'atlas': 0, 'order': 0, 'vertexCount': 3,
                           'positions': typed([0, 0, 2, 0, 2, 2]), 'uvs': typed([0, 0, 1, 0, 1, 1]),
                           'indices': typed([0, 1, 2], 'u16'), 'attrs': {'head': typed([1, 1, 1])},
                           'deformers': [], 'opacity': [{'param': 'MouthOpenY', 'keys': [[0, 0], [1, 1]]}]}],
            'chains': [], 'strands': [], 'expressions': [{'id': 'neutral', 'params': {}}], 'views': {}}

def package_files(model):
    files = {'model.json': json.dumps(model).encode(), 'atlas.png': image_bytes(), 'preview.png': image_bytes()}
    manifest = {'schema_version': 1, 'model_format': 'seethrough-rig', 'model_format_version': 1,
                'runtime': {'abi': 1, 'version': '0.1.0'}, 'project_id': 'test', 'revision': 'one', 'name': '日本語',
                'source_sha256': SOURCE, 'provenance': {'commit': COMMIT}, 'model': 'model.json',
                'settings': None, 'preview': 'preview.png', 'profile': {'id': 'comparison-v1', **PROFILE},
                'capabilities': {'headYaw': True}}
    return files, manifest

def archive(path, files, manifest, extra=None, compression=zipfile.ZIP_STORED):
    manifest = copy.deepcopy(manifest)
    manifest['files'] = [{'path': k, 'size': len(v), 'sha256': hashlib.sha256(v).hexdigest()} for k, v in files.items()]
    with zipfile.ZipFile(path, 'w', compression=compression) as z:
        z.writestr('manifest.json', json.dumps(manifest))
        for name, raw in files.items():
            z.writestr(name, raw)
        if extra:
            extra(z)
    return path

def test_valid_package_recomputes_capabilities(tmp_path, model):
    files, manifest = package_files(model)
    result = validate_package(archive(tmp_path / 'valid.zip', files, manifest))
    assert result['valid'] and result['capabilities']['mouth']
    assert result['capabilities']['headYaw'] is False  # Forged caller capabilities are not trusted.

@pytest.mark.parametrize('name', ['../evil.png', '/evil.png', 'x/y.png', 'x\\y.png', 'C:evil.png', 'x:stream.png',
                                  'CON.png', 'nul.json', 'COM1.png', 'lpt9.png', 'x.png.', 'x.png ', '.', '..',
                                  'evil.js', 'evil.html', 'script.exe', 'x\x00.png', 'x?.png', 'x|.png', 'e\u0301.png'])
def test_bad_flat_names(name):
    with pytest.raises(ValidationError):
        safe_name(name)

def test_japanese_space_resource_name():
    assert safe_name('アトラス one.png') == 'アトラス one.png'

@pytest.mark.parametrize('raw', [b'{"constructor":{}}', b'{"__proto__":1}', b'{"prototype":1}',
                               b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'{"a":1e100}',
                               b'{"a":"file:///etc/passwd"}', b'{"a":"https://evil.example/a"}',
                               b'{"a":"javascript:alert(1)"}', b'\xff', b'[' * 40 + b'0' + b']' * 40])
def test_malicious_json(raw):
    with pytest.raises(ValidationError):
        json_bytes(raw)

@pytest.mark.parametrize('value', [{'dtype': 'f64', 'b64': 'AAAA'}, {'dtype': 'f32', 'b64': '!!!'},
                                  {'dtype': 'f32', 'b64': 'AQ=='}, {'dtype': 'f32', 'b64': 'AAAA', 'extra': 1},
                                  typed([float('nan')]), typed([float('inf')]), [True, 0], [1e20]])
def test_bad_binary_arrays(value):
    with pytest.raises(ValidationError):
        typed_array(value)

@pytest.mark.parametrize('mutation', [
    lambda m: m.update(version=2), lambda m: m.update(canvas={'width': -1, 'height': 2}),
    lambda m: m['atlases'][0].update(file='https://evil/atlas.png'),
    lambda m: m['atlases'].append(copy.deepcopy(m['atlases'][0])),
    lambda m: m['drawables'].append(copy.deepcopy(m['drawables'][0])),
    lambda m: m['drawables'][0].update(vertexCount=4),
    lambda m: m['drawables'][0].update(uvs=typed([0, 0, 2, 0, 1, 1])),
    lambda m: m['drawables'][0].update(indices=typed([0, 1, 3], 'u16')),
    lambda m: m['drawables'][0].update(indices=typed([0, 1], 'u16')),
    lambda m: m['drawables'][0].update(indices=typed([0, 1, 2], 'f32')),
    lambda m: m['drawables'][0].update(atlas=5), lambda m: m['drawables'][0].update(atlas=True),
    lambda m: m['drawables'][0]['attrs'].update(head=typed([1])),
    lambda m: m['drawables'][0].update(clip=['missing']),
    lambda m: m['drawables'][0].update(clip=['face']),
    lambda m: m['drawables'][0].update(strandIds=['missing']),
    lambda m: m['drawables'][0].update(deformers=[{'type': 'chain', 'chain': 'missing'}]),
    lambda m: m['drawables'][0].update(keyforms={'AngleX:30': typed([0, 1])}),
    lambda m: m['expressions'].append({'id': 'neutral', 'params': {}}),
])
def test_malicious_model(model, mutation):
    mutation(model)
    with pytest.raises((ValidationError, TypeError, ValueError)):
        validate_model(model)

@pytest.mark.parametrize('runtime', [{'abi': 2, 'version': '0.1.0'}, {'abi': True, 'version': '0.1.0'},
                                     {'abi': 1, 'version': '99.0.0'}, {'abi': 1, 'version': '0.1.0', 'core_ref': 'evil'}])
def test_unknown_runtime(tmp_path, model, runtime):
    files, manifest = package_files(model)
    manifest['runtime'] = runtime
    with pytest.raises(ValidationError):
        validate_package(archive(tmp_path / 'bad.zip', files, manifest))

@pytest.mark.parametrize('extra', [
    lambda z: z.writestr('../evil.png', b'bad'), lambda z: z.writestr('ATLAS.PNG', image_bytes()),
    lambda z: z.writestr('atlas.png', image_bytes()), lambda z: z.writestr('script.js', b'alert(1)'),
    lambda z: z.writestr('source.png', image_bytes()), lambda z: z.writestr('unlisted.json', b'{}'),
])
def test_malicious_archive_members(tmp_path, model, extra):
    files, manifest = package_files(model)
    with pytest.raises(ValidationError):
        validate_package(archive(tmp_path / 'bad.zip', files, manifest, extra))

@pytest.mark.parametrize('attributes', [(0o120777 << 16), (0o040755 << 16), (0o100644 << 16) | 0x400])
def test_archive_links_and_reparse(tmp_path, model, attributes):
    files, manifest = package_files(model)
    def add(z):
        info = zipfile.ZipInfo('linked.png')
        info.external_attr = attributes
        z.writestr(info, b'outside')
    with pytest.raises(ValidationError):
        validate_package(archive(tmp_path / 'bad.zip', files, manifest, add))

def test_compression_bomb(tmp_path, model):
    files, manifest = package_files(model)
    files['motion_bomb.json'] = b' ' * 1_000_000
    with pytest.raises(ValidationError):
        validate_package(archive(tmp_path / 'bomb.zip', files, manifest, compression=zipfile.ZIP_DEFLATED))

def test_file_count_limit(tmp_path, model):
    files, manifest = package_files(model)
    files.update({f'motion_{i}.json': b'{}' for i in range(130)})
    with pytest.raises(ValidationError):
        validate_package(archive(tmp_path / 'many.zip', files, manifest))

def test_hash_inventory_and_png_integrity(tmp_path, model):
    files, manifest = package_files(model)
    good = archive(tmp_path / 'good.zip', files, manifest)
    with zipfile.ZipFile(good) as z:
        inventory = json.loads(z.read('manifest.json'))
    inventory['files'][0]['sha256'] = '0' * 64
    bad = tmp_path / 'hash.zip'
    with zipfile.ZipFile(bad, 'w') as z:
        z.writestr('manifest.json', json.dumps(inventory))
        for k, v in files.items():
            z.writestr(k, v)
    with pytest.raises(ValidationError):
        validate_package(bad)
    files['preview.png'] = b'invalid png'
    with pytest.raises(ValidationError):
        validate_package(archive(tmp_path / 'image.zip', files, manifest))

def test_declared_settings_only_and_preserved(tmp_path, model):
    files, manifest = package_files(model)
    raw = '{"rig":{"head":{"mode":"rigid"}},"strands":{"髪":{"windPush":0.14}},"visibility":{"face":false},"extraSafeTuning":[1,2]}\n'.encode()
    files['settings.json'] = raw
    with pytest.raises(ValidationError):
        validate_package(archive(tmp_path / 'undeclared.zip', files, manifest))
    manifest['settings'] = 'settings.json'
    checked = validate_package(archive(tmp_path / 'settings.zip', files, manifest))
    assert checked['manifest']['settings'] == 'settings.json'
    with zipfile.ZipFile(tmp_path / 'settings.zip') as z:
        assert z.read('settings.json') == raw
    files['settings.json'] = b'{"rig":{"head":{"__proto__":{}}}}'
    with pytest.raises(ValidationError):
        validate_package(archive(tmp_path / 'poison.zip', files, manifest))

@pytest.fixture
def export_fixture(tmp_path, model, monkeypatch):
    d = tmp_path / '日本語 model with spaces'
    d.mkdir()
    (d / 'model.json').write_text(json.dumps(model), encoding='utf-8')
    (d / 'atlas.png').write_bytes(image_bytes())
    (d / 'rest_composite.png').write_bytes(image_bytes((3, 3)))
    monkeypatch.setattr(exporter, 'git_output', lambda root, args: (COMMIT + '\n').encode())
    monkeypatch.setattr(exporter, 'runtime_capabilities', lambda *a, **k: {'parameters': ['MouthOpenY'], 'mouth': True, 'headYaw': False})
    return d

def test_export_settings_and_source_regression(export_fixture, tmp_path):
    d = export_fixture
    settings = b'{ "visibility":{"face":false}, "strands":{"x":{"windPush":0.2}}, "rig":{"head":{"mode":"rigid"}} }\n'
    (d / 'settings.json').write_bytes(settings)
    before = {p.name: p.read_bytes() for p in d.iterdir()}
    result = exporter.export_package(d, tmp_path / 'result.zip', 'project', 'rev', SOURCE, COMMIT)
    with zipfile.ZipFile(result['package_path']) as z:
        assert z.read('settings.json') == settings
        assert 'source.png' not in z.namelist() and 'rest_composite.png' not in z.namelist()
    assert {p.name: p.read_bytes() for p in d.iterdir()} == before
    assert result['manifest']['provenance']['preview']['artistic_quality_approved'] is False
    with pytest.raises(Exception, match='replace'):
        exporter.export_package(d, tmp_path / 'result.zip', 'project', 'rev', SOURCE, COMMIT)

def test_explicit_preview_and_no_source_fallback(export_fixture, tmp_path):
    d = export_fixture
    (d / 'rest_composite.png').unlink()
    with pytest.raises(Exception, match='preview'):
        exporter.export_package(d, tmp_path / 'none.zip', 'p', 'r', SOURCE, COMMIT)
    preview = tmp_path / 'preview supplied.png'
    preview.write_bytes(image_bytes((3, 3)))
    result = exporter.export_package(d, tmp_path / 'explicit.zip', 'p', 'r', SOURCE, COMMIT, preview=preview)
    assert result['manifest']['provenance']['preview']['kind'] == 'explicit_model_preview'
    source_hash = hashlib.sha256(preview.read_bytes()).hexdigest()
    with pytest.raises(Exception, match='Original source'):
        exporter.export_package(d, tmp_path / 'source.zip', 'p', 'r', source_hash, COMMIT, preview=preview)

def test_export_refuses_unverified_commit(export_fixture, tmp_path, monkeypatch):
    monkeypatch.setattr(exporter, 'git_output', lambda *a: ('c' * 40).encode())
    with pytest.raises(ValidationError, match='commit mismatch'):
        exporter.export_package(export_fixture, tmp_path / 'bad.zip', 'p', 'r', SOURCE, COMMIT)
    assert not (tmp_path / 'bad.zip').exists()

def test_export_model_symlink_escape(export_fixture, tmp_path):
    outside = tmp_path / 'outside.png'
    outside.write_bytes(image_bytes())
    (export_fixture / 'atlas.png').unlink()
    try:
        (export_fixture / 'atlas.png').symlink_to(outside)
    except OSError:
        pytest.skip('Symlink permission unavailable')
    with pytest.raises(Exception, match='Symlink'):
        exporter.export_package(export_fixture, tmp_path / 'bad.zip', 'p', 'r', SOURCE, COMMIT)


def test_conversation_profile_may_carry_breeze_and_wind(tmp_path, model):
    files, manifest = package_files(model)
    manifest['profile'] = {'id': 'conversation-v1', **PROFILE, 'wind': 0.35, 'windBlow': 0.7}
    assert validate_package(archive(tmp_path / 'wind.zip', files, manifest))['valid']


@pytest.mark.parametrize('profile', [
    {'id': 'comparison-v1', **PROFILE, 'wind': 0.35},             # the approved profile has no breeze
    {'id': 'comparison-v1', **PROFILE, 'windBlow': 0.5},          # ... and no wind
    {'id': 'conversation-v1', **PROFILE, 'windBlow': 1.5},        # out of range
    {'id': 'conversation-v1', **PROFILE, 'wind': True},           # not a number
    {'id': 'conversation-v1', **PROFILE, 'gust': 1},              # unknown key
    {'id': 'conversation-v1', **PROFILE, 'idle': False},          # other fields stay fixed
])
def test_bad_wind_profiles(tmp_path, model, profile):
    files, manifest = package_files(model)
    manifest['profile'] = profile
    with pytest.raises(ValidationError):
        validate_package(archive(tmp_path / 'bad.zip', files, manifest))


def test_wind_needs_current_core(tmp_path, model):
    files, manifest = package_files(model)
    manifest['runtime']['core_ref'] = '46c0037779fb5e59715030925d66abdc6c09ca12'
    manifest['profile'] = {'id': 'conversation-v1', **PROFILE, 'windBlow': 0.7}
    with pytest.raises(ValidationError):
        validate_package(archive(tmp_path / 'old-core.zip', files, manifest))



def test_vowel_key_bindings_survive_export_and_validation(export_fixture, tmp_path):
    """Synthetic package round trip, not an artwork or native rendering approval."""
    from seethrough_rig.face_keys import VOWELS
    d = export_fixture
    model = json.loads((d / 'model.json').read_text())
    template = model['drawables'][0]
    model['drawables'] = []
    for i, vowel in enumerate(VOWELS, 1):
        part = copy.deepcopy(template)
        part['id'] = 'mouth-vowel_' + vowel
        part['opacity'] = [
            {'param':'MouthVowel','keys':[[i-1,0],[i,1],[i+1,0]]},
            {'param':'MouthOpenY','keys':[[0.179,0],[0.18,1]]},
        ]
        model['drawables'].append(part)
    (d / 'model.json').write_text(json.dumps(model))
    result = exporter.export_package(d, tmp_path / 'vowels.zip', 'project', 'vowels', SOURCE, COMMIT)
    assert validate_package(Path(result['package_path']))['valid']
    with zipfile.ZipFile(result['package_path']) as package:
        actual = json.loads(package.read('model.json'))
        assert actual['drawables'] == model['drawables']
        assert package.read('atlas.png') == (d / 'atlas.png').read_bytes()
