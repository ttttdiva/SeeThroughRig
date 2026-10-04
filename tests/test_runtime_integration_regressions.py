"""Historical real-data and standalone-server regressions; no artwork committed."""
from __future__ import annotations

import base64
import copy
import json
import subprocess
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from seethrough_rig.package_validate import ValidationError, validate_model
from seethrough_rig.server import Handler

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "46c0037779fb5e59715030925d66abdc6c09ca12"


@pytest.fixture(scope="module")
def historical_model():
    result = subprocess.run(["git", "-C", str(ROOT), "show", BASELINE + ":web/models/aoi/model.json"], capture_output=True, timeout=20)
    if result.returncode:
        pytest.skip("Historical model is not in this checkout; actual-data regression was not performed")
    return json.loads(result.stdout)


def test_historical_model_with_real_multicolumn_weights_and_static_strands(historical_model):
    capabilities = validate_model(historical_model)
    assert capabilities["mouth"] and capabilities["physics"]
    assert not capabilities["headYaw"]
    assert any(s.get("physics", {}).get("static") for s in historical_model["strands"])
    assert any("stw" in d.get("attrs", {}) for d in historical_model["drawables"])
    assert {"neutral", "happy", "worried"} <= set(capabilities["expressions"])


@pytest.mark.parametrize("attribute", ["sw", "stw", "sts"])
def test_real_attribute_matrices_reject_truncated_column(historical_model, attribute):
    model = copy.deepcopy(historical_model)
    if attribute == "sw":
        # The historical model uses strands; inject a valid two-column sub-piece
        # fixture to exercise the newer canonical sub-piece matrix contract too.
        drawable = model["drawables"][0]
        drawable["sub"] = [{"id": "test-left", "c": [0, 0], "z": 0}, {"id": "test-right", "c": [0, 0], "z": 0}]
        drawable["attrs"]["sw"] = {"dtype": "f32", "b64": base64.b64encode(bytes(drawable["vertexCount"] * 2 * 4)).decode()}
        validate_model(model)
    else:
        drawable = next(d for d in model["drawables"] if attribute in d.get("attrs", {}))
    encoded = drawable["attrs"][attribute]
    encoded["b64"] = base64.b64encode(base64.b64decode(encoded["b64"])[:-4]).decode()
    with pytest.raises(ValidationError, match="length"):
        validate_model(model)


@pytest.fixture
def local_editor_server(tmp_path):
    model = tmp_path / "models" / "test"
    model.mkdir(parents=True)
    (model / "model.json").write_text("{}", encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_address[1]), model
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_missing_optional_settings_does_not_create_404_or_file(local_editor_server):
    base, model = local_editor_server
    with urlopen(base + "/models/test/settings.json", timeout=5) as response:
        assert response.status == 200
        assert response.headers["Content-Type"] == "application/json"
        assert response.read() == b"{}"
    assert not (model / "settings.json").exists()
    with urlopen(Request(base + "/models/test/settings.json", method="HEAD"), timeout=5) as response:
        assert response.status == 200 and response.headers["Content-Length"] == "2"


def test_real_saved_settings_remain_unchanged(local_editor_server):
    base, model = local_editor_server
    settings = b'{"rig":{"head":{"pitchScale":0.5}}}'
    (model / "settings.json").write_bytes(settings)
    with urlopen(base + "/models/test/settings.json", timeout=5) as response:
        assert response.status == 200 and response.read() == settings
    assert (model / "settings.json").read_bytes() == settings


def test_unknown_model_is_not_fabricated_by_settings_fallback(local_editor_server):
    base, _ = local_editor_server
    with pytest.raises(HTTPError) as caught:
        urlopen(base + "/models/unknown/settings.json", timeout=5)
    assert caught.value.code == 404


def test_standalone_playback_settings_save_and_reload(local_editor_server):
    base, model = local_editor_server
    settings = {'playbackSettings': {'version': 1, 'blink': {'holdDuration': .25}},
                'rig': {'head': {'pitchScale': .5}}, 'extraSafeTuning': [1, 2]}
    request = Request(base + '/api/settings?model=models/test', data=json.dumps(settings).encode(),
                      headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=5) as response:
        assert response.status == 200
    with urlopen(base + '/models/test/settings.json', timeout=5) as response:
        assert json.loads(response.read()) == settings
    assert json.loads((model / 'settings.json').read_bytes()) == settings


@pytest.mark.parametrize('settings', [[], {'playbackSettings': {'version': 2}},
    {'playbackSettings': {'version': 1, 'wind': {'wind': True}}},
    {'playbackSettings': {'version': 1, 'blink': {'intervalMin': 20, 'intervalMax': 1}}}])
def test_standalone_bad_playback_save_is_rejected_without_overwriting(local_editor_server, settings):
    base, model = local_editor_server
    original = b'{"rig":{"head":{"pitchScale":0.5}}}'
    (model / 'settings.json').write_bytes(original)
    request = Request(base + '/api/settings?model=models/test', data=json.dumps(settings).encode(),
                      headers={'Content-Type': 'application/json'})
    with pytest.raises(HTTPError) as caught:
        urlopen(request, timeout=5)
    assert caught.value.code == 400
    assert (model / 'settings.json').read_bytes() == original


def test_standalone_frame_clock_cannot_send_negative_initial_delta():
    for name in ("player.js", "editor.js"):
        source = (ROOT / "web/js" / name).read_text(encoding="utf-8")
        assert "Math.max(0, Math.min(0.05, (now - last) / 1000))" in source
        assert "loadStandaloneSettings" in source
        assert "settingsUrl:" not in source
