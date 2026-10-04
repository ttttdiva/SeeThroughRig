"""Cut expression parts out of full-face variant images.

The variant images (closed eyes, open mouth, surprised ...) are aligned with a
known crop of the source illustration. For each variant we compare it to the
neutral reference and keep only the regions that changed, as feathered patches
(eyes, mouth) or as matted line art (eyebrows drawn over the bangs).
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image

from .layers import Layer


@dataclass
class VariantSet:
    box: tuple[float, float, float, float]   # source-space rect the images cover
    images: dict[str, np.ndarray]            # name -> HxWx3 float32
    reference: np.ndarray                    # neutral image at the same resolution

    @property
    def density(self) -> float:
        return self.reference.shape[1] / (self.box[2] - self.box[0])

    def src_to_px(self, x, y):
        d = self.density
        return (x - self.box[0]) * d, (y - self.box[1]) * d


def load_variants(box, paths: dict[str, str], source_rgb: np.ndarray, reference: str | None = None) -> VariantSet:
    imgs = {k: np.asarray(Image.open(p).convert("RGB"), np.float32) / 255.0 for k, p in paths.items()}
    if not imgs:
        raise ValueError("Expression image set must not be empty")
    if len({im.shape for im in imgs.values()}) != 1:
        raise ValueError("Expression images must have identical dimensions")
    h, w = next(iter(imgs.values())).shape[:2]
    x0, y0, x1, y1 = box
    if reference and reference in imgs:
        ref = imgs[reference]
    else:
        crop = source_rgb[int(y0):int(y1), int(x0):int(x1)]
        ref = cv2.resize(crop, (w, h), interpolation=cv2.INTER_CUBIC)
    return VariantSet(tuple(box), imgs, ref)


def _feather(mask: np.ndarray, px: float) -> np.ndarray:
    if px <= 0:
        return mask
    k = int(px * 3) | 1
    return cv2.GaussianBlur(mask, (k, k), px)


def diff_mask(a: np.ndarray, b: np.ndarray, thr: float = 0.08, blur: float = 1.5) -> np.ndarray:
    d = np.abs(cv2.GaussianBlur(a, (0, 0), blur) - cv2.GaussianBlur(b, (0, 0), blur)).max(axis=2)
    return (d > thr).astype(np.float32)


def patch_layer(vs: VariantSet, name: str, tag: str, region_src, base_mask_src: np.ndarray | None,
                exclude: Layer | None = None, grow: float = 6.0, feather: float = 3.0,
                thr: float = 0.08, largest: bool = False) -> Layer:
    """Opaque feathered patch of variant ``name`` inside ``region_src`` (source rect).

    base_mask_src: optional float mask (same grid as the variant image) of the
    neutral parts the patch must fully cover (so the neutral art can be hidden).
    exclude: layer drawn above (e.g. front hair) whose alpha is cut out.
    """
    img = vs.images[name]
    d = vs.density
    X0, Y0 = vs.src_to_px(region_src[0], region_src[1])
    X1, Y1 = vs.src_to_px(region_src[2], region_src[3])
    X0, Y0 = max(0, int(np.floor(X0))), max(0, int(np.floor(Y0)))
    X1, Y1 = min(img.shape[1], int(np.ceil(X1))), min(img.shape[0], int(np.ceil(Y1)))
    sub = img[Y0:Y1, X0:X1]
    ref = vs.reference[Y0:Y1, X0:X1]
    m = diff_mask(sub, ref, thr)
    # drop speckles, then grow and close holes
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    if largest:
        # only the main changed shape (e.g. the mouth); stray diff speckles would
        # drift visibly when the patch is scaled by a deformer
        n, lab, stats, _ = cv2.connectedComponentsWithStats((m > 0.5).astype(np.uint8), 8)
        if n > 1:
            areas = stats[1:, cv2.CC_STAT_AREA]
            keep = np.zeros_like(m)
            for i in np.nonzero(areas >= 0.25 * areas.max())[0] + 1:
                keep[lab == i] = 1
            m = keep
    if base_mask_src is not None:
        m = np.maximum(m, base_mask_src[Y0:Y1, X0:X1])
    g = max(1, int(grow * d))
    m = cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * g + 1, 2 * g + 1)))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * g + 1, 2 * g + 1)))
    # fill interior holes
    cnts, _ = cv2.findContours((m > 0.5).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    filled = np.zeros_like(m)
    cv2.drawContours(filled, cnts, -1, 1.0, thickness=-1)
    a = _feather(filled, feather * d)
    # keep the patch inside its region with a soft border
    border = np.zeros_like(a)
    b = max(2, int(feather * d))
    border[b:-b, b:-b] = 1
    a *= _feather(border, b / 2)
    origin = (vs.box[0] + X0 / d, vs.box[1] + Y0 / d)
    if exclude is not None:
        ys, xs = np.mgrid[0:a.shape[0], 0:a.shape[1]].astype(np.float32)
        ex = exclude.sample_alpha(origin[0] + (xs + 0.5) / d, origin[1] + (ys + 0.5) / d)
        a *= np.clip(1.0 - ex * 1.15, 0, 1)
    rgba = np.concatenate([sub, a[..., None]], axis=2).astype(np.float32)
    return Layer(tag, rgba, origin, d).crop_to_content()


def line_layer(vs: VariantSet, name: str, tag: str, region_src, backing: np.ndarray,
               line_rgb=(0.17, 0.2, 0.45), strength: float = 2.2, min_alpha: float = 0.12) -> Layer:
    """Matte dark line art (eyebrows) against a backing image without the lines.

    alpha = how much darker the variant is than the backing, normalised by the
    darkness of the line colour.
    """
    img = vs.images[name]
    d = vs.density
    X0, Y0 = [max(0, int(np.floor(v))) for v in vs.src_to_px(region_src[0], region_src[1])]
    X1, Y1 = vs.src_to_px(region_src[2], region_src[3])
    X1, Y1 = min(img.shape[1], int(np.ceil(X1))), min(img.shape[0], int(np.ceil(Y1)))
    sub = img[Y0:Y1, X0:X1]
    bk = backing[Y0:Y1, X0:X1]
    lum = lambda im: im @ np.array([0.3, 0.55, 0.15], np.float32)
    line = np.array(line_rgb, np.float32)
    dark = (lum(bk) - lum(sub)) / max(1e-3, (lum(bk) - lum(line[None, None])).mean())
    a = np.clip(dark * strength - min_alpha, 0, 1)
    a = cv2.morphologyEx(a, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    # keep only the largest thin structure(s) so hair strand edges are not picked up
    lab_n, lab, stats, _ = cv2.connectedComponentsWithStats((a > 0.25).astype(np.uint8), 8)
    keep = np.zeros_like(a)
    if lab_n > 1:
        order = np.argsort(stats[1:, cv2.CC_STAT_AREA])[::-1] + 1
        best = order[0]
        keep[lab == best] = 1
        for o in order[1:3]:
            if stats[o, cv2.CC_STAT_AREA] > 0.35 * stats[best, cv2.CC_STAT_AREA]:
                keep[lab == o] = 1
    keep = cv2.dilate(keep, np.ones((5, 5), np.uint8))
    a = a * keep
    # colour: the variant's own pixels, un-mixed from the backing where possible
    al = np.maximum(a[..., None], 1e-3)
    col = np.clip((sub - bk * (1 - al)) / al, 0, 1)
    col = np.where(a[..., None] > 0.05, col, line)
    col = 0.5 * col + 0.5 * line            # stabilise the colour of faint pixels
    rgba = np.concatenate([col, a[..., None]], axis=2).astype(np.float32)
    origin = (vs.box[0] + X0 / d, vs.box[1] + Y0 / d)
    return Layer(tag, rgba, origin, d).crop_to_content()

