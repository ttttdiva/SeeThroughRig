"""Keyforms from drawn (generated) pose images.

A keyform image shows the character at one parameter value (e.g. head turned
30 degrees) in the same framing as a crop of the source illustration. We
estimate where every pixel went (dense optical flow, neutral -> keyform) and
turn it into per-vertex offsets for each See-through layer:

* only pixels where the layer is actually visible in the original drive it,
  so the bangs follow the bangs and the face under them follows the face,
* the sparse, noisy flow is turned into a smooth displacement field per layer
  (normalised convolution at several scales),
* small rigid parts (eyes, brows, mouth, nose) get a single similarity
  transform so their drawn shapes never bend.

At runtime the offsets are blended by the parameter value exactly like Live2D
keyforms (linear between keys, 2D keys combined per axis).
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image

from .layers import Layer


@dataclass
class KeyFrame:
    """A keyform image in the crop frame of the source."""
    box: tuple[float, float, float, float]   # source rect covered by the images
    size: int                                # images are size x size

    @property
    def scale(self) -> float:                # image px per source px
        return self.size / (self.box[2] - self.box[0])

    def to_img(self, x, y):
        return (x - self.box[0]) * self.scale, (y - self.box[1]) * self.scale


def load_rgb(path, size: int) -> np.ndarray:
    im = Image.open(path).convert("RGB")
    if im.size != (size, size):
        im = im.resize((size, size), Image.LANCZOS)
    return np.asarray(im, np.float32) / 255.0


def dense_flow(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Flow a -> b (H x W x 2, in px) and a confidence map (forward/backward check)."""
    ga = cv2.cvtColor((a * 255).astype(np.uint8), cv2.COLOR_RGB2GRAY)
    gb = cv2.cvtColor((b * 255).astype(np.uint8), cv2.COLOR_RGB2GRAY)
    dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_ULTRAFAST)
    dis.setFinestScale(0)
    dis.setPatchSize(12)
    dis.setPatchStride(4)
    dis.setGradientDescentIterations(25)
    dis.setVariationalRefinementIterations(10)
    dis.setVariationalRefinementAlpha(20.0)
    dis.setUseSpatialPropagation(True)
    fwd = dis.calc(ga, gb, None)
    bwd = dis.calc(gb, ga, None)
    h, w = ga.shape
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    bx = cv2.remap(bwd[..., 0], xs + fwd[..., 0], ys + fwd[..., 1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    by = cv2.remap(bwd[..., 1], xs + fwd[..., 0], ys + fwd[..., 1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    err = np.hypot(fwd[..., 0] + bx, fwd[..., 1] + by)
    # photometric check too: warped keyform must look like the neutral pixel
    wb = cv2.remap(b, xs + fwd[..., 0], ys + fwd[..., 1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    photo = np.abs(wb - a).mean(axis=2)
    conf = np.exp(-(err / 2.0) ** 2) * np.exp(-(photo / 0.08) ** 2)
    # flat areas carry no information
    gx = cv2.Sobel(ga, cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(ga, cv2.CV_32F, 0, 1)
    tex = cv2.GaussianBlur(np.hypot(gx, gy), (0, 0), 3)
    conf *= np.clip(tex / 40.0, 0.05, 1.0)
    return fwd, conf.astype(np.float32)


def warp_image(img: np.ndarray, flow_to: np.ndarray) -> np.ndarray:
    """Backward-warp: out(p) = img(p + flow_to(p))."""
    h, w = img.shape[:2]
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    return cv2.remap(img, xs + flow_to[..., 0], ys + flow_to[..., 1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def visibility(layers: list[Layer], idx: int, sx: np.ndarray, sy: np.ndarray) -> np.ndarray:
    """How much of layer idx is visible (not covered by later layers) at source points."""
    L = layers[idx]
    a = L.sample_alpha(sx, sy)
    cover = np.zeros_like(a)
    for up in layers[idx + 1:]:
        if up.meta.get("no_occlude"):
            continue
        c = up.sample_alpha(sx, sy)
        cover = cover + c * (1 - cover)
    return a * (1 - cover)


def smooth_field(px, py, vx, vy, w, qx, qy, sigmas=(6, 12, 24, 48, 96)):
    """Normalised-convolution interpolation of scattered vectors (px,py)->(vx,vy)
    with weights w, evaluated at query points (qx,qy). Coarser scales fill in
    where finer scales have no support."""
    qx = np.asarray(qx, np.float64)
    qy = np.asarray(qy, np.float64)
    outx = np.zeros(qx.shape)
    outy = np.zeros(qx.shape)
    have = np.zeros(qx.shape)
    if len(px) == 0:
        return outx, outy
    # grid accumulation for speed
    x0, y0 = min(px.min(), qx.min()) - 1, min(py.min(), qy.min()) - 1
    x1, y1 = max(px.max(), qx.max()) + 2, max(py.max(), qy.max()) + 2
    cell = 2.0
    W, H = int((x1 - x0) / cell) + 1, int((y1 - y0) / cell) + 1
    gi = ((px - x0) / cell).astype(int)
    gj = ((py - y0) / cell).astype(int)
    acc_w = np.zeros((H, W), np.float64)
    acc_x = np.zeros((H, W), np.float64)
    acc_y = np.zeros((H, W), np.float64)
    np.add.at(acc_w, (gj, gi), w)
    np.add.at(acc_x, (gj, gi), w * vx)
    np.add.at(acc_y, (gj, gi), w * vy)
    qi = np.clip((qx - x0) / cell, 0, W - 1).astype(np.float32)
    qj = np.clip((qy - y0) / cell, 0, H - 1).astype(np.float32)
    for s in sigmas:
        k = s / cell
        bw = cv2.GaussianBlur(acc_w.astype(np.float32), (0, 0), k)
        bx = cv2.GaussianBlur(acc_x.astype(np.float32), (0, 0), k)
        by = cv2.GaussianBlur(acc_y.astype(np.float32), (0, 0), k)
        sw = cv2.remap(bw, qi.reshape(-1, 1), qj.reshape(-1, 1), cv2.INTER_LINEAR).reshape(qx.shape)
        sx = cv2.remap(bx, qi.reshape(-1, 1), qj.reshape(-1, 1), cv2.INTER_LINEAR).reshape(qx.shape)
        sy = cv2.remap(by, qi.reshape(-1, 1), qj.reshape(-1, 1), cv2.INTER_LINEAR).reshape(qx.shape)
        # support relative to a fully covered neighbourhood at this scale
        support = np.clip(sw / (0.05 / (2 * np.pi * k * k) + 1e-12) * 0 + sw * 2 * np.pi * k * k / 3.0, 0, 1)
        take = np.clip(support, 0, 1) * (1 - have)
        ok = sw > 1e-9
        outx[ok] += take[ok] * sx[ok] / sw[ok]
        outy[ok] += take[ok] * sy[ok] / sw[ok]
        have += take
        if have.min() > 0.999:
            break
    # anything still unsupported keeps the mean motion
    rest = 1 - have
    if rest.max() > 1e-6:
        mw = w.sum() + 1e-9
        outx += rest * (w * vx).sum() / mw
        outy += rest * (w * vy).sum() / mw
    return outx, outy


def fit_similarity(px, py, vx, vy, w):
    """Weighted least-squares similarity transform (scale+rotation+translation)
    mapping p -> p + v. Returns a function f(x, y) -> (dx, dy)."""
    sw = w.sum() + 1e-9
    mx, my = (w * px).sum() / sw, (w * py).sum() / sw
    tx, ty = (w * (px + vx)).sum() / sw, (w * (py + vy)).sum() / sw
    ax, ay = px - mx, py - my
    bx, by = px + vx - tx, py + vy - ty
    den = (w * (ax * ax + ay * ay)).sum() + 1e-9
    a = (w * (ax * bx + ay * by)).sum() / den
    b = (w * (ax * by - ay * bx)).sum() / den
    # keep it near rigid (drawn parts should not grow or shrink much)
    s = np.hypot(a, b)
    if s > 0:
        s_c = np.clip(s, 0.85, 1.15)
        a, b = a * s_c / s, b * s_c / s

    def f(x, y):
        x = np.asarray(x, np.float64)
        y = np.asarray(y, np.float64)
        nx = tx + a * (x - mx) - b * (y - my)
        ny = ty + b * (x - mx) + a * (y - my)
        return nx - x, ny - y
    return f


class KeyformSolver:
    """Per-layer displacement fields for one keyform image."""

    def __init__(self, kf: KeyFrame, neutral: np.ndarray, pose: np.ndarray, ordered: list[Layer]):
        self.kf = kf
        self.flow, self.conf = dense_flow(neutral, pose)
        self.ordered = ordered
        self.index = {L.tag: i for i, L in enumerate(ordered)}
        self._cache: dict[str, object] = {}

    def samples(self, tag: str, step: float = 3.0):
        """Visible flow samples of a layer (source coords, source-px vectors, weights)."""
        idx = self.index[tag]
        L = self.ordered[idx]
        b = L.bbox_src(0.05)
        kx0, ky0, kx1, ky1 = self.kf.box
        x0, y0 = max(b[0], kx0), max(b[1], ky0)
        x1, y1 = min(b[2], kx1), min(b[3], ky1)
        if x1 <= x0 or y1 <= y0:
            return None
        ys, xs = np.mgrid[y0:y1:step, x0:x1:step]
        xs, ys = xs.ravel(), ys.ravel()
        vis = visibility(self.ordered, idx, xs, ys)
        # stay off the silhouette: flow there belongs to the neighbour
        ker = self.ordered[idx]
        inner = ker.sample_alpha(xs + step, ys) * ker.sample_alpha(xs - step, ys) * \
            ker.sample_alpha(xs, ys + step) * ker.sample_alpha(xs, ys - step)
        ix, iy = self.kf.to_img(xs, ys)
        ix = ix.astype(np.float32)
        iy = iy.astype(np.float32)
        fx = cv2.remap(self.flow[..., 0], ix.reshape(-1, 1), iy.reshape(-1, 1), cv2.INTER_LINEAR).ravel()
        fy = cv2.remap(self.flow[..., 1], ix.reshape(-1, 1), iy.reshape(-1, 1), cv2.INTER_LINEAR).ravel()
        cf = cv2.remap(self.conf, ix.reshape(-1, 1), iy.reshape(-1, 1), cv2.INTER_LINEAR).ravel()
        w = np.clip(vis, 0, 1) ** 2 * cf * np.clip(inner, 0, 1)
        keep = w > 0.02
        s = 1.0 / self.kf.scale
        return xs[keep], ys[keep], fx[keep] * s, fy[keep] * s, w[keep]

    def field(self, tag: str, rigid: bool = False, donors: list[str] | None = None):
        """Function (x, y) -> (dx, dy) in source px for a layer."""
        key = (tag, rigid)
        if key in self._cache:
            return self._cache[key]
        parts = [tag] + list(donors or [])
        acc = [self.samples(t) for t in parts if t in self.index]
        acc = [a for a in acc if a is not None and len(a[0])]
        if not acc:
            f = lambda x, y: (np.zeros(np.shape(x)), np.zeros(np.shape(x)))  # noqa: E731
        else:
            px, py, vx, vy, w = [np.concatenate(v) for v in zip(*acc)]
            if rigid:
                f = fit_similarity(px, py, vx, vy, w)
            else:
                def f(x, y, _d=(px, py, vx, vy, w)):
                    return smooth_field(*_d, x, y)
        self._cache[key] = f
        return f

    def box_fade(self, x, y, margin: float = 40.0):
        """1 inside the keyform box, fading to 0 at its border (outside: no motion)."""
        x0, y0, x1, y1 = self.kf.box
        fx = np.clip(np.minimum(x - x0, x1 - x) / margin, 0, 1)
        fy = np.clip(np.minimum(y - y0, y1 - y) / margin, 0, 1)
        # the top edge of the crop is the top of the image: no fade there
        if y0 <= 0.5:
            fy = np.clip((y1 - y) / margin, 0, 1)
        return fx * fy


def align_to_neutral(neutral: np.ndarray, pose: np.ndarray, body_from: float = 0.62) -> tuple[np.ndarray, np.ndarray]:
    """Register the pose image to the neutral one on the part that must not move
    (shoulders / chest below ``body_from`` of the height) with a similarity
    transform, so generation drift (small shift / zoom) is not read as motion.
    Returns (aligned pose image, 2x3 matrix pose->neutral)."""
    h, w = neutral.shape[:2]
    g1 = cv2.cvtColor((neutral * 255).astype(np.uint8), cv2.COLOR_RGB2GRAY)
    g2 = cv2.cvtColor((pose * 255).astype(np.uint8), cv2.COLOR_RGB2GRAY)
    mask = np.zeros((h, w), np.uint8)
    mask[int(h * body_from):, int(w * 0.18):int(w * 0.82)] = 255
    sift = cv2.SIFT_create(4000)
    k1, d1 = sift.detectAndCompute(g1, mask)
    k2, d2 = sift.detectAndCompute(g2, mask)
    m = cv2.BFMatcher().knnMatch(d2, d1, k=2)
    good = [a for a, b in m if a.distance < 0.75 * b.distance]
    src = np.float32([k2[a.queryIdx].pt for a in good])
    dst = np.float32([k1[a.trainIdx].pt for a in good])
    M, inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=3.0)
    if M is None:
        M = np.float32([[1, 0, 0], [0, 1, 0]])
    out = cv2.warpAffine(pose, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    return out, M


# ---------------------------------------------------------------- layer-pair matching
#
# Optical flow between whole pictures fails where parts overlap and move
# differently (bangs over the face, face over the back hair). Decomposing the
# pose image with See-through as well gives us the *same parts* in both poses,
# and matching part-to-part has no occlusion problem.

RIGID_TAGS = {"nose", "mouth", "ears-l", "ears-r", "eyebrow-l", "eyebrow-r"}
EYE_GROUPS = {"eye-l": ["eyewhite-l", "irides-l", "eyelash-l"], "eye-r": ["eyewhite-r", "irides-r", "eyelash-r"]}


def load_canvases(opt_dir, frame: int) -> dict[str, np.ndarray]:
    """See-through 'optimized' layers pasted onto full frame-size RGBA canvases."""
    import json
    from pathlib import Path
    opt_dir = Path(opt_dir)
    info = json.loads((opt_dir / "info.json").read_text(encoding="utf-8"))
    out = {}
    for tag, p in info["parts"].items():
        f = opt_dir / f"{tag}.png"
        if not f.exists():
            continue
        im = np.asarray(Image.open(f).convert("RGBA"), np.float32) / 255.0
        if im[..., 3].max() < 0.1:
            continue
        x0, y0, x1, y1 = p["xyxy"]
        can = np.zeros((frame, frame, 4), np.float32)
        h, w = im.shape[:2]
        can[y0:y0 + h, x0:x0 + w] = im[: frame - y0, : frame - x0]
        out[tag] = can
    for g, tags in EYE_GROUPS.items():
        parts = [out[t] for t in tags if t in out]
        if parts:
            acc = np.zeros_like(parts[0])
            for pimg in parts:
                a = pimg[..., 3:4]
                acc[..., :3] = pimg[..., :3] * a + acc[..., :3] * (1 - a)
                acc[..., 3:4] = a + acc[..., 3:4] * (1 - a)
            out[g] = acc
    return out


def _gray(can: np.ndarray, bg: float = 0.5) -> np.ndarray:
    a = can[..., 3:4]
    rgb = can[..., :3] * a + bg * (1 - a)
    g = cv2.cvtColor((np.clip(rgb, 0, 1) * 255).astype(np.uint8), cv2.COLOR_RGB2GRAY)
    return g


def affine_from_masks(a: np.ndarray, b: np.ndarray, rigid: bool) -> np.ndarray:
    """2x3 matrix M with a(p) ~ b(M p), from the alpha masks (ECC on blurred alpha)."""
    ya, xa = np.nonzero(a > 0.3)
    yb, xb = np.nonzero(b > 0.3)
    if len(xa) < 20 or len(xb) < 20:
        return np.float32([[1, 0, 0], [0, 1, 0]])
    # initial guess: match centroids and spread
    sa = np.sqrt(xa.var() + ya.var()) + 1e-6
    sb = np.sqrt(xb.var() + yb.var()) + 1e-6
    s = sb / sa if rigid else 1.0
    M = np.float32([[s, 0, xb.mean() - s * xa.mean()], [0, s, yb.mean() - s * ya.mean()]])
    A = cv2.GaussianBlur(a.astype(np.float32), (0, 0), 3)
    B = cv2.GaussianBlur(b.astype(np.float32), (0, 0), 3)
    mode = cv2.MOTION_EUCLIDEAN if rigid else cv2.MOTION_AFFINE
    try:
        # findTransformECC estimates warp such that template(x) ~ input(W x)
        _, M = cv2.findTransformECC(A, B, M.astype(np.float32), mode,
                                    (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 200, 1e-6), None, 5)
    except cv2.error:
        pass
    return M.astype(np.float32)


def match_layer(na: np.ndarray, kb: np.ndarray, mode: str):
    """Displacement field (H,W,2) mapping neutral layer pixels to pose layer
    positions, and a weight map (where it is trustworthy).
    mode: 'rigid' (rotation+scale+shift), 'affine', or 'dense' (affine + smooth residual)."""
    H, W = na.shape[:2]
    M = affine_from_masks(na[..., 3], kb[..., 3], mode == "rigid")
    ys, xs = np.mgrid[0:H, 0:W].astype(np.float32)
    ax = M[0, 0] * xs + M[0, 1] * ys + M[0, 2]
    ay = M[1, 0] * xs + M[1, 1] * ys + M[1, 2]
    if mode in ("rigid", "affine"):
        return np.stack([ax - xs, ay - ys], -1), (na[..., 3] > 0.3).astype(np.float32), M
    # pose layer pulled back into the neutral frame by the affine
    kw = cv2.remap(kb, ax, ay, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
    g1, g2 = _gray(na), _gray(kw)
    dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
    dis.setVariationalRefinementAlpha(30.0)
    dis.setVariationalRefinementIterations(10)
    r = dis.calc(g1, g2, None)
    # keep the residual smooth and modest: the affine already did the big move
    r = cv2.GaussianBlur(r, (0, 0), 6)
    px, py = xs + r[..., 0], ys + r[..., 1]
    fx = M[0, 0] * px + M[0, 1] * py + M[0, 2]
    fy = M[1, 0] * px + M[1, 1] * py + M[1, 2]
    # confidence: does the matched pose pixel look like the neutral one?
    kr = cv2.remap(kw, px, py, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
    diff = np.abs(kr[..., :3] * kr[..., 3:4] - na[..., :3] * na[..., 3:4]).mean(-1)
    conf = np.exp(-(diff / 0.1) ** 2) * (na[..., 3] > 0.3) * (kr[..., 3] > 0.3)
    return np.stack([fx - xs, fy - ys], -1), conf.astype(np.float32), M


def smooth_dense(field: np.ndarray, w: np.ndarray, support: np.ndarray, sigmas=(4, 10, 24, 60)) -> np.ndarray:
    """Normalised convolution of a dense field, filled from coarser scales."""
    out = np.zeros_like(field)
    have = np.zeros(w.shape, np.float32)
    for s in sigmas:
        bw = cv2.GaussianBlur(w, (0, 0), s)
        bx = cv2.GaussianBlur(field[..., 0] * w, (0, 0), s)
        by = cv2.GaussianBlur(field[..., 1] * w, (0, 0), s)
        full = cv2.GaussianBlur(np.ones_like(w) * (w.max() if w.max() > 0 else 1), (0, 0), s)
        sup = np.clip(bw / (full * 0.15 + 1e-9), 0, 1)
        take = sup * (1 - have)
        ok = bw > 1e-6
        out[..., 0][ok] += take[ok] * bx[ok] / bw[ok]
        out[..., 1][ok] += take[ok] * by[ok] / bw[ok]
        have += take
    rest = 1 - have
    if w.sum() > 0:
        out[..., 0] += rest * (field[..., 0] * w).sum() / w.sum()
        out[..., 1] += rest * (field[..., 1] * w).sum() / w.sum()
    return out


class PoseKeyform:
    """Part-to-part correspondence between the neutral and one pose decomposition."""

    def __init__(self, neutral_dir, pose_dir, box, frame: int = 1024):
        self.box = tuple(box)
        self.frame = frame
        self.scale = frame / (box[2] - box[0])
        self.N = load_canvases(neutral_dir, frame)
        self.K = load_canvases(pose_dir, frame)
        self._fields: dict[str, np.ndarray | None] = {}
        self.repair_front_hair()

    def repair_front_hair(self, grow: int = 6):
        """See-through sometimes files a lock of the bangs under 'back hair' in
        the pose image. Where the neutral bangs land in the pose (via the inverse
        field) and the pose's back hair has pixels, move them to the front hair."""
        if not all(t in d for t in ("front hair", "back hair") for d in (self.N, self.K)):
            return
        inv = self.inverse_field("front hair")
        H, W = inv.shape[:2]
        ys, xs = np.mgrid[0:H, 0:W].astype(np.float32)
        na = cv2.remap(self.N["front hair"][..., 3], xs + inv[..., 0], ys + inv[..., 1], cv2.INTER_LINEAR,
                       borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        m = cv2.dilate((na > 0.5).astype(np.uint8),
                       cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * grow + 1, 2 * grow + 1)))
        m = cv2.GaussianBlur(m.astype(np.float32), (0, 0), 1.5)
        fh = self.K["front hair"]
        bh = self.K["back hair"]
        # never over the face: behind the face the back hair is only inpainted
        # filler and would cover the eyes if it became "front" hair
        face = np.zeros((H, W), np.float32)
        for t in ("face", "ears-l", "ears-r", "neck", "eye-l", "eye-r"):
            if t in self.K:
                face = np.maximum(face, self.K[t][..., 3])
        face = cv2.dilate((face > 0.2).astype(np.uint8), np.ones((9, 9), np.uint8)).astype(np.float32)
        take = bh[..., 3] * m * (1 - fh[..., 3]) * (1 - face)
        out = fh.copy()
        prem = fh[..., :3] * fh[..., 3:4] + bh[..., :3] * take[..., None]
        out[..., 3] = np.clip(fh[..., 3] + take, 0, 1)
        out[..., :3] = np.where(out[..., 3:4] > 1e-4, prem / np.maximum(out[..., 3:4], 1e-4), 0)
        self.K["front hair"] = out.astype(np.float32)
        self._fields.clear()

    @staticmethod
    def mode(tag: str) -> str:
        if tag in EYE_GROUPS:
            return "affine"
        if tag in RIGID_TAGS:
            return "rigid"
        return "dense"

    def field(self, tag: str):
        if tag in self._fields:
            return self._fields[tag]
        f = None
        if tag in self.N and tag in self.K:
            d, conf, _ = match_layer(self.N[tag], self.K[tag], self.mode(tag))
            if self.mode(tag) == "dense":
                d = smooth_dense(d, conf, self.N[tag][..., 3])
            f = d.astype(np.float32)
        self._fields[tag] = f
        return f

    def inverse_field(self, tag: str):
        """Field on the POSE layer pointing back to the neutral pose."""
        key = "inv:" + tag
        if key in self._fields:
            return self._fields[key]
        f = None
        if tag in self.N and tag in self.K:
            d, conf, _ = match_layer(self.K[tag], self.N[tag], self.mode(tag))
            if self.mode(tag) == "dense":
                d = smooth_dense(d, conf, self.K[tag][..., 3])
            f = d.astype(np.float32)
        self._fields[key] = f
        return f

    def sample(self, f, x, y):
        qx = ((np.asarray(x) - self.box[0]) * self.scale).astype(np.float32)
        qy = ((np.asarray(y) - self.box[1]) * self.scale).astype(np.float32)
        dx = remap_points(f[..., 0], qx, qy).reshape(np.shape(x)) / self.scale
        dy = remap_points(f[..., 1], qx, qy).reshape(np.shape(x)) / self.scale
        return dx, dy

    def pose_layer(self, tag: str, name: str) -> Layer | None:
        """The pose image's own part as a Layer in source coordinates."""
        if tag not in self.K:
            return None
        img = self.K[tag].copy()
        img[..., 3][img[..., 3] < 0.06] = 0      # See-through leaves faint alpha noise everywhere
        L = Layer(name, img, (float(self.box[0]), float(self.box[1])), float(self.scale))
        return L.crop_to_content(pad=4, thr=0.06)

    def offsets(self, tags: list[str], x, y, fade_margin: float = 40.0):
        """Source-px offsets at source points for the first tag that has a field."""
        x = np.asarray(x, np.float64)
        y = np.asarray(y, np.float64)
        f = None
        for t in tags:
            f = self.field(t)
            if f is not None:
                break
        if f is None:
            return np.zeros(x.shape), np.zeros(x.shape)
        qx = ((x - self.box[0]) * self.scale).astype(np.float32)
        qy = ((y - self.box[1]) * self.scale).astype(np.float32)
        dx = remap_points(f[..., 0], qx, qy).reshape(x.shape) / self.scale
        dy = remap_points(f[..., 1], qx, qy).reshape(x.shape) / self.scale
        x0, y0, x1, y1 = self.box
        fx = np.clip(np.minimum(x - x0, x1 - x) / fade_margin, 0, 1)
        if y0 <= 0.5:
            fy = np.clip((y1 - y) / fade_margin, 0, 1)
        else:
            fy = np.clip(np.minimum(y - y0, y1 - y) / fade_margin, 0, 1)
        return dx * fx * fy, dy * fx * fy


def source_tags(drawable_id: str) -> list[str]:
    """Which decomposed part(s) drive a drawable, in order of preference."""
    t = drawable_id
    for suf in ("-front", "-back"):
        if t.endswith(suf):
            t = t[: -len(suf)]
    for pre, side in (("eyeL-", "l"), ("eyeR-", "r")):
        if t.startswith(pre):
            return [f"eye-{side}"]
    for pre, side in (("browL-", "l"), ("browR-", "r")):
        if t.startswith(pre):
            return [f"eyebrow-{side}", f"eye-{side}"]
    if t.startswith("mouth"):
        return ["mouth", "face"]
    for base in ("eyewhite", "irides", "eyelash"):
        if t.startswith(base + "-"):
            return [f"eye-{t[-1]}"]
    if t.startswith("eyebrow-"):
        return [t, f"eye-{t[-1]}"]
    if t.startswith("ears"):
        return [t, "face"]
    return {
        "headwear": ["headwear", "front hair"],
        "neck": ["neck", "face"],
        "nose": ["nose", "face"],
        "bottomwear": ["topwear"],
        "handwear-l": ["handwear-l", "topwear"],
        "handwear-r": ["handwear-r", "topwear"],
    }.get(t, [t])


def remap_points(img: np.ndarray, qx: np.ndarray, qy: np.ndarray, border=cv2.BORDER_REPLICATE) -> np.ndarray:
    """Bilinear sample img at many points (works around cv2.remap's 32767 limit)."""
    qx = np.asarray(qx, np.float32).ravel()
    qy = np.asarray(qy, np.float32).ravel()
    n = qx.size
    cols = 4096
    rows = max(1, -(-n // cols))
    pad = rows * cols - n
    mx = np.pad(qx, (0, pad)).reshape(rows, cols)
    my = np.pad(qy, (0, pad)).reshape(rows, cols)
    out = cv2.remap(np.ascontiguousarray(img, np.float32), mx, my, cv2.INTER_LINEAR, borderMode=border)
    return out.ravel()[:n]


# ---------------------------------------------------------------- fitted keyforms
#
# Deformation fields optimised by fitkeys_torch.py: for every motion group a
# backward warp (output -> neutral) on the keyform frame. Vertex offsets are
# obtained by inverting it with a fixed-point iteration.

FIT_GROUP = {
    "back hair": "back", "headwear": "headwear", "ears-r": "earR", "ears-l": "earL", "neck": "neck",
    "face": "face", "nose": "face", "mouth": "face", "front hair": "front",
    "eyewhite-r": "eyeR", "irides-r": "eyeR", "eyelash-r": "eyeR",
    "eyewhite-l": "eyeL", "irides-l": "eyeL", "eyelash-l": "eyeL",
    "eyebrow-r": "browR", "eyebrow-l": "browL",
}


def fit_group(drawable_id: str) -> str | None:
    t = drawable_id.split("@")[0]
    for suf in ("-front", "-back"):
        if t.endswith(suf):
            t = t[: -len(suf)]
    for pre, g in (("eyeL-", "eyeL"), ("eyeR-", "eyeR"), ("browL-", "browL"), ("browR-", "browR"), ("mouth", "face")):
        if t.startswith(pre):
            return g
    return FIT_GROUP.get(t)


class FittedKeyform:
    def __init__(self, npz_path, box, frame: int = 1024):
        d = np.load(npz_path)
        self.fields = {k: d[k] for k in d.files if k != "name"}
        self.box = tuple(box)
        self.frame = frame

    def offsets(self, group: str | None, x, y, fade_margin: float = 40.0, iters: int = 30):
        x = np.asarray(x, np.float64)
        y = np.asarray(y, np.float64)
        if group is None or group not in self.fields:
            return np.zeros(x.shape), np.zeros(x.shape)
        f = self.fields[group]
        G = f.shape[0]
        x0, y0, x1, y1 = self.box
        W = x1 - x0
        nsx = (x - x0) / W * 2 - 1
        nsy = (y - y0) / W * 2 - 1
        # solve o + b(o) = s with Newton's method (a plain fixed-point iteration
        # diverges where a part is compressed by more than 2x, e.g. the far eye)
        h = 2.0 / G
        gx = np.gradient(f, axis=1) / h           # d b / d x  (normalised units)
        gy = np.gradient(f, axis=0) / h           # d b / d y

        def samp(arr, ox_, oy_):
            px = ((ox_ + 1) / 2 * G - 0.5).astype(np.float32)
            py = ((oy_ + 1) / 2 * G - 0.5).astype(np.float32)
            return remap_points(arr, px, py).reshape(x.shape)

        ox, oy = nsx.copy(), nsy.copy()
        for _ in range(iters):
            rx = ox + samp(f[..., 0], ox, oy) - nsx
            ry = oy + samp(f[..., 1], ox, oy) - nsy
            j11 = 1 + samp(gx[..., 0], ox, oy)
            j12 = samp(gy[..., 0], ox, oy)
            j21 = samp(gx[..., 1], ox, oy)
            j22 = 1 + samp(gy[..., 1], ox, oy)
            det = j11 * j22 - j12 * j21
            det = np.where(np.abs(det) < 1e-3, 1e-3, det)
            ox = ox - (j22 * rx - j12 * ry) / det
            oy = oy - (-j21 * rx + j11 * ry) / det
            if max(np.abs(rx).max(initial=0), np.abs(ry).max(initial=0)) < 1e-5:
                break
        dx = (ox - nsx) * W / 2
        dy = (oy - nsy) * W / 2
        fx = np.clip(np.minimum(x - x0, x1 - x) / fade_margin, 0, 1)
        fy = np.clip((y1 - y) / fade_margin, 0, 1) if y0 <= 0.5 else np.clip(np.minimum(y - y0, y1 - y) / fade_margin, 0, 1)
        return dx * fx * fy, dy * fx * fy
