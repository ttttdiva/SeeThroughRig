"""Fit deformation-only keyforms to drawn pose images (analysis by synthesis).

Runs in the See-through Python environment (needs torch + CUDA):

    python seethrough_rig/fitkeys_torch.py --layers work/fit/layers.npz \
        --key "AngleX:30=work/keyforms/yaw_right.png=work/keyforms/st/yaw_right/yaw_right/optimized" \
        --out work/fit

For every motion group of the neutral decomposition (face, bangs, back hair,
each eye, brow, ear, mouth ...) we optimise a smooth warp (dense lattice for
hair / face / neck, an affine transform for small drawn parts) so that the
re-composited neutral layers reproduce the drawn pose image. The pose image's
own See-through masks are used as an additional shape target. The result is
a pure deformation of the original artwork: at runtime nothing is swapped or
cross-faded, exactly like a Live2D keyform.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image

GROUPS = [
    # name, member tags, kind, key-mask tags, mask weight
    ("back", ["back hair"], "dense", ["back hair"], 0.15),
    ("headwear", ["headwear", "headwear-front"], "dense", ["headwear"], 0.3),
    ("earR", ["ears-r"], "affine", ["ears-r"], 0.6),
    ("earL", ["ears-l"], "affine", ["ears-l"], 0.6),
    ("neck", ["neck"], "dense", ["neck"], 0.5),
    # nose and mouth are drawn on the face: they ride on the face's warp
    ("face", ["face", "nose", "mouth"], "dense", ["face"], 1.5),
    ("eyeR", ["eyewhite-r", "irides-r", "eyelash-r"], "affine", ["eyewhite-r", "irides-r", "eyelash-r"], 1.5),
    ("eyeL", ["eyewhite-l", "irides-l", "eyelash-l"], "affine", ["eyewhite-l", "irides-l", "eyelash-l"], 1.5),
    ("front", ["front hair"], "dense", ["front hair"], 0.4),
    ("browR", ["eyebrow-r"], "affine", ["eyebrow-r"], 0.5),
    ("browL", ["eyebrow-l"], "affine", ["eyebrow-l"], 0.5),
]

# stiffer / coarser warps for parts whose drawn strokes must not bend
GROUP_OPTS = {
    "front": {"reg": 4.0},
    "back": {"reg": 3.0, "max_lat": 16},
    "headwear": {"reg": 4.0, "max_lat": 8},
    "neck": {"reg": 2.0, "max_lat": 12},
    "face": {"reg": 1.5},
    # affine parts: how strongly scale / shear is resisted (drawn shapes stay close to rigid)
    "mouth": {"reg": 15.0}, "nose": {"reg": 15.0}, "browL": {"reg": 5.0}, "browR": {"reg": 5.0},
    "eyeL": {"reg": 2.0}, "eyeR": {"reg": 2.0}, "earL": {"reg": 3.0}, "earR": {"reg": 3.0},
}

DEV = "cuda"


def load_key_masks(opt_dir: Path, frame: int) -> dict[str, np.ndarray]:
    info = json.loads((opt_dir / "info.json").read_text(encoding="utf-8"))
    out = {}
    for tag, p in info["parts"].items():
        f = opt_dir / f"{tag}.png"
        if not f.exists():
            continue
        im = np.asarray(Image.open(f).convert("RGBA"), np.float32)[..., 3] / 255.0
        if im.max() < 0.1:
            continue
        x0, y0 = p["xyxy"][:2]
        can = np.zeros((frame, frame), np.float32)
        h, w = im.shape
        can[y0:y0 + h, x0:x0 + w] = im[: frame - y0, : frame - x0]
        can[can < 0.06] = 0
        out[tag] = can
    return out


def blur(x, sigma):
    if sigma <= 0.3:
        return x
    k = int(sigma * 3) * 2 + 1
    t = torch.arange(k, device=x.device, dtype=x.dtype) - k // 2
    g = torch.exp(-(t * t) / (2 * sigma * sigma))
    g = g / g.sum()
    c = x.shape[1]
    x = F.conv2d(F.pad(x, (k // 2, k // 2, 0, 0), mode="replicate"), g.view(1, 1, 1, k).repeat(c, 1, 1, 1), groups=c)
    x = F.conv2d(F.pad(x, (0, 0, k // 2, k // 2), mode="replicate"), g.view(1, 1, k, 1).repeat(c, 1, 1, 1), groups=c)
    return x


def sobel(x):
    lum = (x[:, 0:1] * 0.3 + x[:, 1:2] * 0.55 + x[:, 2:3] * 0.15)
    kx = torch.tensor([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]], device=x.device, dtype=x.dtype).view(1, 1, 3, 3) / 4
    gx = F.conv2d(F.pad(lum, (1, 1, 1, 1), mode="replicate"), kx)
    gy = F.conv2d(F.pad(lum, (1, 1, 1, 1), mode="replicate"), kx.transpose(2, 3))
    return torch.cat([gx, gy], 1)


class Fitter:
    def __init__(self, layers: np.ndarray, tags: list[str], res: int):
        self.tags = tags
        self.res = res
        n = len(tags)
        lay = torch.from_numpy(layers).to(DEV).float().permute(0, 3, 1, 2) / 255.0   # n,4,H,W
        lay = F.interpolate(lay, size=(res, res), mode="area")
        a = lay[:, 3:4]
        # irides are clipped by their eye whites
        for side in ("l", "r"):
            if f"irides-{side}" in tags and f"eyewhite-{side}" in tags:
                i, w = tags.index(f"irides-{side}"), tags.index(f"eyewhite-{side}")
                a[i] = a[i] * a[w]
        self.prem = torch.cat([lay[:, :3] * a, a], 1)                                   # premultiplied
        self.group_of = [-1] * n
        self.groups = []
        for gi, (name, members, kind, mtags, mw) in enumerate(GROUPS):
            idx = [tags.index(m) for m in members if m in tags]
            if not idx:
                continue
            o = GROUP_OPTS.get(name, {})
            g = {"name": name, "kind": kind, "idx": idx, "mtags": mtags, "mw": mw,
                 "max_lat": o.get("max_lat", 99), "reg": o.get("reg", 1.0)}
            al = self.prem[idx, 3].sum(0)
            ys, xs = torch.nonzero(al > 0.3, as_tuple=True)
            g["center"] = torch.stack([xs.float().mean(), ys.float().mean()]) / res * 2 - 1
            for i in idx:
                self.group_of[i] = len(self.groups)
            self.groups.append(g)
        ys, xs = torch.meshgrid(torch.linspace(-1, 1, res, device=DEV), torch.linspace(-1, 1, res, device=DEV), indexing="ij")
        # align_corners=False pixel centres
        s = (res - 1) / res
        self.base = torch.stack([xs * s, ys * s], -1)                                  # H,W,2 (x,y)

    def init_params(self, lattice: int):
        self.params = []
        for g in self.groups:
            if g["kind"] == "dense":
                lat = min(lattice, g["max_lat"])
                p = torch.zeros(1, 2, lat, lat, device=DEV, requires_grad=True)
            else:
                p = torch.zeros(6, device=DEV, requires_grad=True)   # a b c d tx ty
            self.params.append(p)

    def upsample_params(self, lattice: int):
        new = []
        for g, p in zip(self.groups, self.params):
            if g["kind"] == "dense":
                lat = min(lattice, g["max_lat"])
                q = F.interpolate(p.detach(), size=(lat, lat), mode="bicubic", align_corners=True)
                new.append(q.clone().requires_grad_(True))
            else:
                new.append(p.detach().clone().requires_grad_(True))
        self.params = new

    def field(self, gi):
        """Backward displacement (H,W,2) in normalised coords for group gi."""
        g, p = self.groups[gi], self.params[gi]
        if g["kind"] == "dense":
            d = F.interpolate(p, size=(self.res, self.res), mode="bicubic", align_corners=True)
            return d[0].permute(1, 2, 0)
        a, b, c, d, tx, ty = p
        rel = self.base - g["center"]
        dx = a * rel[..., 0] + b * rel[..., 1] + tx
        dy = c * rel[..., 0] + d * rel[..., 1] + ty
        return torch.stack([dx, dy], -1)

    def render(self):
        fields = [self.field(i) for i in range(len(self.groups))]
        n = len(self.tags)
        C = torch.ones(1, 3, self.res, self.res, device=DEV)
        alphas = {}
        for i in range(n):
            gi = self.group_of[i]
            L = self.prem[i:i + 1]
            if gi >= 0:
                grid = (self.base + fields[gi]).unsqueeze(0)
                L = F.grid_sample(L, grid, mode="bilinear", padding_mode="zeros", align_corners=False)
            C = L[:, :3] + C * (1 - L[:, 3:4])
            alphas[self.tags[i]] = L[0, 3]
        return C, alphas, fields

    def reg(self, fields):
        r = 0.0
        for g, p, f in zip(self.groups, self.params, fields):
            if g["kind"] == "dense":
                # thin-plate (second differences) + membrane on the lattice, in pixel units
                q = p * (self.res / 2)
                d2x = q[..., :, 2:] - 2 * q[..., :, 1:-1] + q[..., :, :-2]
                d2y = q[..., 2:, :] - 2 * q[..., 1:-1, :] + q[..., :-2, :]
                dxy = q[..., 1:, 1:] - q[..., 1:, :-1] - q[..., :-1, 1:] + q[..., :-1, :-1]
                d1x = q[..., :, 1:] - q[..., :, :-1]
                d1y = q[..., 1:, :] - q[..., :-1, :]
                # normalise by the lattice spacing so coarse and fine lattices mean the same curvature
                sp = self.res / (q.shape[-1] - 1)
                r = r + g["reg"] * ((d2x.pow(2).mean() + d2y.pow(2).mean() + 2 * dxy.pow(2).mean()) * (32 / sp) ** 2
                                    + 0.02 * (d1x.pow(2).mean() + d1y.pow(2).mean()) * (32 / sp))
                # no folding: the Jacobian of the backward map must stay well positive
                J = f.permute(2, 0, 1)[None] * (self.res / 2)
                gxx = J[:, 0, :, 1:] - J[:, 0, :, :-1]
                gyy = J[:, 1, 1:, :] - J[:, 1, :-1, :]
                gxy = J[:, 0, 1:, :] - J[:, 0, :-1, :]
                gyx = J[:, 1, :, 1:] - J[:, 1, :, :-1]
                det = (1 + gxx[:, :-1, :]) * (1 + gyy[:, :, :-1]) - gxy[:, :, :-1] * gyx[:, :-1, :]
                r = r + 200.0 * F.relu(0.5 - det).pow(2).mean()
            else:
                r = r + 20.0 * g["reg"] * p[:4].pow(2).sum()
        return r

    def fit(self, target: torch.Tensor, key_masks: dict, lattices=(6, 10, 16, 24), iters=(400, 300, 300, 300),
            sigmas=((16, 8), (8, 4), (4, 2), (2, 1.2)), mask_w=(6.0, 3.0, 1.5, 1.0), log=print):
        weight_map = torch.ones(1, 1, self.res, self.res, device=DEV)
        km = {}
        for g in self.groups:
            m = None
            for t in g["mtags"]:
                if t in key_masks:
                    k = torch.from_numpy(cv2.resize(key_masks[t], (self.res, self.res), interpolation=cv2.INTER_AREA)).to(DEV)
                    m = k if m is None else torch.maximum(m, k)
            km[g["name"]] = m
        tgt_blur = {}
        self.init_params(lattices[0])
        for stage, (lat, it, (s0, s1), mw_s) in enumerate(zip(lattices, iters, sigmas, mask_w)):
            if stage:
                self.upsample_params(lat)
            opt = torch.optim.Adam(self.params, lr=4e-3 if stage == 0 else 1.5e-3)
            for k in range(it):
                u = k / max(1, it - 1)
                sig = s0 * (1 - u) + s1 * u
                C, alphas, fields = self.render()
                key = round(sig * 4) / 4
                if key not in tgt_blur:
                    tgt_blur[key] = (blur(target, key), sobel(blur(target, key)))
                tb, te = tgt_blur[key]
                cb = blur(C, key)
                photo = ((cb - tb).abs() * weight_map).mean() + 0.5 * (sobel(cb) - te).abs().mean() * 4
                mloss = 0.0
                for g in self.groups:
                    m = km[g["name"]]
                    if m is None:
                        continue
                    a = None
                    for i in g["idx"]:
                        ai = alphas[self.tags[i]]
                        a = ai if a is None else torch.maximum(a, ai)
                    ab, mb = blur(a[None, None], key)[0, 0], blur(m[None, None], key)[0, 0]
                    inter = (ab * mb).sum()
                    union = (ab + mb - ab * mb).sum() + 1e-6
                    mloss = mloss + g["mw"] * (1 - inter / union)
                loss = photo * 20 + mloss * mw_s + self.reg(fields) * 1e-3
                opt.zero_grad()
                loss.backward()
                opt.step()
            log(f"    stage {stage} lattice {lat}: loss {loss.item():.4f} photo {photo.item():.4f} mask {float(mloss):.3f}")
        self.last_alphas = {k: v.detach() for k, v in alphas.items()}
        with torch.no_grad():
            C, alphas, fields = self.render()
        return C, fields

    def export(self, size: int = 256):
        """Backward displacement fields at size x size (normalised coords) per group."""
        out = {}
        with torch.no_grad():
            for gi, g in enumerate(self.groups):
                f = self.field(gi).permute(2, 0, 1)[None]
                f = F.interpolate(f, size=(size, size), mode="bilinear", align_corners=False)[0].permute(1, 2, 0)
                out[g["name"]] = f.cpu().numpy().astype(np.float32)
        return out


def to_img(C):
    return (C[0].permute(1, 2, 0).clamp(0, 1).cpu().numpy() * 255).astype(np.uint8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--layers", type=Path, required=True)
    ap.add_argument("--key", action="append", required=True, help="NAME=image.png[=seethrough_optimized_dir]")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--res", type=int, default=512)
    ap.add_argument("--frame", type=int, default=1024)
    args = ap.parse_args()
    d = np.load(args.layers)
    layers, tags = d["layers"], [str(t) for t in d["tags"]]
    args.out.mkdir(parents=True, exist_ok=True)
    fitter = Fitter(layers, tags, args.res)
    # sanity: the un-warped composite
    with torch.no_grad():
        fitter.init_params(6)
        C0, _, _ = fitter.render()
    Image.fromarray(to_img(C0)).save(args.out / "neutral_composite.png")
    for spec in args.key:
        parts = spec.split("=")
        name, img = parts[0], Path(parts[1])
        masks = load_key_masks(Path(parts[2]), args.frame) if len(parts) > 2 else {}
        t0 = time.time()
        tim = np.asarray(Image.open(img).convert("RGB").resize((args.res, args.res), Image.LANCZOS), np.float32) / 255
        target = torch.from_numpy(tim).to(DEV).permute(2, 0, 1)[None]
        print(f"[fit] {name}")
        C, fields = fitter.fit(target, masks)
        fname = name.replace(":", "_").replace(",", "__")
        np.savez_compressed(args.out / f"{fname}.npz", **fitter.export(), name=name)
        ov = to_img(C).copy()
        for t in ("face", "eyewhite-l", "eyewhite-r", "front hair", "mouth", "ears-l", "ears-r"):
            if t in masks:
                m = (cv2.resize(masks[t], (args.res, args.res)) > 0.5).astype(np.uint8)
                cs, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
                cv2.drawContours(ov, cs, -1, (255, 0, 0), 1)
            if t in fitter.last_alphas:
                m = (fitter.last_alphas[t].cpu().numpy() > 0.5).astype(np.uint8)
                cs, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
                cv2.drawContours(ov, cs, -1, (0, 160, 0), 1)
        prev = np.concatenate([(tim * 255).astype(np.uint8), to_img(C), ov], 1)
        Image.fromarray(prev).save(args.out / f"{fname}_preview.png")
        print(f"    done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
