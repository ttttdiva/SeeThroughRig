"""Unit tests for the build pipeline (no GPU / See-through needed)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from seethrough_rig.build import Atlas, AttrModel, chain_bindings, composite, eye_curves, opacity_term, smoothstep
from seethrough_rig.decompose import head_crop_box
from seethrough_rig.layers import Layer, full_pass_transform, head_pass_transform, resample
from seethrough_rig.mesh import grid_mesh


def disc_layer(r=20, density=1.0, origin=(10.0, 20.0)):
    n = int(2 * r * density) + 4
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float32)
    c = n / 2
    a = ((xx - c) ** 2 + (yy - c) ** 2 < (r * density) ** 2).astype(np.float32)
    rgba = np.zeros((n, n, 4), np.float32)
    rgba[..., 0] = 1
    rgba[..., 3] = a
    return Layer("disc", rgba, origin, density)


def test_full_pass_transform_maps_padded_square_to_source():
    s, ox, oy = full_pass_transform(1280, (1037, 1516))
    # the square canvas is padded horizontally; frame (0,0) is left of the image
    assert s == pytest.approx(1516 / 1280)
    assert oy == 0
    assert ox == pytest.approx(-(1516 - 1037) / 2)
    # frame centre maps to image centre
    assert 640 * s + ox == pytest.approx(1037 / 2)


def test_head_pass_transform_square_box():
    s, ox, oy = head_pass_transform(1280, [273, 55, 752, 534])
    assert s == pytest.approx(479 / 1280)
    assert (ox, oy) == (273, 55)


def test_head_crop_box_contains_face(tmp_path: Path):
    info = {"frame_size": [1280, 1280], "parts": {
        "face": {"xyxy": [503, 73, 780, 410]},
        "ears-l": {"xyxy": [723, 269, 774, 345]},
        "ears-r": {"xyxy": [490, 269, 538, 346]},
    }}
    (tmp_path / "info.json").write_text(json.dumps(info))
    box = head_crop_box(tmp_path, (1037, 1516))
    assert box[2] - box[0] == box[3] - box[1]          # square
    s, ox, oy = full_pass_transform(1280, (1037, 1516))
    face = [503 * s + ox, 73 * s + oy, 780 * s + ox, 410 * s + oy]
    assert box[0] <= face[0] and box[1] <= face[1] and box[2] >= face[2] and box[3] >= face[3]


def test_grid_mesh_covers_alpha_and_maps_uv():
    L = disc_layer(r=20, density=2.0)
    pos, uv, tris = grid_mesh(L, spacing=4)
    assert len(tris) > 0 and tris.max() < len(pos)
    # positions are uv / density + origin
    np.testing.assert_allclose(pos, uv / 2.0 + np.array(L.origin), atol=1e-4)
    # every opaque pixel centre lies inside some kept cell (bbox check)
    ys, xs = np.nonzero(L.alpha > 0.5)
    assert uv[:, 0].min() <= xs.min() and uv[:, 0].max() >= xs.max()
    assert uv[:, 1].min() <= ys.min() and uv[:, 1].max() >= ys.max()


def test_resample_preserves_source_extent():
    L = disc_layer(r=10, density=1.0)
    R = resample(L, 2.0)
    assert R.size_src == pytest.approx(L.size_src, rel=0.02)
    assert R.bbox_src() == pytest.approx(L.bbox_src(), abs=1.0)


def test_composite_single_layer_places_pixels():
    L = disc_layer(r=5, density=1.0, origin=(30.0, 40.0))
    comp = composite([L], (100, 100))
    assert comp[40 + 7, 30 + 7, 3] == pytest.approx(1.0)
    assert comp[5, 5, 3] == 0


def test_eye_curves_upper_above_lower():
    n = 40
    rgba = np.zeros((20, n, 4), np.float32)
    for x in range(4, n - 4):
        h = int(6 * np.sin(np.pi * (x - 4) / (n - 8))) + 1
        rgba[10 - h:10 + h // 2, x, 3] = 1
    L = Layer("eyewhite-l", rgba, (100.0, 50.0), 1.0)
    xs, up, low = eye_curves(L, samples=12)
    assert np.all(low >= up)
    assert xs[0] >= 100 and xs[-1] <= 100 + n


def test_attr_model_depth_and_weights():
    lm = {"head_center": [100.0, 100.0], "head_radius": [50.0, 60.0, 45.0], "eye_y": 100.0, "chin_y": 160.0,
          "head_top": 30.0, "shoulder_y": 180.0, "waist_y": 300.0, "hip_y": 380.0, "hem_y": 450.0,
          "bottom_y": 600.0, "center_x": 100.0, "neck_pivot": [100.0, 175.0], "body_radius": 60.0,
          "neck_box": [80.0, 150.0, 120.0, 200.0]}
    am = AttrModel(lm, {})
    a = am.attrs(np.array([100.0, 130.0, 149.0]), np.array([100.0, 100.0, 100.0]), "facepart")
    assert a["z"][0] == pytest.approx(45.0)
    assert a["z"][0] > a["z"][1] > a["z"][2]
    # neck: top moves exactly like the chin, bottom stays with the body
    nk = am.attrs(np.array([100.0, 100.0]), np.array([160.0, 200.0]), "neck")
    assert nk["head"][0] == pytest.approx(1) and nk["head"][1] == pytest.approx(0)
    h = am.attrs(np.full(3, 100.0), np.array([50.0, 200.0, 500.0]), "hair_back")["head"]
    assert h[0] == pytest.approx(1) and h[2] == pytest.approx(0) and 0 <= h[1] <= 1


def test_head_profile_has_finite_slope_and_is_monotonic():
    from seethrough_rig.build import head_profile
    r = np.linspace(0, 2, 4001)
    f = head_profile(r)
    d = np.diff(f) / np.diff(r)
    assert f[0] == pytest.approx(1) and np.all(d <= 1e-9) and d.min() > -6


def test_chain_bindings_side_weights():
    chains = [{"id": "c", "points": [[0, 0], [0, 100]], "region": {"x": [50, None], "xsoft": 10}}]
    out = chain_bindings(np.array([0.0, 50.0, 100.0]), np.array([50.0, 50.0, 200.0]), chains, ["c"])[0]
    import base64
    t = np.frombuffer(base64.b64decode(out["t"]["b64"]), np.float32)
    w = np.frombuffer(base64.b64decode(out["w"]["b64"]), np.float32)
    assert t.tolist() == pytest.approx([0.5, 0.5, 1.0])
    assert w[0] == pytest.approx(0) and w[1] == pytest.approx(0.5) and w[2] == pytest.approx(1)


def test_opacity_term_side_substitution():
    assert opacity_term(["Eye{S}Open", [[0, 1], [1, 0]]], "L") == {"param": "EyeLOpen", "keys": [[0, 1], [1, 0]]}
    assert opacity_term({"oneMinusSum": ["A"]}) == {"oneMinusSum": ["A"]}


def test_atlas_packs_without_overlap(tmp_path: Path):
    at = Atlas(size=256, pad=4)
    rects = []
    for w, h in [(100, 50), (100, 50), (60, 120), (200, 30)]:
        img = np.ones((h, w, 4), np.float32)
        page, x, y = at.add(img)
        rects.append((page, x, y, w, h))
    for i, a in enumerate(rects):
        for b in rects[i + 1:]:
            if a[0] != b[0]:
                continue
            overlap = not (a[1] + a[3] <= b[1] or b[1] + b[3] <= a[1] or a[2] + a[4] <= b[2] or b[2] + b[4] <= a[2])
            assert not overlap
    files = at.save(tmp_path, "atlas")
    assert all((tmp_path / f["file"]).exists() for f in files)


def test_smoothstep_bounds():
    assert smoothstep(0, 1, -1) == 0 and smoothstep(0, 1, 2) == 1
    assert smoothstep(0, 1, 0.5) == pytest.approx(0.5)


def test_fitted_keyform_inverts_backward_field(tmp_path):
    from seethrough_rig.keyforms import FittedKeyform, fit_group
    G = 64
    # backward field: output p samples the neutral at p + b; here b = uniform shift + slight scale
    ys, xs = np.mgrid[0:G, 0:G].astype(np.float32)
    nx = (xs + 0.5) / G * 2 - 1
    ny = (ys + 0.5) / G * 2 - 1
    b = np.stack([-0.1 + 0.05 * nx, 0.02 * np.ones_like(ny)], -1).astype(np.float32)
    np.savez(tmp_path / "k.npz", face=b, name="AngleX:30")
    fk = FittedKeyform(tmp_path / "k.npz", [0, 0, 1000, 1000])
    x = np.array([500.0, 600.0]); y = np.array([500.0, 400.0])
    dx, dy = fk.offsets("face", x, y)
    # forward-mapped point o must satisfy o + b(o) = source point (in normalised coords)
    ox = (x + dx) / 1000 * 2 - 1; oy = (y + dy) / 1000 * 2 - 1
    assert np.allclose(ox + (-0.1 + 0.05 * ox), x / 1000 * 2 - 1, atol=2e-3)
    assert np.allclose(oy + 0.02, y / 1000 * 2 - 1, atol=2e-3)
    assert fit_group("eyeL-happy") == "eyeL" and fit_group("mouth-happy") == "face" and fit_group("front hair") == "front" and fit_group("topwear") is None
