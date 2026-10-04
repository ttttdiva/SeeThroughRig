"""Load See-through outputs and bring them into source-image coordinates.

A :class:`Layer` is an RGBA raster (straight alpha, float32 0..1) covering a
rectangle of the source image at a given pixel *density* (raster px per source
px). Head parts are kept at ~2x density so the face stays crisp when zoomed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


@dataclass
class Layer:
    tag: str
    rgba: np.ndarray            # H x W x 4 float32, straight alpha
    origin: tuple[float, float]  # source-space position of raster pixel (0,0)'s top-left corner
    density: float               # raster px per source px
    depth: np.ndarray | None = None   # H x W float32 (0 near .. 1 far) if available
    depth_median: float = 0.5
    meta: dict = field(default_factory=dict)

    @property
    def alpha(self) -> np.ndarray:
        return self.rgba[..., 3]

    @property
    def size_src(self) -> tuple[float, float]:
        h, w = self.rgba.shape[:2]
        return w / self.density, h / self.density

    def bbox_src(self, thr: float = 0.05) -> tuple[float, float, float, float] | None:
        ys, xs = np.nonzero(self.alpha > thr)
        if len(xs) == 0:
            return None
        ox, oy = self.origin
        d = self.density
        return (ox + xs.min() / d, oy + ys.min() / d, ox + (xs.max() + 1) / d, oy + (ys.max() + 1) / d)

    def to_raster(self, x: np.ndarray, y: np.ndarray):
        return (x - self.origin[0]) * self.density, (y - self.origin[1]) * self.density

    def sample_alpha(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        shape = np.shape(x)
        rx, ry = self.to_raster(np.asarray(x, np.float32).ravel(), np.asarray(y, np.float32).ravel())
        # cv2.remap wants 2D maps smaller than SHRT_MAX per side: fold into rows of 4096
        n = rx.size
        cols = 4096
        rows = max(1, -(-n // cols))
        pad = rows * cols - n
        mx = np.pad(rx - 0.5, (0, pad)).astype(np.float32).reshape(rows, cols)
        my = np.pad(ry - 0.5, (0, pad)).astype(np.float32).reshape(rows, cols)
        out = cv2.remap(self.alpha, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        return out.ravel()[:n].reshape(shape)

    def crop_to_content(self, pad: int = 4, thr: float = 0.004) -> "Layer":
        ys, xs = np.nonzero(self.alpha > thr)
        if len(xs) == 0:
            return self
        h, w = self.alpha.shape
        x0, x1 = max(0, xs.min() - pad), min(w, xs.max() + 1 + pad)
        y0, y1 = max(0, ys.min() - pad), min(h, ys.max() + 1 + pad)
        return Layer(self.tag, self.rgba[y0:y1, x0:x1].copy(),
                     (float(self.origin[0] + x0 / self.density), float(self.origin[1] + y0 / self.density)),
                     float(self.density),
                     None if self.depth is None else self.depth[y0:y1, x0:x1].copy(), self.depth_median, dict(self.meta))


def read_rgba(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGBA"), dtype=np.float32) / 255.0


def load_pass(opt_dir: Path, frame_to_src, density: float, src_rect: tuple[float, float, float, float]) -> dict[str, Layer]:
    """Load one See-through ``optimized`` directory.

    ``frame_to_src`` maps frame (inference canvas) pixel coords to source coords
    as (scale, offx, offy): src = frame * scale + off. Layers are resampled to
    the requested density and clipped to ``src_rect``.
    """
    info = json.loads((opt_dir / "info.json").read_text(encoding="utf-8"))
    scale, offx, offy = frame_to_src
    out: dict[str, Layer] = {}
    for tag, pinfo in info["parts"].items():
        p = opt_dir / f"{tag}.png"
        if not p.exists():
            continue
        img = read_rgba(p)
        if img[..., 3].max() < 0.05:
            continue
        x0f, y0f, x1f, y1f = pinfo["xyxy"]
        # source rect of this crop
        sx0, sy0 = x0f * scale + offx, y0f * scale + offy
        sx1, sy1 = x1f * scale + offx, y1f * scale + offy
        # clip to the allowed source region
        cx0, cy0 = max(sx0, src_rect[0]), max(sy0, src_rect[1])
        cx1, cy1 = min(sx1, src_rect[2]), min(sy1, src_rect[3])
        if cx1 <= cx0 or cy1 <= cy0:
            continue
        # integer raster grid at target density, aligned to the source pixel grid
        gx0, gy0 = np.floor(cx0 * density) / density, np.floor(cy0 * density) / density
        W = int(np.ceil((cx1 - gx0) * density))
        H = int(np.ceil((cy1 - gy0) * density))
        # affine: raster(dst) -> crop pixel(src)
        k = 1.0 / (density * scale)      # crop px per raster px
        tx = (gx0 - sx0) / scale
        ty = (gy0 - sy0) / scale
        M = np.array([[k, 0, tx + 0.5 * k - 0.5], [0, k, ty + 0.5 * k - 0.5]], np.float32)
        interp = cv2.INTER_CUBIC if k < 1 else cv2.INTER_AREA
        prem = img.copy()
        prem[..., :3] *= prem[..., 3:4]
        warped = cv2.warpAffine(prem, M, (W, H), flags=interp | cv2.WARP_INVERSE_MAP,
                                borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
        warped = np.clip(warped, 0, 1)
        a = warped[..., 3:4]
        rgb = np.where(a > 1e-4, warped[..., :3] / np.maximum(a, 1e-4), 0)
        rgba = np.concatenate([np.clip(rgb, 0, 1), a], axis=2).astype(np.float32)
        depth = None
        dp = opt_dir / f"{tag}_depth.png"
        if dp.exists():
            d = np.asarray(Image.open(dp).convert("L"), np.float32) / 255.0
            depth = cv2.warpAffine(d, M, (W, H), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                                   borderMode=cv2.BORDER_REPLICATE)
        out[tag] = Layer(tag, rgba, (float(gx0), float(gy0)), density, depth,
                         float(pinfo.get("depth_median", 0.5)), {"pass": str(opt_dir)})
    return out


def full_pass_transform(frame_size: int, image_size: tuple[int, int]):
    w, h = image_size
    side = max(w, h)
    s = side / frame_size
    return s, -(side - w) / 2, -(side - h) / 2


def head_pass_transform(frame_size: int, box: list[int]):
    x0, y0, x1, y1 = box
    side = max(x1 - x0, y1 - y0)
    s = side / frame_size
    return s, x0 - (side - (x1 - x0)) / 2, y0 - (side - (y1 - y0)) / 2


def layer_from_image(tag: str, path: Path, origin=(0.0, 0.0), density: float = 1.0) -> Layer:
    return Layer(tag, read_rgba(path), origin, density).crop_to_content()


def resample(layer: Layer, density: float) -> Layer:
    if abs(layer.density - density) < 1e-6:
        return layer
    h, w = layer.alpha.shape
    f = density / layer.density
    W, H = max(1, int(round(w * f))), max(1, int(round(h * f)))
    prem = layer.rgba.copy()
    prem[..., :3] *= prem[..., 3:4]
    interp = cv2.INTER_CUBIC if f > 1 else cv2.INTER_AREA
    r = np.clip(cv2.resize(prem, (W, H), interpolation=interp), 0, 1)
    a = r[..., 3:4]
    rgb = np.where(a > 1e-4, r[..., :3] / np.maximum(a, 1e-4), 0)
    depth = None if layer.depth is None else cv2.resize(layer.depth, (W, H), interpolation=cv2.INTER_LINEAR)
    return Layer(layer.tag, np.concatenate([np.clip(rgb, 0, 1), a], 2).astype(np.float32), layer.origin, density,
                 depth, layer.depth_median, dict(layer.meta))
