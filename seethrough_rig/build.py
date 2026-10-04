"""Build a rigged puppet model (model.json + texture atlases) from a recipe.

The recipe (JSON) says which See-through pass each layer is taken from, gives
optional art overrides and expression images, and may override any automatically
estimated landmark. See ``recipes/aoi.json`` for a complete example.
"""
from __future__ import annotations

import base64
import json
import math
import zlib
from pathlib import Path

import cv2
from scipy import ndimage
import numpy as np
from PIL import Image

from . import expressions as ex
from .layers import (Layer, full_pass_transform, head_pass_transform, layer_from_image, load_pass, read_rgba,
                     resample)
from .mesh import grid_mesh
from .keyforms import FittedKeyform, PoseKeyform, fit_group, source_tags

HEAD_TAGS = {"face", "nose", "mouth", "ears-l", "ears-r", "ears", "eyewhite-l", "eyewhite-r", "irides-l", "irides-r",
             "eyelash-l", "eyelash-r", "eyebrow-l", "eyebrow-r"}

# ---------------------------------------------------------------- utilities


def smoothstep(a, b, x):
    t = np.clip((np.asarray(x, np.float64) - a) / (b - a if b != a else 1e-6), 0, 1)
    return t * t * (3 - 2 * t)


def b64(arr: np.ndarray, dtype: str) -> dict:
    np_dtype = {"f32": np.float32, "u16": np.uint16, "u32": np.uint32}[dtype]
    return {"b64": base64.b64encode(np.ascontiguousarray(arr, np_dtype).tobytes()).decode("ascii"), "dtype": dtype}


def composite(layers: list[Layer], size: tuple[int, int], density: float = 1.0) -> np.ndarray:
    """Straight-alpha over-composite into a source-space canvas (for checks)."""
    W, H = int(size[0] * density), int(size[1] * density)
    acc = np.zeros((H, W, 4), np.float32)
    for l in layers:
        L = resample(l, density)
        h, w = L.alpha.shape
        x0 = int(round(L.origin[0] * density))
        y0 = int(round(L.origin[1] * density))
        sx0, sy0 = max(0, -x0), max(0, -y0)
        dx0, dy0 = max(0, x0), max(0, y0)
        cw, ch = min(w - sx0, W - dx0), min(h - sy0, H - dy0)
        if cw <= 0 or ch <= 0:
            continue
        src = L.rgba[sy0:sy0 + ch, sx0:sx0 + cw]
        dst = acc[dy0:dy0 + ch, dx0:dx0 + cw]
        a = src[..., 3:4] * l.meta.get("opacity", 1.0)
        dst[..., :3] = src[..., :3] * a + dst[..., :3] * (1 - a)
        dst[..., 3:4] = a + dst[..., 3:4] * (1 - a)
    return acc


# ---------------------------------------------------------------- restoration


def restore_visible(ordered: list[Layer], source_rgb: np.ndarray, strength: float = 1.0, tags=None,
                    edge_px: float = 2.0, boundary_tags=()):
    """Replace the colour of each layer where it is the visible top layer by the
    original illustration's pixels.

    See-through re-synthesises every layer, which softens line art and shifts
    colours slightly. Where a layer is actually visible in the original we know
    its true colour, so we copy it back and keep the synthesised content only for
    the occluded (inpainted) parts.
    """
    H, W = source_rgb.shape[:2]
    for idx, L in enumerate(ordered):
        if tags is not None and L.tag not in tags:
            continue
        h, w = L.alpha.shape
        ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
        sx = L.origin[0] + (xs + 0.5) / L.density
        sy = L.origin[1] + (ys + 0.5) / L.density
        # coverage of everything drawn above this layer
        cover = np.zeros((h, w), np.float32)
        for up in ordered[idx + 1:]:
            if up.meta.get("no_occlude"):
                continue
            a = up.sample_alpha(sx, sy) * up.meta.get("opacity", 1.0)
            cover = cover + a * (1 - cover)
        # also keep away from the edges of the parts drawn above (e.g. the chin
        # outline above the neck): those pixels belong to the occluder's line art
        er0 = max(1, int(round(edge_px * L.density)))
        cover = cv2.dilate(cover.astype(np.float32), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * er0 + 1, 2 * er0 + 1)))
        vis = L.alpha * (1 - cover)
        wgt = smoothstep(0.55, 0.92, vis) * smoothstep(0.6, 0.95, L.alpha) * strength
        # stay ~2 source px away from the layer's own edge: there the original
        # pixel often belongs to the neighbouring part (e.g. the face outline
        # next to a lock of hair) and would travel with this layer as a dark seam
        er = max(1, int(round(edge_px * L.density)))
        inner = cv2.erode((L.alpha > 0.5).astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * er + 1, 2 * er + 1)))
        wgt = wgt.astype(np.float32) * inner
        if L.tag in boundary_tags:
            # An authored aperture may have a generated outline absent from the
            # source. Restore its RGB all the way through the alpha fringe,
            # while retaining the original mask and occluder protection. RGB
            # restoration must not fade with own alpha (it is applied at draw).
            wgt = (1 - cover) * strength
        nr = L.meta.get("no_restore")
        if nr is not None:
            wgt = wgt * (1 - np.asarray(nr, np.float32))
        if L.tag not in boundary_tags:
            wgt = cv2.GaussianBlur(wgt, (0, 0), 0.8 * max(1.0, L.density))
        src = cv2.remap(source_rgb, (sx - 0.5).astype(np.float32), (sy - 0.5).astype(np.float32), cv2.INTER_CUBIC,
                        borderMode=cv2.BORDER_REPLICATE)
        L.rgba[..., :3] = np.clip(L.rgba[..., :3] * (1 - wgt[..., None]) + np.clip(src, 0, 1) * wgt[..., None], 0, 1)
        L.meta["restored"] = float((wgt > 0.5).mean())


# ---------------------------------------------------------------- landmarks


def alpha_bbox(L: Layer, thr=0.3):
    return L.bbox_src(thr)


def eye_curves(white: Layer, samples: int = 24):
    """Upper and lower boundary of the eye opening, sampled along x (source px)."""
    a = white.alpha > 0.35
    cols = np.nonzero(a.any(axis=0))[0]
    x0r, x1r = cols.min(), cols.max()
    xs_r = np.linspace(x0r, x1r, samples)
    up, low = [], []
    for xr in xs_r:
        c = a[:, int(round(xr))]
        ys = np.nonzero(c)[0]
        up.append(ys.min())
        low.append(ys.max() + 1)
    d = white.density
    xs = white.origin[0] + xs_r / d
    up = white.origin[1] + np.array(up) / d
    low = white.origin[1] + np.array(low) / d
    # the lid curves should be smooth; also pin the corners closed
    k = np.array([1, 2, 3, 2, 1], np.float64)
    k /= k.sum()
    up = np.convolve(np.pad(up, 2, mode="edge"), k, mode="valid")
    low = np.convolve(np.pad(low, 2, mode="edge"), k, mode="valid")
    mid = (up + low) / 2
    taper = smoothstep(0, 0.12, np.linspace(0, 1, samples)) * smoothstep(0, 0.12, np.linspace(1, 0, samples))
    up = mid + (up - mid) * (0.35 + 0.65 * taper)
    low = mid + (low - mid) * (0.35 + 0.65 * taper)
    return xs, up, low


def estimate_landmarks(L: dict[str, Layer], size) -> dict:
    face = alpha_bbox(L["face"])
    fx0, fy0, fx1, fy1 = face
    cx = (fx0 + fx1) / 2
    fw, fh = fx1 - fx0, fy1 - fy0
    eyes = [alpha_bbox(L[t]) for t in ("eyewhite-l", "eyewhite-r") if t in L]
    eye_y = float(np.mean([(b[1] + b[3]) / 2 for b in eyes])) if eyes else fy0 + fh * 0.6
    ears = [alpha_bbox(L[t]) for t in ("ears-l", "ears-r", "ears") if t in L]
    ear_y = float(np.mean([(b[1] + b[3]) / 2 for b in ears])) if ears else eye_y + fh * 0.1
    chin = fy1
    neck = alpha_bbox(L["neck"]) if "neck" in L else (cx - fw * 0.15, chin - fh * 0.2, cx + fw * 0.15, chin + fh * 0.3)
    top = alpha_bbox(L["topwear"]) if "topwear" in L else (0, neck[3], size[0], size[1])
    fh_box = alpha_bbox(L["front hair"]) if "front hair" in L else face
    shoulder = neck[3] - (neck[3] - neck[1]) * 0.15
    hem = top[3]
    lm = {
        "face_box": list(face),
        "center_x": cx,
        "head_top": min(fh_box[1], fy0),
        "eye_y": eye_y,
        "ear_y": ear_y,
        "chin_y": chin,
        "neck_box": list(neck),
        "shoulder_y": shoulder,
        "waist_y": shoulder + 0.40 * (hem - shoulder),
        "hip_y": shoulder + 0.70 * (hem - shoulder),
        "hem_y": hem,
        "bottom_y": float(size[1]),
        "head_center": [cx, ear_y - 0.08 * fh],
        # the face layer includes the forehead up to the hairline, so its half
        # height is a good vertical radius; the depth radius is a style choice
        "head_radius": [fw / 2 * 1.02, fh / 2 * 1.05, fw / 2 * 0.95],
        "neck_pivot": [cx, neck[3] - (neck[3] - neck[1]) * 0.3],
        "body_radius": fw * 0.55,
    }
    return lm


def head_profile(r, r0: float = 0.8, k_out: float = 2.5, depth_out: float = 0.25):
    """Normalised head depth: sphere inside r0, cubic rim, receding outside."""
    r = np.asarray(r, np.float64)
    f0 = np.sqrt(1 - r0 * r0)
    s0 = -r0 / f0                        # sphere slope at r0
    s1 = -depth_out * k_out              # slope of the outside part at r = 1
    t = np.clip((r - r0) / (1 - r0), 0, 1)
    h = 1 - r0
    # cubic Hermite from (r0, f0, s0) to (1, 0, s1)
    h00, h10, h01, h11 = 2 * t**3 - 3 * t**2 + 1, t**3 - 2 * t**2 + t, -2 * t**3 + 3 * t**2, t**3 - t**2
    rim = h00 * f0 + h10 * h * s0 + h01 * 0.0 + h11 * h * s1
    inside = np.sqrt(np.clip(1 - r * r, 0, 1))
    outside = -depth_out * (1 - np.exp(-k_out * np.maximum(r - 1, 0)))
    return np.where(r < r0, inside, np.where(r <= 1, rim, outside))


# ---------------------------------------------------------------- per-vertex attributes


class AttrModel:
    """Evaluates rig attributes (depth, weights) at source-space points."""

    def __init__(self, lm: dict, cfg: dict):
        self.lm = lm
        self.cfg = cfg

    def head_surface(self, x, y, scale=(1.0, 1.0, 1.0)):
        cx, cy = self.lm["head_center"]
        rx, ry, rz = [r * s for r, s in zip(self.lm["head_radius"], scale)]
        u = (x - cx) / rx
        v = (y - cy) / ry
        # sphere in the middle, finite-slope rim (an exact ellipsoid has an
        # infinite slope at its edge, which folded hair strands there)
        return rz * head_profile(np.sqrt(u * u + v * v))

    def hair_head_weight(self, y):
        lm = self.lm
        a = lm["chin_y"] - 0.05 * (lm["chin_y"] - lm["head_top"])
        b = lm["shoulder_y"] + 0.35 * (lm["waist_y"] - lm["shoulder_y"])
        fade = self.cfg.get("hair_fade")          # e.g. ["chin_y", "hem_y"]: longer, gentler bend
        if fade:
            a, b = lm[fade[0]], lm[fade[1]]
        return 1 - smoothstep(a, b, y)

    def body_attrs(self, x, y, kind):
        lm = self.lm
        cfg = self.cfg
        R = lm["body_radius"]
        u = np.clip((x - lm["center_x"]) / (R * 1.6), -1, 1)
        cyl = np.sqrt(1 - u * u) * R
        zb_off = cfg.get("zb", {}).get(kind, 0.0)
        zb = cyl * cfg.get("zb_scale", {}).get(kind, 0.6) + zb_off * R
        twist = 1 - smoothstep(lm["waist_y"], lm["hem_y"] + 0.2 * (lm["bottom_y"] - lm["hem_y"]), y)
        lean = 1 - smoothstep(lm["hip_y"], lm["bottom_y"], y)
        breath = 1 - smoothstep(lm["shoulder_y"] + 0.25 * (lm["waist_y"] - lm["shoulder_y"]), lm["waist_y"], y)
        if kind == "legs":
            twist = twist * 0.3
            lean = lean * 0.5
            breath = breath * 0
        return zb, twist, lean, breath

    def attrs(self, x, y, kind: str, z_offset: float = 0.0):
        x = np.asarray(x, np.float64)
        y = np.asarray(y, np.float64)
        lm = self.lm
        rz = lm["head_radius"][2]
        n = x.shape
        if kind in ("face", "facepart"):
            z = self.head_surface(x, y) + z_offset * rz
            if kind == "face":
                # the scalp part of the face layer is hidden under the bangs: let it
                # move with the hair shell so it never peeks out when the head turns
                ry = lm["head_radius"][1]
                top = 1 - smoothstep(lm["eye_y"] - 0.55 * ry, lm["eye_y"] - 0.2 * ry, y)
                yy = np.minimum(y, lm["head_center"][1] + 0.35 * ry)
                shell = self.head_surface(x, yy, (1.28, 1.2, 1.12)) + 0.02 * rz
                z = z * (1 - top) + shell * top
            head = np.ones(n)
            zb, tw, le, br = self.body_attrs(np.full(n, lm["center_x"]), np.full(n, lm["neck_pivot"][1]), "head")
        elif kind == "ears":
            z = np.full(n, z_offset * rz)
            head = np.ones(n)
            zb, tw, le, br = self.body_attrs(np.full(n, lm["center_x"]), np.full(n, lm["neck_pivot"][1]), "head")
        elif kind == "hair_front":
            yy = np.minimum(y, lm["head_center"][1] + 0.35 * lm["head_radius"][1])
            z = self.head_surface(x, yy, (1.28, 1.2, 1.12)) + z_offset * rz
            head = self.hair_head_weight(y)
            zb, tw, le, br = self.body_attrs(x, y, "hair_front")
        elif kind == "hair_back":
            z = np.full(n, z_offset * rz)
            head = self.hair_head_weight(y)
            zb, tw, le, br = self.body_attrs(x, y, "hair_back")
        elif kind == "headwear":
            z = np.full(n, z_offset * rz)
            head = self.hair_head_weight(y)
            zb, tw, le, br = self.body_attrs(x, y, "hair_front")
        elif kind == "neck":
            # the top of the neck moves exactly like the chin above it (same depth,
            # full head weight) and blends into the body under the collar, so no
            # gap opens between chin and neck when the head turns or looks up
            z = self.head_surface(x, np.minimum(y, lm["chin_y"])) + z_offset * rz
            nb = lm["neck_box"]
            head = 1 - smoothstep(lm["chin_y"], nb[3], y)
            zb, tw, le, br = self.body_attrs(x, y, "body")
        else:  # body / arms / legs
            z = np.zeros(n)
            head = np.zeros(n)
            zb, tw, le, br = self.body_attrs(x, y, kind)
        return {"z": z, "head": head, "zb": zb, "twist": tw, "lean": le, "breath": br}


# ---------------------------------------------------------------- chains


def chain_bindings(x, y, chains: list[dict], layer_chains: list[str]):
    """Per-vertex (t, w) for each chain listed for this layer."""
    out = []
    for cid in layer_chains:
        c = next(c for c in chains if c["id"] == cid)
        (rx, ry), (bx, by) = c["points"][0], c["points"][-1]
        length = max(1.0, by - ry)
        t = np.clip((y - ry) / length, 0, 1)
        w = np.ones_like(x, dtype=np.float64)
        reg = c.get("region", {})
        if "x" in reg:
            lo, hi = reg["x"]
            if lo is not None:
                w = w * smoothstep(lo - reg.get("xsoft", 20), lo + reg.get("xsoft", 20), x)
            if hi is not None:
                w = w * (1 - smoothstep(hi - reg.get("xsoft", 20), hi + reg.get("xsoft", 20), x))
        if "y" in reg:
            lo, hi = reg["y"]
            w = w * smoothstep(lo, hi, y)
        if "absx" in reg:  # distance from the centre line
            cx, lo, hi = reg["absx"]
            w = w * smoothstep(lo, hi, np.abs(x - cx))
        if "absx_max" in reg:
            cx, lo, hi = reg["absx_max"]
            w = w * (1 - smoothstep(lo, hi, np.abs(x - cx)))
        out.append({"type": "chain", "chain": cid, "t": b64(t, "f32"), "w": b64(w * c.get("weight", 1.0), "f32")})
    return out


def default_chains(lm: dict, L: dict[str, Layer], cfg: dict) -> list[dict]:
    cx = lm["center_x"]
    hcx, hcy = lm["head_center"]
    rx, ry, _ = lm["head_radius"]
    chains = []

    def line(x0, y0, x1, y1, n):
        return [[float(x0 + (x1 - x0) * i / (n - 1)), float(y0 + (y1 - y0) * i / (n - 1))] for i in range(n)]

    if "back hair" in L:
        bb = alpha_bbox(L["back hair"])
        for side, sx in (("L", 1), ("R", -1)):
            x0 = hcx + sx * rx * 0.8
            chains.append({
                "id": f"hairBack{side}", "kind": "hair_back",
                "points": line(x0, lm["eye_y"], x0 + sx * rx * 0.25, bb[3], 8),
                "stiffness": 55, "tipStiffness": 18, "damping": 4.2, "follow": 0.92, "inertia": 1.0, "wind": 38,
                "maxOffset": 90,
                "region": {"x": [cx, None] if side == "L" else [None, cx], "xsoft": rx * 0.35,
                           "y": [lm["eye_y"], lm["chin_y"] + 0.3 * (lm["chin_y"] - lm["eye_y"])]},
            })
    if "front hair" in L:
        fb = alpha_bbox(L["front hair"])
        for side, sx in (("L", 1), ("R", -1)):
            x0 = hcx + sx * rx * 0.95
            chains.append({
                "id": f"sideLock{side}", "kind": "hair_front",
                "points": line(x0, lm["eye_y"] - 0.1 * ry, x0 + sx * rx * 0.05, fb[3], 7),
                "stiffness": 80, "tipStiffness": 26, "damping": 4.6, "follow": 0.9, "inertia": 0.9, "wind": 30,
                "maxOffset": 60,
                "region": {"x": [hcx + rx * 0.62, None] if side == "L" else [None, hcx - rx * 0.62], "xsoft": rx * 0.3,
                           "y": [lm["eye_y"] - 0.2 * ry, lm["chin_y"]]},
            })
        chains.append({
            "id": "bangs", "kind": "hair_front",
            "points": line(hcx, lm["head_top"] + 0.1 * ry, hcx, lm["eye_y"], 5),
            "stiffness": 160, "tipStiffness": 90, "damping": 7, "follow": 0.8, "inertia": 0.6, "wind": 12,
            "maxOffset": 10,
            "weight": 0.8,
            "region": {"absx_max": [hcx, rx * 0.62, rx * 0.8],
                       "y": [lm["head_top"] + 0.35 * (lm["eye_y"] - lm["head_top"]), lm["eye_y"]]},
        })
    if "headwear" in L:
        hb = alpha_bbox(L["headwear"])
        top = hb[1] + 0.12 * (hb[3] - hb[1])
        xm = (hb[0] + hb[2]) / 2
        chains.append({
            "id": "ribbon", "kind": "headwear",
            "points": line(xm, top, xm - 0.1 * (hb[2] - hb[0]), hb[3], 7),
            "stiffness": 70, "tipStiffness": 26, "damping": 4.0, "follow": 0.9, "inertia": 1.0, "wind": 40,
            "maxOffset": 70,
            "region": {"y": [top, top + 0.25 * (hb[3] - top)]},
        })
    if "topwear" in L:
        tb = alpha_bbox(L["topwear"])
        for side, sx in (("L", 1), ("R", -1)):
            x0 = cx + sx * (tb[2] - tb[0]) * 0.22
            chains.append({
                "id": f"skirt{side}", "kind": "body",
                "points": line(x0, lm["waist_y"], x0 + sx * 20, lm["hem_y"], 5),
                "stiffness": 90, "tipStiffness": 40, "damping": 5.5, "follow": 0.85, "inertia": 0.8, "wind": 14,
                "maxOffset": 30,
                "weight": 0.9,
                "region": {"x": [cx, None] if side == "L" else [None, cx], "xsoft": 60,
                           "y": [lm["waist_y"] + 0.3 * (lm["hem_y"] - lm["waist_y"]), lm["hem_y"]]},
            })
    return chains


# ---------------------------------------------------------------- atlas


class Atlas:
    def __init__(self, size=4096, pad=12):
        self.size = size
        self.pad = pad
        self.pages: list[np.ndarray] = []
        self.cursor = []   # per page: (x, y, row_h)

    def _new_page(self):
        self.pages.append(np.zeros((self.size, self.size, 4), np.float32))
        self.cursor.append([self.pad, self.pad, 0])

    def add(self, rgba: np.ndarray):
        h, w = rgba.shape[:2]
        if w + 2 * self.pad > self.size or h + 2 * self.pad > self.size:
            raise ValueError(f"layer {w}x{h} does not fit in a {self.size} atlas")
        if not self.pages:
            self._new_page()
        for pi in range(len(self.pages) + 1):
            if pi == len(self.pages):
                self._new_page()
            x, y, rh = self.cursor[pi]
            if x + w + self.pad > self.size:
                x, y, rh = self.pad, y + rh + self.pad, 0
            if y + h + self.pad > self.size:
                continue
            self.pages[pi][y:y + h, x:x + w] = rgba
            # bleed colour into the padding to avoid dark fringes under mipmapping
            self.cursor[pi] = [x + w + self.pad, y, max(rh, h)]
            return pi, x, y
        raise RuntimeError("atlas packing failed")

    def save(self, out_dir: Path, name: str) -> list[dict]:
        res = []
        for i, page in enumerate(self.pages):
            used_h = self.cursor[i][1] + self.cursor[i][2] + self.pad
            # UVs are computed against the full page size, so pages are never cropped
            H = self.size
            img = page[:H]
            rgb = img[..., :3]
            a = img[..., 3:4]
            # dilate colours under transparent pixels (edge padding)
            filled = rgb.copy()
            known = (a[..., 0] > 0.01).astype(np.uint8)
            for _ in range(self.pad):
                dil = cv2.dilate(filled * known[..., None], np.ones((3, 3), np.uint8))
                cnt = cv2.dilate(known, np.ones((3, 3), np.uint8))
                blur = cv2.blur(filled * known[..., None], (3, 3))
                kb = cv2.blur(known.astype(np.float32), (3, 3))
                upd = (known == 0) & (cnt > 0)
                filled[upd] = (blur[upd] / np.maximum(kb[upd][:, None], 1e-6))
                known = np.maximum(known, cnt)
            out = np.concatenate([filled, a], axis=2)
            fn = f"{name}_{i}.webp"
            Image.fromarray((np.clip(out, 0, 1) * 255 + 0.5).astype(np.uint8), "RGBA").save(
                out_dir / fn, quality=92, alpha_quality=100, method=6)
            res.append({"file": fn, "width": self.size, "height": H})
        return res


# ---------------------------------------------------------------- main builder


def _merge(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def load_recipe(path: Path) -> dict:
    """Load a recipe; ``"extends": "other.json"`` deep-merges onto a base recipe
    (paths stay relative to the recipes directory)."""
    path = Path(path)
    r = json.loads(path.read_text(encoding="utf-8"))
    if "extends" in r:
        base = load_recipe(path.parent / r.pop("extends"))
        r = _merge(base, r)
    return r


def load_layers(recipe: dict, base_dir: Path) -> tuple[dict[str, Layer], dict, np.ndarray]:
    work = base_dir / recipe["work"]
    meta = json.loads((work / "decompose.json").read_text(encoding="utf-8"))
    size = tuple(meta["source_size"])
    source_rgb = np.asarray(Image.open(work / "source.png").convert("RGB"), np.float32) / 255.0
    passes: dict[str, dict[str, Layer]] = {}
    head_density = recipe.get("head_density", 2.0)
    for name, p in meta["passes"].items():
        d = Path(p["dir"])
        info = json.loads((d / "info.json").read_text(encoding="utf-8"))
        frame = info["frame_size"][0]
        if p["kind"] == "full":
            tf = full_pass_transform(frame, size)
            dens = 1.0
        else:
            tf = head_pass_transform(frame, p["box"])
            dens = head_density
        passes[name] = load_pass(d, tf, dens, (0, 0, size[0], size[1]))
    layers: dict[str, Layer] = {}
    for tag, spec in recipe["layers"].items():
        if "image" in spec:
            p = (base_dir / spec["image"]) if not Path(spec["image"]).is_absolute() else Path(spec["image"])
            L = layer_from_image(tag, p, tuple(spec.get("origin", (0, 0))), spec.get("density", 1.0))
        else:
            src_tag = spec.get("tag", tag)
            ps = passes.get(spec["pass"], {})
            if src_tag not in ps:
                print(f"  ! layer '{tag}' missing in pass '{spec['pass']}', skipped")
                continue
            L = ps[src_tag]
            L = Layer(tag, L.rgba.copy(), L.origin, L.density, L.depth, L.depth_median, dict(L.meta))
        if "density" in spec and "image" not in spec:
            L = resample(L, spec["density"])
        if "clip_rect" in spec:
            x0, y0, x1, y1 = spec["clip_rect"]
            h, w = L.alpha.shape
            ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
            sx = L.origin[0] + (xs + 0.5) / L.density
            sy = L.origin[1] + (ys + 0.5) / L.density
            soft = spec.get("clip_soft", 8)
            m = smoothstep(x0 - soft, x0 + soft, sx) * (1 - smoothstep(x1 - soft, x1 + soft, sx)) \
                * smoothstep(y0 - soft, y0 + soft, sy) * (1 - smoothstep(y1 - soft, y1 + soft, sy))
            L.rgba[..., 3] *= m.astype(np.float32)
        layers[tag] = L.crop_to_content()
    return layers, meta, source_rgb


def inpaint_under(L: Layer, occluders: list[Layer], grow: float = 6, shrink: float = 3):
    """Repaint the part of ``L`` hidden under ``occluders`` from its surroundings.

    See-through sometimes hallucinates outlines of the occluding part (e.g. a
    face edge inside the back hair). Those become visible once the occluder
    moves, so we replace them with colours propagated from the visible hair.
    The border ``shrink`` px inside the occluder edge is repainted too.
    """
    h, w = L.alpha.shape
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    sx = L.origin[0] + (xs + 0.5) / L.density
    sy = L.origin[1] + (ys + 0.5) / L.density
    occ = np.zeros((h, w), np.float32)
    for o in occluders:
        occ = np.maximum(occ, o.sample_alpha(sx, sy))
    m = (occ > 0.5).astype(np.uint8)
    g = max(1, int(grow * L.density))
    m = cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * g + 1, 2 * g + 1)))
    m &= (L.alpha > 0.05).astype(np.uint8)
    if m.sum() == 0:
        return
    rgb8 = (np.clip(L.rgba[..., :3], 0, 1) * 255).astype(np.uint8)
    # inpaint at reduced resolution for speed on large masks, then upsample
    f = 0.5 if m.sum() > 40000 else 1.0
    small = cv2.resize(rgb8, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
    # transparent pixels carry no colour: never use them as inpainting sources
    src_mask = np.maximum(m, (L.alpha < 0.6).astype(np.uint8))
    ms = cv2.resize(src_mask, (small.shape[1], small.shape[0]), interpolation=cv2.INTER_NEAREST)
    filled = cv2.inpaint(small, ms, 5, cv2.INPAINT_TELEA)
    filled = cv2.resize(filled, (w, h), interpolation=cv2.INTER_LINEAR).astype(np.float32) / 255
    wgt = cv2.GaussianBlur(m.astype(np.float32), (0, 0), 1.5)[..., None]
    L.rgba[..., :3] = L.rgba[..., :3] * (1 - wgt) + filled * wgt


def split_front_back(L: Layer, occluder: Layer, source_rgb: np.ndarray, tol=0.15, grow=4):
    """Split an accessory into the part drawn in front of ``occluder`` and the rest.

    A pixel is 'in front' where the occluder is present but the original shows
    the accessory's colour.
    """
    h, w = L.alpha.shape
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    sx = L.origin[0] + (xs + 0.5) / L.density
    sy = L.origin[1] + (ys + 0.5) / L.density
    occ = occluder.sample_alpha(sx, sy)
    mx, my = (sx - 0.5).astype(np.float32), (sy - 0.5).astype(np.float32)
    src = cv2.remap(source_rgb, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    # occluder colour at the same spots (in occluder raster space)
    ox = ((sx - occluder.origin[0]) * occluder.density - 0.5).astype(np.float32)
    oy = ((sy - occluder.origin[1]) * occluder.density - 0.5).astype(np.float32)
    occ_rgb = cv2.remap(np.ascontiguousarray(occluder.rgba[..., :3]), ox, oy, cv2.INTER_LINEAR,
                        borderMode=cv2.BORDER_REPLICATE)
    d_acc = np.abs(src - L.rgba[..., :3]).max(axis=2)
    d_occ = np.abs(src - occ_rgb).max(axis=2)
    # in front only where the original clearly shows the accessory, not the occluder
    match = (d_acc < tol) & (d_occ > d_acc + 0.06) & (L.alpha > 0.6) & (occ > 0.5)
    m = cv2.morphologyEx(match.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    # keep sizeable components only
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    keep = np.zeros_like(m)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] > 200 * L.density ** 2:
            keep[lab == i] = 1
    keep = cv2.dilate(keep, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * grow + 1, 2 * grow + 1)))
    keep = cv2.GaussianBlur(keep.astype(np.float32), (0, 0), 1.5)
    front = Layer(L.tag + "-front", L.rgba.copy(), L.origin, L.density, None, L.depth_median, dict(L.meta))
    front.rgba[..., 3] *= keep
    return front.crop_to_content()


def dump_fit_layers(ordered: list[Layer], box, frame: int, path: Path):
    """Every layer rendered onto the keyform frame (box -> frame x frame), uint8 RGBA straight alpha."""
    path.parent.mkdir(parents=True, exist_ok=True)
    x0, y0, x1, y1 = box
    s = frame / (x1 - x0)
    ys, xs = np.mgrid[0:frame, 0:frame].astype(np.float32)
    sx = x0 + (xs + 0.5) / s
    sy = y0 + (ys + 0.5) / s
    arrs, tags = [], []
    for L in ordered:
        rx = ((sx - L.origin[0]) * L.density - 0.5).astype(np.float32)
        ry = ((sy - L.origin[1]) * L.density - 0.5).astype(np.float32)
        prem = L.rgba.copy()
        prem[..., :3] *= prem[..., 3:4]
        w = cv2.remap(np.ascontiguousarray(prem), rx, ry, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT,
                      borderValue=(0, 0, 0, 0))
        if w[..., 3].max() < 0.02:
            continue
        a = w[..., 3:4]
        rgb = np.where(a > 1e-4, w[..., :3] / np.maximum(a, 1e-4), 0)
        arrs.append((np.clip(np.concatenate([rgb, a], 2), 0, 1) * 255 + 0.5).astype(np.uint8))
        tags.append(L.tag)
    np.savez_compressed(path, layers=np.stack(arrs), tags=np.array(tags), box=np.array(box, np.float32))



def build_strand(spec: dict, L: dict, lm: dict, attr) -> dict | None:
    """Spine of a hair bundle / ribbon tie: the centre line of the layer's
    pixels inside the bundle's region, sampled by height."""
    layer = L.get(spec["layer"])
    if layer is None:
        return None
    cx = lm["center_x"]
    rx = lm["head_radius"][0]

    def ref(v):
        return lm[v] if isinstance(v, str) else float(v)

    if spec["y0"] == "top":
        rows_ = np.nonzero((layer.alpha > 0.5).any(1))[0]
        y0 = layer.origin[1] + (rows_[0] + 0.5) / layer.density + spec.get("y0_off", 0.0) * rx
    else:
        y0 = ref(spec["y0"]) + spec.get("y0_off", 0.0) * rx
    a = layer.alpha
    H, W = a.shape
    ys = np.arange(H)
    xs = layer.origin[0] + (np.arange(W) + 0.5) / layer.density
    lo = None if spec["x"][0] is None else cx + spec["x"][0] * rx
    hi = None if spec["x"][1] is None else cx + spec["x"][1] * rx
    colmask = np.ones(W, bool)
    if lo is not None:
        colmask &= xs >= lo
    if hi is not None:
        colmask &= xs <= hi
    rows_y, rows_x = [], []
    for j in ys:
        y = layer.origin[1] + (j + 0.5) / layer.density
        if y < y0:
            continue
        row = (a[j] > 0.5) & colmask
        if row.sum() < 3:
            continue
        rows_y.append(y)
        rows_x.append(float(xs[row].mean()))
    if len(rows_y) < 10:
        return None
    rows_y = np.array(rows_y); rows_x = np.array(rows_x)
    y1 = float(rows_y.max())
    n = spec.get("points", 24)
    yy = np.linspace(y0, y1, n)
    xx = np.interp(yy, rows_y, rows_x)
    # smooth strongly: the spine follows the bundle's overall line, not its wiggles
    k = max(1, n // 6)
    xx = np.convolve(np.pad(xx, k, mode="edge"), np.ones(2 * k + 1) / (2 * k + 1), mode="valid")
    root_attrs = attr.attrs(np.array([xx[0]]), np.array([yy[0]]), spec.get("kind", "hair_front"))
    if spec.get("bendPerWidth"):
        # how quickly the bundle may turn to hang straight down: a bend spread
        # over bendLen px stretches the bundle's edge by ~ halfwidth * angle / bendLen,
        # so wide bundles bend over a longer stretch than thin ones
        hw = 0.0
        for j in ys:
            y = layer.origin[1] + (j + 0.5) / layer.density
            if y < y0 or y > y0 + spec.get("bendProbe", 400):
                continue
            row = (a[j] > 0.5) & colmask
            if row.any():
                sx_ = np.interp(y, yy, xx)
                hw = max(hw, float(np.abs(xs[row] - sx_).max()))
        spec = dict(spec, bendLen=max(spec.get("bendMin", 80.0), hw * spec["bendPerWidth"]))
    return {
        "id": spec["id"], "layer": spec["layer"], "droop": spec.get("droop", 0.5),
        **{k2: spec[k2] for k2 in ("gravity", "droopCurve", "bendLen", "anchor", "anchorMax") if k2 in spec},
        "pts": [[float(x), float(y)] for x, y in zip(xx, yy)],
        "root": {**{k2: float(root_attrs[k2][0]) for k2 in ("zb", "twist", "lean", "breath")},
                 "head": 1.0 if spec.get("followHead", True) else 0.0},
        "physics": spec.get("physics", {}), "phase": float(zlib.crc32(spec["id"].encode()) % 1000) / 159.0,
        "_x": [lo, hi], "_band": spec.get("band", 0.12) * rx, "_ramp": spec.get("ramp", 0.35),
        "_full_below": ([ref(spec["full_below"]), cx, 1 if (lo is not None and lo >= cx) else -1, 0.1 * rx]
                        if spec.get("full_below") else None),
    }


def strand_weights(sd: dict, x: np.ndarray, y: np.ndarray):
    """Per-vertex weight and spine parameter for one strand."""
    lo, hi = sd["_x"]
    b = sd["_band"]
    w = np.ones(len(x))
    if lo is not None:
        w *= smoothstep(lo - b, lo + b, x)
    if hi is not None:
        w *= 1 - smoothstep(hi - b, hi + b, x)
    y0, y1 = sd["pts"][0][1], sd["pts"][-1][1]
    s = np.clip((y - y0) / max(1.0, y1 - y0), 0, 1)
    fb = sd.get("_full_below")
    if fb is not None:
        # below this height everything on this side of the centre line hangs on the strand
        yfb, cx_, side, r_ = fb
        sidew = smoothstep(cx_ - r_, cx_ + r_, x) if side > 0 else 1 - smoothstep(cx_ - r_, cx_ + r_, x)
        w = np.maximum(w, smoothstep(yfb - r_, yfb + r_, y) * sidew)
    w *= smoothstep(0.0, sd["_ramp"], s)
    return w, s



def lift_over(parts: list[Layer], occ: Layer, source_rgb: np.ndarray, name: str, tol: float = 0.16,
              reach: float = 3.0) -> Layer | None:
    """Lines drawn OVER another layer in the original (upper eyelashes over the
    tips of the bangs) would be copied into that layer by restore_visible and
    stay on the hair when the eyes close.

    See-through's own prediction of the occluder is clean there, so around the
    parts we keep that prediction (no restore) and move whatever the original
    shows on top of it - pixels that differ clearly from the predicted hair -
    into a separate layer drawn above the occluder, which is shown and hidden
    together with ``parts``. Returns the new layer."""
    h, w = occ.alpha.shape
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    sx = occ.origin[0] + (xs + 0.5) / occ.density
    sy = occ.origin[1] + (ys + 0.5) / occ.density
    pa = np.zeros((h, w), np.float32)
    for part in parts:
        pa = np.maximum(pa, part.sample_alpha(sx, sy))
    r = max(1, int(round(reach * occ.density)))
    zone = cv2.dilate((pa > 0.25).astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1)))
    zone = zone.astype(bool) & (occ.alpha > 0.5)
    src = cv2.remap(source_rgb, (sx - 0.5).astype(np.float32), (sy - 0.5).astype(np.float32), cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_REPLICATE)
    diff = np.abs(src - occ.rgba[..., :3]).max(-1)
    # the predicted hair stays unrestored over the whole zone (+ a soft margin)
    keep = cv2.GaussianBlur(cv2.dilate(zone.astype(np.float32), np.ones((3, 3), np.uint8)), (0, 0), 1.5 * occ.density)
    occ.meta["no_restore"] = np.maximum(occ.meta.get("no_restore", 0), keep)
    # soft cut-out: over the predicted hair, lifted * a + hair * (1 - a) stays
    # within a few percent of the original, without hard pixel steps
    # only what is drawn darker than the hair (lash lines and their blue
    # shading); lighter differences are the hair's own edge highlights, which
    # must stay with the (swaying) hair
    wl = np.array([0.3, 0.55, 0.15], np.float32)
    darker = (occ.rgba[..., :3] @ wl) - (src @ wl)
    a = smoothstep(tol * 0.3, tol, diff) * smoothstep(0.01, 0.06, darker) * keep * occ.alpha
    if (a > 0.5).sum() < 10:
        return None
    # un-premultiply the colour so the composite reproduces the original
    hair = occ.rgba[..., :3]
    col = np.where(a[..., None] > 1e-3, hair + (src - hair) / np.maximum(a[..., None], 1e-3), src)
    src = np.clip(col, 0, 1)
    rgba = np.concatenate([src, a[..., None]], 2).astype(np.float32)
    lifted = Layer(name, rgba, occ.origin, occ.density).crop_to_content()
    lifted.meta["no_restore"] = 1.0
    return lifted



def trace_cut(L: Layer, x_end: float, y_end: float, y_top: float, slope: float = 0.25, win: float = 40.0,
              edge_margin: float = 0.0, edge_until: float | None = None) -> np.ndarray:
    """A cut between two hair bundles: a path from the gap between their tips
    (x_end, y_end) up to y_top that runs along the drawn line art (dark lines
    and transparent gaps are cheap to cut through, bright hair is expensive).
    The path is guided by the layer's own fan-out (a proportional line between
    the silhouettes at y_top and y_end) and may drift by ``slope`` px per row.
    Returns the cut's x (source px) for every layer row from y_top to y_end."""
    a = L.alpha
    rgb = L.rgba[..., :3]
    d = L.density
    lum = rgb @ np.array([0.3, 0.55, 0.15], np.float32)
    ref = cv2.GaussianBlur(lum * a, (0, 0), 5 * d) / np.maximum(cv2.GaussianBlur(a, (0, 0), 5 * d), 1e-3)
    dark = np.clip((ref - lum) / 0.25, 0, 1)
    cost = np.where(a < 0.3, 0.05, 1.0 - 0.9 * dark).astype(np.float32)
    H, W = a.shape
    if edge_margin > 0:
        # keep cuts away from the outer silhouette (in the upper part, where
        # it runs diagonally): a cut next to the outline would shift the
        # outline whenever the two bundles slide along each other
        re = H if edge_until is None else int(np.clip(round((edge_until - L.origin[1]) * d), 0, H))
        m = int(round(edge_margin * d))
        colsw = np.arange(W)
        for r in range(re):
            c = np.nonzero(a[r] > 0.5)[0]
            if len(c):
                cost[r, (colsw < c.min() + m) | (colsw > c.max() - m)] += 5.0
    r0 = int(np.clip(round((y_top - L.origin[1]) * d), 0, H - 1))
    r1 = int(np.clip(round((y_end - L.origin[1]) * d), 0, H - 1))
    xe = (x_end - L.origin[0]) * d

    def span(r):
        c = np.nonzero(a[r] > 0.5)[0]
        return (c.min(), c.max()) if len(c) else (0, W - 1)

    l0, rr0 = span(r0)
    l1, rr1 = span(r1)
    u = (xe - l1) / max(1, rr1 - l1)
    xt = l0 + u * (rr0 - l0)
    rows = np.arange(r0, r1 + 1)
    guide = xt + (xe - xt) * (rows - r0) / max(1, r1 - r0)
    INF = np.float32(1e9)
    D = np.full(W, INF, np.float32)
    D[int(round(np.clip(xe, 0, W - 1)))] = 0
    back = np.zeros((len(rows), W), np.int8)
    step = max(1, int(round(1 / (slope / d if d > 1 else slope))))
    cols = np.arange(W)
    wr = win * d
    for i in range(len(rows) - 2, -1, -1):
        if i % step == 0:
            left = np.concatenate([[INF], D[:-1]])    # came from x-1 on the row below
            right = np.concatenate([D[1:], [INF]])    # came from x+1
            cand = np.stack([D, left, right])
            k = cand.argmin(0)
            best = cand.min(0)
        else:
            k = np.zeros(W, np.int64)
            best = D
        band = np.abs(cols - guide[i]) <= wr
        D = np.where(band, best + cost[rows[i]] + 0.01 / d * np.abs(cols - guide[i]), INF).astype(np.float32)
        back[i] = np.array([0, -1, 1], np.int8)[k]
    x = int(D.argmin())
    path = np.empty(len(rows), np.float64)
    for i in range(len(rows)):
        path[i] = x
        if i < len(rows) - 1:
            x = int(np.clip(x + int(back[i, x]), 0, W - 1))
    return path   # in layer columns, rows r0..r1


def trace_cut_down(L: Layer, x_start: float, y_start: float, win: float = 60.0) -> np.ndarray:
    """Continue a cut below the bundle tips down to the bottom of the layer,
    through the gaps (transparent pixels are cheap, hair is expensive), so a
    lock hanging below the tips is not split by a straight continuation.
    Returns the cut's x (layer columns) for rows from y_start to the bottom."""
    a = L.alpha
    d = L.density
    H, W = a.shape
    r1 = int(np.clip(round((y_start - L.origin[1]) * d), 0, H - 1))
    x0 = (x_start - L.origin[0]) * d
    cost = np.where(a < 0.02, 0.02, 0.3 + 0.7 * a).astype(np.float32)
    cols = np.arange(W)
    rows = np.arange(r1, H)
    INF = np.float32(1e9)
    D = np.full(W, INF, np.float32)
    D[int(round(np.clip(x0, 0, W - 1)))] = 0
    back = np.zeros((len(rows), W), np.int8)
    band = np.abs(cols - x0) <= win * d
    for i in range(1, len(rows)):
        left = np.concatenate([[INF], D[:-1]])
        right = np.concatenate([D[1:], [INF]])
        cand = np.stack([D, left, right])
        k = cand.argmin(0)
        D = np.where(band, cand.min(0) + cost[rows[i]] + 0.002 * np.abs(cols - x0), INF).astype(np.float32)
        back[i] = np.array([0, -1, 1], np.int8)[k]
    x = int(D.argmin())
    path = np.empty(len(rows))
    for i in range(len(rows) - 1, -1, -1):
        path[i] = x
        x = int(np.clip(x + int(back[i, x]), 0, W - 1))
    return path


def split_bundles(L: Layer, spec: dict, lm: dict, log) -> list[tuple[Layer, dict]]:
    """Cut a hair layer into separate bundles below ``y_top`` (Live2D-style: one
    art mesh per bundle), so that every bundle can swing on its own without
    stretching the drawing between them.

    The cuts follow the line art (trace_cut). The part above y_top stays in the
    parent layer and keeps ``overlap`` px below y_top, so the weld at the top of
    each bundle (where it still moves exactly like the parent) shows no seam.
    Along a cut, the bundle drawn behind extends ``extend`` px under its
    neighbour, so a small relative swing never opens a gap.
    Returns [(piece layer, strand spec)] from back to front."""
    d = L.density
    H, W = L.alpha.shape

    def ref(v):
        return lm[v] if isinstance(v, str) else float(v)

    y_top = ref(spec["y_top"])
    y_gap = ref(spec["y_gap"])
    r0 = int(np.clip(round((y_top - L.origin[1]) * d), 0, H - 1))
    r1 = int(np.clip(round((y_gap - L.origin[1]) * d), 0, H - 1))
    cuts = []
    for xg in spec["cuts"]:
        path = trace_cut(L, xg, y_gap, y_top, spec.get("slope", 0.25), spec.get("window", 40),
                         spec.get("edge_margin", 0.0), ref(spec["edge_until"]) if "edge_until" in spec else None)
        full = np.empty(H)
        full[:r0] = path[0]
        full[r0:r1 + 1] = path
        down = trace_cut_down(L, L.origin[0] + (path[-1] + 0.5) / d, y_gap, spec.get("window_down", 60))
        full[r1:] = down
        cuts.append(full)
    cuts.sort(key=lambda c: c[r1])
    cols = np.arange(W)[None, :]
    rowsv = np.arange(H)[:, None]
    below = rowsv >= r0
    # label = number of cuts left of the pixel
    label = np.zeros((H, W), np.int32)
    for c in cuts:
        label += (cols >= c[:, None]).astype(np.int32)
    n = len(cuts) + 1
    cx = (lm["center_x"] - L.origin[0]) * d
    centre = [np.median(np.nonzero((label == k) & below & (L.alpha > 0.5))[1]) if ((label == k) & below & (L.alpha > 0.5)).any()
              else cx for k in range(n)]
    # draw order: bundles farther from the centre line go behind ("inner" in
    # front), or in front ("outer": the silhouette and its outline stay on top)
    dist = [abs(c - cx) for c in centre]
    sign = 1 if spec.get("front", "inner") == "outer" else -1
    order = sorted(range(n), key=lambda k: sign * dist[k])
    rank = {k: i for i, k in enumerate(order)}
    ext = spec.get("extend", 8) * d
    pieces = []
    er = max(1, int(round(ext)))
    kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * er + 1, 2 * er + 1))
    for k in order:
        own = (label == k) & below
        # extend under the neighbours drawn in front of this bundle
        grown = cv2.dilate(own.astype(np.uint8), kern).astype(bool) & below
        front = np.zeros_like(own)
        for j in range(n):
            if j != k and rank[j] > rank[k]:
                front |= (label == j)
        m = own | (grown & front)
        m = m.astype(np.float32)
        # soften the cut edge by half a pixel
        m = np.minimum(m, cv2.GaussianBlur(m, (0, 0), 0.5 * d) * 2)
        rgba = L.rgba.copy()
        rgba[..., 3] *= np.clip(m, 0, 1)
        # stray near-transparent specks would give the piece mesh far away
        rgba[..., 3] = np.where(rgba[..., 3] < 0.04, 0, rgba[..., 3])
        if (rgba[..., 3] > 0.5).sum() < 50 * d * d:
            continue
        tag = f"{L.tag}#{k}"
        piece = Layer(tag, rgba, L.origin, L.density).crop_to_content()
        piece.meta.update({k2: v for k2, v in L.meta.items() if k2 in ("opacity", "no_occlude")})
        piece.meta["overlap"] = float(spec.get("extend", 8))
        st = dict(spec.get("strand", {}))
        st.update((spec.get("pieces") or {}).get(str(k), {}))
        st.update({"id": f"{spec.get('id', L.tag)}{k}", "layer": tag, "x": [None, None]})
        st.setdefault("y0", y_top)
        # every bundle swings at its own pace
        ph = dict(st.get("physics", {}))
        if not ph.get("static"):
            f = 1.0 + 0.12 * np.sin(2.3 * k + 0.7)
            for key in ("freqMid", "freqTip"):
                if key in ph:
                    ph[key] = float(ph[key] * f)
        st["physics"] = ph
        pieces.append((piece, st))
    # the parent keeps everything above y_top (+ overlap under the bundles)
    keep = (rowsv < r0 + spec.get("overlap", 6) * d).astype(np.float32)
    L.rgba[..., 3] *= keep
    log(f"[build] split '{L.tag}' into {len(pieces)} bundles below y={y_top:.0f}")
    return pieces


def lift_part(L: Layer, spec: dict, source_rgb: np.ndarray) -> Layer | None:
    """Cut a small accessory drawn on top of a layer (a cord with its tassel on
    a dress or on a ribbon's cloth) out into its own layer so it can swing.

    The accessory is found in the original: pixels of its colour (HSV limits)
    connected to the given seed points inside a box, plus its outline. Under
    it the parent is filled from the surrounding colours - but only where the
    parent continues on both sides (a closing of the parent's own coverage), so
    a cord hanging over the background leaves nothing behind."""
    x0, y0, x1, y1 = spec["box"]
    rgb8 = (np.clip(source_rgb, 0, 1) * 255).astype(np.uint8)
    hsv = cv2.cvtColor(rgb8, cv2.COLOR_RGB2HSV).astype(np.int32)
    Hh, Ss, Vv = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    cond = np.zeros(Hh.shape, bool)
    sl = (slice(y0, y1), slice(x0, x1))
    cond[sl] = ((Ss >= spec.get("s_min", 140)) & (Vv >= spec.get("v_min", 90)) & (Vv <= spec.get("v_max", 255))
                & (Hh >= spec.get("h_min", 100)) & (Hh <= spec.get("h_max", 122)))[sl]
    lab, _ = ndimage.label(cond)
    ids = [lab[y, x] for x, y in spec["seeds"] if lab[y, x] > 0]
    if not ids:
        return None
    m = np.isin(lab, ids)
    m = ndimage.binary_closing(m, iterations=2)
    m = ndimage.binary_fill_holes(m)
    m = ndimage.binary_dilation(m, iterations=spec.get("dilate", 2)).astype(np.float32)
    # into the layer's own pixel grid
    h, w = L.alpha.shape
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    sx = L.origin[0] + (xs + 0.5) / L.density - 0.5
    sy = L.origin[1] + (ys + 0.5) / L.density - 0.5
    ml = cv2.remap(m, sx, sy, cv2.INTER_LINEAR, borderValue=0)
    ml = np.clip(ml, 0, 1) * (L.alpha > 0.05)
    if ml.sum() < 30:
        return None
    rgba = L.rgba.copy()
    rgba[..., 3] = L.alpha * ml
    part = Layer(spec["name"], rgba, L.origin, L.density).crop_to_content()
    part.meta["no_restore"] = 1.0
    # the parent under the accessory
    hard = (ml > 0.5).astype(np.uint8)
    k = max(3, int(round(spec.get("under", 16) * L.density)) | 1)
    rest = (L.alpha * (1 - hard)).astype(np.float32)
    closed = cv2.morphologyEx(rest, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    newa = np.where(hard > 0, np.minimum(closed, L.alpha), L.alpha)
    src8 = (np.clip(L.rgba[..., :3], 0, 1) * 255).astype(np.uint8)
    fill = cv2.inpaint(src8, cv2.dilate(hard, np.ones((3, 3), np.uint8)), 5, cv2.INPAINT_TELEA).astype(np.float32) / 255
    wgt = cv2.GaussianBlur(cv2.dilate(hard, np.ones((3, 3), np.uint8)).astype(np.float32), (0, 0), 0.8)[..., None]
    L.rgba[..., :3] = L.rgba[..., :3] * (1 - wgt) + fill * wgt
    L.rgba[..., 3] = newa
    return part

def split_behind(L: Layer, front: Layer, source_rgb: np.ndarray, margin=0.04, grow=2):
    """Cut out of ``L`` the pixels that the original shows as ``front``.

    See-through sometimes puts the hidden inside of a part into the part that is
    drawn *above* its neighbour (e.g. the inside of the collar in the top wear,
    over the neck). Where the original shows the neighbour's colour, the pixel
    really lies behind it; left in place it becomes a seam once the neighbour
    moves. Returns (L without those pixels, the cut-out part as '<tag>-back').
    """
    h, w = L.alpha.shape
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    sx = L.origin[0] + (xs + 0.5) / L.density
    sy = L.origin[1] + (ys + 0.5) / L.density
    fa = front.sample_alpha(sx, sy)
    src = cv2.remap(source_rgb, (sx - 0.5).astype(np.float32), (sy - 0.5).astype(np.float32), cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_REPLICATE)
    fx = ((sx - front.origin[0]) * front.density - 0.5).astype(np.float32)
    fy = ((sy - front.origin[1]) * front.density - 0.5).astype(np.float32)
    f_rgb = cv2.remap(np.ascontiguousarray(front.rgba[..., :3]), fx, fy, cv2.INTER_LINEAR,
                      borderMode=cv2.BORDER_REPLICATE)
    d_own = np.abs(src - L.rgba[..., :3]).mean(axis=2)
    d_front = np.abs(src - f_rgb).mean(axis=2)
    behind = ((d_front + margin < d_own) & (fa > 0.5)).astype(np.uint8)
    behind = cv2.morphologyEx(behind, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    behind = cv2.morphologyEx(behind, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    g = max(1, int(grow * L.density))
    behind = cv2.dilate(behind, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * g + 1, 2 * g + 1)))
    behind = behind.astype(np.float32) * (fa > 0.2)
    m = cv2.GaussianBlur(behind, (0, 0), 1.0)
    back = Layer(L.tag + "-back", L.rgba.copy(), L.origin, L.density, None, L.depth_median, dict(L.meta))
    back.rgba[..., 3] *= m
    L.rgba[..., 3] *= 1 - m
    return back.crop_to_content()


def build(recipe_path: Path, out_dir: Path, log=print, dump_only: bool = False) -> dict:
    recipe_path = Path(recipe_path)
    recipe = load_recipe(recipe_path)
    base_dir = recipe_path.parent   # every path in a recipe is relative to the recipe file
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"[build] loading layers for '{recipe['name']}'")
    L, meta, source_rgb = load_layers(recipe, base_dir)
    size = tuple(meta["source_size"])

    lm = estimate_landmarks(L, size)
    lm.update(recipe.get("landmarks", {}))
    log(f"[build] landmarks: head_center={np.round(lm['head_center'], 1).tolist()} "
        f"radius={np.round(lm['head_radius'], 1).tolist()} chin={lm['chin_y']:.0f} hem={lm['hem_y']:.0f}")

    # accessory split (e.g. ribbon knot in front of the bangs)
    for spec in recipe.get("split_front", []):
        if spec["layer"] in L and spec["occluder"] in L:
            fr = split_front_back(L[spec["layer"]], L[spec["occluder"]], source_rgb)
            if fr.alpha.max() > 0.1:
                L[fr.tag] = fr

    for spec in recipe.get("lift_over", []):
        parts = [L[t] for t in spec["parts"] if t in L]
        if parts and spec["occluder"] in L:
            lf = lift_over(parts, L[spec["occluder"]], source_rgb, spec["name"], spec.get("tol", 0.16))
            if lf is not None:
                L[spec["name"]] = lf
                log(f"[build] lifted {int((lf.alpha > 0.5).sum())} px of {spec['parts']} drawn over '{spec['occluder']}'")

    for spec in recipe.get("split_behind", []):
        if spec["layer"] in L and spec["front"] in L:
            bk = split_behind(L[spec["layer"]], L[spec["front"]], source_rgb, spec.get("margin", 0.04))
            if bk.alpha.max() > 0.1:
                L[bk.tag] = bk
                log(f"[build] moved {int((bk.alpha > 0.5).sum())} px of '{spec['layer']}' behind '{spec['front']}'")

    order = [t for t in recipe["order"] if t in L]
    for t in L:
        if t not in order:
            log(f"  ! layer '{t}' not in order list, appended on top")
            order.append(t)
    ordered = [L[t] for t in order]
    for t, spec in recipe["layers"].items():
        if t in L and spec.get("no_occlude"):
            L[t].meta["no_occlude"] = True

    for spec in recipe.get("cleanup", []):
        if spec["layer"] in L:
            inpaint_under(L[spec["layer"]], [L[t] for t in spec["under"] if t in L], spec.get("grow", 6),
                          spec.get("shrink", 3))
            log(f"[build] cleaned occluded area of '{spec['layer']}'")

    rs = recipe.get("restore", {})
    if rs.get("enabled", True):
        restore_visible(ordered, source_rgb, rs.get("strength", 1.0), tags=set(rs.get("tags", order)),
                        boundary_tags=rs.get("boundary_tags", []))
        log("[build] restored visible pixels from the source illustration")

    # ------------------------------------------------ accessories cut out of their layer (cords, tassels)
    for spec in recipe.get("lift_parts", []):
        if spec["layer"] not in L:
            continue
        part = lift_part(L[spec["layer"]], spec, source_rgb)
        if part is None:
            log(f"  ! lift_parts {spec['name']}: nothing found")
            continue
        L[part.tag] = part
        order.insert(order.index(spec["layer"]) + 1, part.tag)
        log(f"[build] cut '{part.tag}' out of '{spec['layer']}' ({int((part.alpha > 0.5).sum())} px)")

    # ------------------------------------------------ hair bundles (one mesh per bundle)
    extra_strands = []
    for spec in recipe.get("bundles", []):
        if spec["layer"] not in L:
            continue
        pieces = split_bundles(L[spec["layer"]], spec, lm, log)
        at = order.index(spec["layer"]) + 1
        for piece, st in pieces:
            L[piece.tag] = piece
            order.insert(at, piece.tag)
            at += 1
            extra_strands.append(st)
    recipe["strands"] = list(recipe.get("strands", [])) + extra_strands

    dump_dir = __import__("os").environ.get("STRIG_DUMP_LAYERS")
    if dump_dir:
        # debugging aid: every layer on its own, at page size
        Path(dump_dir).mkdir(parents=True, exist_ok=True)
        for t in order:
            im = composite([L[t]], size)
            cv2.imwrite(str(Path(dump_dir) / f"{t}.png"), (im[..., [2, 1, 0, 3]] * 255).clip(0, 255).astype(np.uint8))

    kr0 = recipe.get("keyforms") or {}
    if kr0.get("fit_dir"):
        dump_fit_layers([L[t] for t in order], kr0["box"], kr0.get("frame", 1024), base_dir / kr0["fit_dir"] / "layers.npz")
        log(f"[build] wrote fitting inputs to {kr0['fit_dir']}/layers.npz")
        if dump_only:
            return {}

    # ------------------------------------------------ expression variants
    variant_layers: list[tuple[Layer, dict]] = []
    ev = recipe.get("expressions")
    if ev:
        paths = {k: (base_dir / v if not Path(v).is_absolute() else Path(v)) for k, v in ev["images"].items()}
        vs = ex.load_variants(ev["box"], paths, source_rgb, reference=ev.get("reference"))
        variant_layers = build_variants(vs, ev, L, lm, log)

    attr = AttrModel(lm, recipe.get("attr", {}))
    chains = default_chains(lm, L, recipe)
    ch_over = recipe.get("chains", {})
    for c in chains:
        c.update(ch_over.get(c["id"], {}))
    chains = [c for c in chains if not c.get("disabled")]

    # ------------------------------------------------ keyforms (drawn poses)
    poses: dict[str, PoseKeyform] = {}
    kr = recipe.get("keyforms")
    if kr:
        nd = base_dir / kr["neutral"]
        fitted = kr.get("fitted")
        for key, pdir in kr["keys"].items():
            # canonical key name: AngleX first, integer values
            kv = dict(p_.split(":") for p_ in key.replace(" ", "").split(","))
            key = ",".join(f"{a_}:{int(float(kv[a_]))}" for a_ in ("AngleX", "AngleY") if a_ in kv)
            pdir = base_dir / pdir
            fnpz = base_dir / kr.get("fit_dir", "") / (key.replace(":", "_").replace(",", "__") + ".npz")
            if fitted and fnpz.exists():
                poses[key] = FittedKeyform(fnpz, kr["box"], kr.get("frame", 1024))
            elif fitted:
                log(f"  ! keyform {key}: no fitted field ({fnpz.name}), skipped")
            elif (pdir / "info.json").exists() and (nd / "info.json").exists():
                poses[key] = PoseKeyform(nd, pdir, kr["box"], kr.get("frame", 1024))
            else:
                log(f"  ! keyform {key}: decomposition missing ({pdir}), skipped")
        log(f"[build] keyforms: {sorted(poses)}")

    anchors = (kr or {}).get("anchored", {})   # e.g. headwear follows the bangs at one point

    def keyform_offsets(did: str, x, y):
        out = {}
        base = did.split("@")[0]
        for suf in ("-front", "-back"):
            if base.endswith(suf):
                base = base[: -len(suf)]
        for key, pk in poses.items():
            if isinstance(pk, FittedKeyform):
                dx, dy = pk.offsets(fit_group(did), x, y)
            elif base in anchors:
                # rigid follow: the motion of the anchor part at the anchor point,
                # faded out where the part hangs below the head
                src_tag, (axp, ayp) = anchors[base]
                ddx, ddy = pk.offsets([src_tag], np.array([axp]), np.array([ayp]))
                hw = attr.hair_head_weight(np.asarray(y, np.float64))
                dx, dy = ddx[0] * hw, ddy[0] * hw
            else:
                dx, dy = pk.offsets(source_tags(did), x, y)
            out[key] = b64(np.stack([dx, dy], 1).reshape(-1), "f32")
        return out

    # ------------------------------------------------ rigid facial parts
    # Eyes, brows, mouth, nose and ears are drawn shapes: under head motion they
    # only move / rotate / foreshorten as a unit and never shear or bend.
    RIGID = {"eyeR": ["eyewhite-r", "irides-r", "eyelash-r"], "eyeL": ["eyewhite-l", "irides-l", "eyelash-l"],
             "browR": ["eyebrow-r"], "browL": ["eyebrow-l"], "mouth": ["mouth"], "nose": ["nose"],
             "earR": ["ears-r"], "earL": ["ears-l"]}
    rigid_anchor = {}
    for gname, tags_ in RIGID.items():
        bbs = [alpha_bbox(L[t], 0.3) for t in tags_ if t in L]
        if not bbs:
            continue
        cx = (min(b[0] for b in bbs) + max(b[2] for b in bbs)) / 2
        cy = (min(b[1] for b in bbs) + max(b[3] for b in bbs)) / 2
        t0 = tags_[0]
        sp = recipe["layers"].get(t0, {})
        a0 = attr.attrs(np.array([cx]), np.array([cy]), sp.get("kind", "facepart"), sp.get("z", 0.0))
        rigid_anchor[gname] = {"c": [float(cx), float(cy)], "z": float(a0["z"][0])}

    def rigid_group(did: str):
        t = did
        for pre, g in (("eyeL-", "eyeL"), ("eyeR-", "eyeR"), ("browL-", "browL"), ("browR-", "browR"), ("mouth", "mouth")):
            if t.startswith(pre):
                return g
        for g, tags_ in RIGID.items():
            if t in tags_:
                return g
        return None

    # ------------------------------------------------ strands (bending hair bundles)
    strands = [sd for sd in (build_strand(sp, L, lm, attr) for sp in recipe.get("strands", [])) if sd]
    if strands:
        log(f"[build] strands: {[sd['id'] for sd in strands]}")

    # ------------------------------------------------ drawables
    atlas = Atlas(recipe.get("atlas_size", 4096))
    drawables = []
    specs = recipe["layers"]
    spacing_default = recipe.get("mesh_spacing", {})

    def add_drawable(layer: Layer, did: str, order_i: float, lspec: dict):
        kind = lspec.get("kind", "body")
        spacing = lspec.get("spacing", spacing_default.get(kind, 18))
        pos, uvpx, tris = grid_mesh(layer, spacing)
        if len(tris) == 0:
            return
        page, ax, ay = atlas.add(layer.rgba)
        uv = np.empty_like(uvpx)
        uv[:, 0] = (ax + uvpx[:, 0]) / atlas.size
        uv[:, 1] = (ay + uvpx[:, 1]) / atlas.size
        a = attr.attrs(pos[:, 0], pos[:, 1], kind, lspec.get("z", 0.0))
        d = {
            "id": did, "tag": layer.tag, "atlas": page, "order": order_i, "kind": kind,
            "positions": b64(pos.reshape(-1), "f32"), "uvs": b64(uv.reshape(-1), "f32"),
            "indices": b64(tris.reshape(-1), "u32"),
            "attrs": {k: b64(v, "f32") for k, v in a.items()},
            "deformers": [auto_deformer(df, layer) for df in lspec.get("deformers", [])],
            "vertexCount": int(len(pos)),
            # parallax mode: one depth and centre per part (median over the part)
            **({"pz": float(np.median(a["z"][a["head"] > 0.9])),
                "pc": [float(np.median(pos[a["head"] > 0.9, 0])), float(np.median(pos[a["head"] > 0.9, 1]))]}
               if (a["head"] > 0.9).any() else {}),
        }
        if poses and kind in ("face", "facepart", "ears", "hair_front", "hair_back", "headwear", "neck"):
            kfo = keyform_offsets(did, pos[:, 0], pos[:, 1])
            if kind == "neck":
                # only the top of the neck follows the head; the base stays on the body
                hw = a["head"].astype(np.float32)
                for k, v in kfo.items():
                    arr = np.frombuffer(base64.b64decode(v["b64"]), np.float32).reshape(-1, 2) * hw[:, None]
                    kfo[k] = b64(arr.reshape(-1), "f32")
            d["keyforms"] = kfo
        rg = rigid_group(did)
        if rg in rigid_anchor:
            d["rigid"] = rigid_anchor[rg]
        pf = (recipe.get("rig", {}).get("head", {}).get("follow", {}) or {}).get(did.split("-front")[0])
        if pf and "pc" in d:
            y0 = lm[pf["from"]] if isinstance(pf["from"], str) else pf["from"]
            y1 = lm[pf["to"]] if isinstance(pf["to"], str) else pf["to"]
            d["pfollow"] = pf["part"]
            d["attrs"]["pf"] = b64(1 - smoothstep(y0, y1, pos[:, 1]), "f32")
        hcfg = recipe.get("rig", {}).get("head", {})
        spl = hcfg.get("split", {}).get(did.split("-front")[0])
        if spl and "pc" in d:
            # sub-pieces: [{"name", "x": [lo, hi] in head radii from centre, "z": depth in head radii}]
            hcx_ = lm["head_center"][0]
            rx_, _, rz_ = lm["head_radius"]
            band = spl.get("band", 0.12) * rx_
            subs, W = [], []
            for pc_ in spl["pieces"]:
                lo, hi = [hcx_ + v * rx_ if v is not None else None for v in pc_["x"]]
                w_ = np.ones(len(pos))
                if lo is not None:
                    w_ *= smoothstep(lo - band, lo + band, pos[:, 0])
                if hi is not None:
                    w_ *= 1 - smoothstep(hi - band, hi + band, pos[:, 0])
                if "y" in pc_:
                    # vertical extent in units of (eye_y - head_top) below eye_y; y0 blends over yband
                    ey_ = lm["eye_y"]; span_ = ey_ - lm["head_top"]
                    y0_ = ey_ + pc_["y"] * span_
                    yb_ = spl.get("yband", 0.3) * span_
                    w_ *= smoothstep(y0_ - yb_, y0_, pos[:, 1])
                cx_ = float(np.median(pos[w_ > 0.9, 0])) if (w_ > 0.9).any() else float(hcx_)
                cy_ = float(np.median(pos[w_ > 0.9, 1])) if (w_ > 0.9).any() else d["pc"][1]
                subs.append({"id": f"{did}#{pc_['name']}", "c": [cx_, cy_], "z": float(pc_["z"] * rz_)})
                W.append(w_)
            d["sub"] = subs
            d["attrs"]["sw"] = b64(np.stack(W, 1).reshape(-1), "f32")
        fsc = hcfg.get("foreshorten")
        if fsc and "pc" in d and kind in fsc.get("kinds", []):
            d["fs"] = 1
        hang = hcfg.get("hang_translate", [])
        if kind in hang:
            d["attrs"]["ht"] = b64(np.ones(len(pos)), "f32")
        if kind in hcfg.get("hang_rotate", []):
            d["attrs"]["head"] = b64(np.ones(len(pos)), "f32")
        if kind in hcfg.get("body_rigid", []):
            # carried by the neck: the body motion of the neck base, not per vertex
            zb0, tw0, le0, br0 = attr.body_attrs(np.array([lm["center_x"]]), np.array([lm["neck_pivot"][1]]), "head")
            for k_, v_ in (("zb", zb0), ("twist", tw0), ("lean", le0), ("breath", br0)):
                d["attrs"][k_] = b64(np.full(len(pos), float(v_[0])), "f32")
        if lspec.get("opacity"):
            d["opacity"] = [opacity_term(o) for o in lspec["opacity"]]
        if lspec.get("clip"):
            d["clip"] = lspec["clip"]
        mine = [sd for sd in strands if sd["layer"] == did]
        if mine:
            W_, S_ = zip(*(strand_weights(sd, pos[:, 0], pos[:, 1]) for sd in mine))
            W_ = np.stack(W_, 1)
            tot = W_.sum(1, keepdims=True)
            W_ = np.where(tot > 1, W_ / np.maximum(tot, 1e-9), W_)
            d["strandIds"] = [sd["id"] for sd in mine]
            d["attrs"]["stw"] = b64(W_.reshape(-1), "f32")
            d["attrs"]["sts"] = b64(np.stack(S_, 1).reshape(-1), "f32")
        if lspec.get("chains") and not recipe.get("no_chains"):
            d["deformers"] += chain_bindings(pos[:, 0], pos[:, 1], chains, lspec["chains"])
        if lspec.get("group"):
            d["group"] = lspec["group"]
        if "overlap" in layer.meta:
            d["overlap"] = layer.meta["overlap"]   # px this bundle reaches under its neighbours
        drawables.append(d)

    for i, t in enumerate(order):
        lspec = dict(specs.get(t, {}))
        if t.endswith("-front") and t[:-6] in specs:
            lspec = dict(specs[t[:-6]], **recipe.get("front_overrides", {}).get(t, {}))
        if t.endswith("-back") and t[:-5] in specs:
            lspec = dict(specs[t[:-5]])
        if "#" in t or "+" in t:
            lspec = dict(specs.get(t.split("#")[0].split("+")[0], {}))
        add_drawable(L[t], t, float(i), lspec)
    # ------------------------------------------------ pose parts (morph targets)
    # The generated pose image's own parts, mapped back to the neutral shape.
    # They are drawn just above the matching neutral part and fade in towards
    # their key, so at the key the drawn pose itself is visible.
    morph_tags = kr.get("morph_parts", []) if kr and not kr.get("fitted") else []
    def ramp_on(val):     # 0 at 30% of the key value, 1 at 70%
        lo, hi = 0.3 * val, 0.7 * val
        return [[lo, 0], [hi, 1]] if val > 0 else [[hi, 1], [lo, 0]]

    def ramp_off(val):    # 1 near zero, 0 towards |val| on either side
        a, b = 0.3 * abs(val), 0.7 * abs(val)
        return [[-b, 0], [-a, 1], [a, 1], [b, 0]]

    for ki, (key, pk) in enumerate(sorted(poses.items())):
        axes = dict((a, float(v)) for a, v in (kv.split(":") for kv in key.split(",")))
        # a pose part set is shown only near its own key: the ramps of its axes
        # times "the other axis is near zero" for single-axis keys
        ramp = [{"param": a, "keys": ramp_on(v)} for a, v in axes.items()]
        for other in ("AngleX", "AngleY"):
            if other not in axes and any(other in k for k in poses):
                ramp.append({"param": other, "keys": ramp_off(30.0)})
        for t in morph_tags:
            if t not in order:
                continue
            kl = pk.pose_layer(t, f"{t}@{key}")
            inv = pk.inverse_field(t)
            if kl is None or inv is None:
                continue
            base_spec = dict(specs.get(t, {}))
            kind = base_spec.get("kind", "facepart")
            spacing = 1.6 * base_spec.get("spacing", spacing_default.get(kind, 18))
            kpos, kuv, ktris = grid_mesh(kl, spacing)
            if len(ktris) == 0:
                continue
            dx, dy = pk.sample(inv, kpos[:, 0], kpos[:, 1])
            rest = kpos + np.stack([dx, dy], 1)
            page, ax_, ay_ = atlas.add(kl.rgba)
            uv = np.empty_like(kuv)
            uv[:, 0] = (ax_ + kuv[:, 0]) / atlas.size
            uv[:, 1] = (ay_ + kuv[:, 1]) / atlas.size
            a = attr.attrs(rest[:, 0], rest[:, 1], kind, base_spec.get("z", 0.0))
            kfo = keyform_offsets(t, rest[:, 0], rest[:, 1])
            kfo[key] = b64((kpos - rest).reshape(-1), "f32")
            did = f"{t}@{key}"
            d = {
                "id": did, "tag": t, "atlas": page, "order": order.index(t) + 0.05 * (ki + 1), "kind": kind,
                "positions": b64(rest.reshape(-1), "f32"), "uvs": b64(uv.reshape(-1), "f32"),
                "indices": b64(ktris.reshape(-1), "u32"),
                "attrs": {k2: b64(v, "f32") for k2, v in a.items()},
                "deformers": [auto_deformer(df, kl) for df in base_spec.get("deformers", [])],
                "vertexCount": int(len(rest)),
                "keyforms": kfo,
                "opacity": [opacity_term(o) for o in base_spec.get("opacity", [])],
                "morph": {"tag": t, "key": key},
            }
            if base_spec.get("clip"):
                d["clip"] = [f"{c}@{key}" for c in base_spec["clip"]]
            if base_spec.get("chains"):
                d["deformers"] += chain_bindings(rest[:, 0], rest[:, 1], chains, base_spec["chains"])
            drawables.append(d)
        log(f"[build] {key}: pose parts added")

    if morph_tags and poses:
        for d in drawables:
            if d["id"] in morph_tags and "morph" not in d:
                d["morph"] = {"tag": d["id"], "key": None}

    for layer, vspec in variant_layers:
        anchor = vspec["after"]
        base_i = order.index(anchor) if anchor in order else len(order)
        add_drawable(layer, vspec["id"], base_i + vspec.get("sub", 0.5), vspec)

    # ------------------------------------------------ rig block
    hc = lm["head_center"]
    rig = {
        "head": {
            "center": hc, "pivot": lm["neck_pivot"], "radius": lm["head_radius"],
            "rigidPivot": [lm["center_x"], lm["neck_box"][3] - 0.15 * (lm["neck_box"][3] - lm["neck_box"][1])],
            "yawScale": 0.75, "pitchScale": 0.6, "rollScale": 0.55, "focal": lm["head_radius"][0] * 9,
            "shift": [lm["head_radius"][0] * 0.06, lm["head_radius"][1] * 0.04, lm["head_radius"][0] * 0.05],
        },
        "body": {
            "axisX": lm["center_x"], "pivot": [lm["center_x"], lm["hem_y"]],
            "twistScale": 1.6, "pitchScale": 0.8, "leanScale": 0.9,
            "breathLift": 3.0, "breathWiden": 0.006, "shift": lm["body_radius"] * 0.05,
            # rigid body: closer / further and sideways weight shift of the whole figure
            "stagePivot": [lm["center_x"], lm["shoulder_y"]], "depthScale": 0.05,
            "depthDrop": lm["head_radius"][1] * 0.03, "sway": lm["body_radius"] * 0.08,
        },
        "eyes": {},
        "brows": {},
        "landmarks": lm,
    }
    for side, tag in (("L", "l"), ("R", "r")):
        wt = f"eyewhite-{tag}"
        if wt in L:
            xs, up, low = eye_curves(L[wt])
            ir = alpha_bbox(L.get(f"irides-{tag}", L[wt]))
            wb = alpha_bbox(L[wt])
            rig["eyes"][side] = {
                "x0": float(xs[0]), "step": float(xs[1] - xs[0]), "up": up.tolist(), "low": low.tolist(),
                "margin": float((wb[2] - wb[0]) * 0.18),
                "irisCenter": [(ir[0] + ir[2]) / 2, (ir[1] + ir[3]) / 2],
                "irisRange": [float((wb[2] - wb[0]) * 0.11), float((wb[3] - wb[1]) * 0.08)],
            }
        bt = f"eyebrow-{tag}"
        if bt in L:
            bb = alpha_bbox(L[bt], 0.1)
            rig["brows"][side] = {"center": [(bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2],
                                  "range": float(lm["head_radius"][1] * 0.05), "maxAngle": 9.0,
                                  "blinkDrop": float(lm["head_radius"][1] * 0.012)}
    if poses:
        values: dict[str, set] = {}
        for k in poses:
            for kv in k.split(","):
                a_, v_ = kv.split(":")
                values.setdefault(a_, {0}).add(int(float(v_)))
        rig["keyforms"] = {"values": {a_: sorted(v_) for a_, v_ in values.items()}, "keys": {k: True for k in poses}}
    for k, v in recipe.get("rig", {}).items():
        rig.setdefault(k, {}).update(v)

    # chain point attributes
    for c in chains:
        pts = np.array(c["points"], np.float64)
        a = attr.attrs(pts[:, 0], pts[:, 1], c.get("kind", "body"))
        c["pid"] = {"hair_back": "back hair", "hair_front": "front hair", "headwear": "headwear"}.get(c.get("kind"))
        hcfg = recipe.get("rig", {}).get("head", {})
        if c.get("kind") in hcfg.get("hang_translate", []):
            a["ht"] = np.ones(len(pts))
        if c.get("kind") in hcfg.get("hang_rotate", []):
            a["head"] = np.ones(len(pts))
        if c.get("kind") in hcfg.get("body_rigid", []):
            zb0, tw0, le0, br0 = attr.body_attrs(np.array([lm["center_x"]]), np.array([lm["neck_pivot"][1]]), "head")
            for k_, v_ in (("zb", zb0), ("twist", tw0), ("lean", le0), ("breath", br0)):
                a[k_] = np.full(len(pts), float(v_[0]))
        if poses:
            owner = {"hair_back": "back hair", "hair_front": "front hair", "headwear": "headwear"}.get(c.get("kind"))
            if owner:
                c["keyforms"] = keyform_offsets(owner, pts[:, 0], pts[:, 1])
        c["attrs"] = {k: b64(v, "f32") for k, v in a.items()}
        c.pop("region", None)

    atlases = atlas.save(out_dir, "atlas")
    model = {
        "version": 1,
        "name": recipe["name"],
        "canvas": {"width": size[0], "height": size[1]},
        "atlases": atlases,
        "rig": rig,
        "drawables": drawables,
        "chains": [] if recipe.get("no_chains") else chains,
        "strands": [{k: v for k, v in sd.items() if not k.startswith("_")} for sd in strands],
        "expressions": recipe.get("expression_presets", []),
        "views": recipe.get("views", {}),
    }
    (out_dir / "model.json").write_text(json.dumps(model, ensure_ascii=False), encoding="utf-8")
    nverts = sum(d["vertexCount"] for d in drawables)
    log(f"[build] {len(drawables)} drawables, {nverts} vertices, {len(chains)} chains, "
        f"{len(atlases)} atlas page(s) -> {out_dir / 'model.json'}")
    # a flat composite for regression checks
    comp = composite([L[t] for t in order], size)
    Image.fromarray((np.clip(comp, 0, 1) * 255).astype(np.uint8), "RGBA").save(out_dir / "rest_composite.png")
    return model


def opacity_term(o, side: str = ""):
    """Recipe opacity entry -> runtime term. ``[param, keys]`` or a raw dict; '{S}' is the eye side."""
    if isinstance(o, dict):
        return json.loads(json.dumps(o).replace("{S}", side))
    return {"param": o[0].replace("{S}", side), "keys": o[1]}


def auto_deformer(df: dict, layer: Layer) -> dict:
    df = dict(df)
    if df.get("type") == "mouthForm" and "anchor" not in df:
        b = layer.bbox_src(0.3)
        df["anchor"] = [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2]
        df["halfWidth"] = (b[2] - b[0]) / 2
        df.setdefault("amount", (b[2] - b[0]) * 0.06)
    return df


def build_variants(vs: ex.VariantSet, ev: dict, L: dict[str, Layer], lm: dict, log) -> list[tuple[Layer, dict]]:
    """Cut eye / mouth patches and eyebrow line art out of the variant images."""
    res = []
    d = vs.density
    H, W = vs.reference.shape[:2]
    ys, xs = np.mgrid[0:H, 0:W].astype(np.float32)
    sx = vs.box[0] + (xs + 0.5) / d
    sy = vs.box[1] + (ys + 0.5) / d
    front_hair = L.get("front hair")

    def mask_of(tags):
        m = np.zeros((H, W), np.float32)
        for t in tags:
            if t in L:
                m = np.maximum(m, L[t].sample_alpha(sx, sy))
        return (m > 0.2).astype(np.float32)

    def region(tags, pad):
        bbs = [alpha_bbox(L[t], 0.1) for t in tags if t in L]
        x0 = min(b[0] for b in bbs) - pad
        y0 = min(b[1] for b in bbs) - pad
        x1 = max(b[2] for b in bbs) + pad
        y1 = max(b[3] for b in bbs) + pad
        return (x0, y0, x1, y1)

    rx = lm["head_radius"][0]
    for side, tag in (("L", "l"), ("R", "r")):
        eye_tags = [f"eyewhite-{tag}", f"irides-{tag}", f"eyelash-{tag}"]
        base = mask_of(eye_tags)
        reg = region(eye_tags, rx * 0.12)
        for name, spec in ev.get("eyes", {}).items():
            if name not in vs.images:
                continue
            layer = ex.patch_layer(vs, name, f"eye{side}-{name}", reg, base, exclude=front_hair,
                                   grow=spec.get("grow", 2.5), feather=spec.get("feather", 1.5))
            vspec = dict(spec)
            vspec.update({"id": f"eye{side}-{name}", "after": f"eyelash-{tag}", "kind": "facepart",
                          "sub": 0.5 + 0.01 * len(res)})
            vspec["deformers"] = [{"type": "blink", "eye": side, **spec.get("blink", {})}] if spec.get("blinks", True) else []
            vspec["opacity"] = [opacity_term(o, side) for o in spec["opacity"]]
            res.append((layer, vspec))
        bt = f"eyebrow-{tag}"
        if bt in L and "backing" in ev.get("brows", {}):
            bk = vs.images[ev["brows"]["backing"]]
            breg = region([bt], rx * 0.22)
            breg = (breg[0], breg[1] - rx * 0.1, breg[2], breg[3] + rx * 0.05)
            for name, spec in ev["brows"].get("variants", {}).items():
                if name not in vs.images:
                    continue
                layer = ex.line_layer(vs, name, f"brow{side}-{name}", breg, bk)
                if layer.alpha.max() < 0.2:
                    log(f"  ! no brow found in variant {name}")
                    continue
                vspec = {"id": f"brow{side}-{name}", "after": bt, "kind": "facepart", "z": 0.05,
                         "sub": 0.2 + 0.01 * len(res),
                         "deformers": [{"type": "brow", "side": side}],
                         "opacity": [opacity_term(o, side) for o in spec["opacity"]]}
                res.append((layer, vspec))
    if "mouth" in L:
        base = mask_of(["mouth"])
        reg = region(["mouth"], rx * 0.2)
        reg = (reg[0], reg[1] - rx * 0.05, reg[2], reg[3] + rx * 0.2)
        for name, spec in ev.get("mouth", {}).items():
            if name not in vs.images:
                continue
            layer = ex.patch_layer(vs, name, f"mouth-{name}", reg, base, grow=spec.get("grow", 2.0),
                                   feather=spec.get("feather", 1.5), largest=True)
            vspec = dict(spec)
            vspec.update({"id": f"mouth-{name}", "after": "mouth", "kind": "facepart", "z": 0.02,
                          "sub": 0.5 + 0.01 * len(res)})
            vspec["opacity"] = [opacity_term(o) for o in spec["opacity"]]
            dfs = []
            if spec.get("open"):
                ab = layer.bbox_src(0.5)
                dfs.append({"type": "mouthOpen", "anchor": [(ab[0] + ab[2]) / 2, ab[1] + (ab[3] - ab[1]) * 0.25],
                            "minScale": spec.get("minScale", 0.3)})
            vspec["deformers"] = dfs
            res.append((layer, vspec))
    log(f"[build] {len(res)} expression variant parts")
    return res
