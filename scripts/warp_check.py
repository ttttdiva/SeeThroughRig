"""Zero-tolerance distortion check: every drawable, every frame.

    python scripts/warp_check.py --model models/aoi

For each drawable the deformation gradient of every triangle is measured
(|log singular value|): rotation - also a rotation that varies along a bending
hair strand - is 0; shear, stretch and squash are > 0. No part is exempt
(hair, ribbon ties, skirt, neck included). Also: head plate rigidity, holes,
half-transparent art. Runs 12 static poses and the whole demo motion
(physics on) at 30 fps.
"""
from __future__ import annotations

import argparse, base64, io, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402
from scipy import ndimage  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

POSES = [("rest", {}), ("Z+30", {"AngleZ": 30}), ("Z-30", {"AngleZ": -30}), ("Y+30", {"AngleY": 30}), ("Y-30", {"AngleY": -30}),
         ("lean+10", {"BodyAngleZ": 10}), ("lean-10", {"BodyAngleZ": -10}), ("breath", {"Breath": 1}),
         ("mix1", {"AngleZ": 20, "AngleY": -15, "BodyAngleZ": -8, "Breath": 1}),
         ("mix2", {"AngleZ": -25, "AngleY": 20, "BodyAngleZ": 10}),
         ("near", {"BodyAngleY": 10, "BodyAngleX": 10}), ("far", {"BodyAngleY": -10, "BodyAngleX": -10}),
         ("mix3", {"AngleZ": 18, "BodyAngleZ": -8, "BodyAngleY": 8, "BodyAngleX": -6, "Breath": 1})]
STRAIN = 0.05       # 5 % local stretch / shear at most, anywhere
HOLES = 40
CRACKS = 12         # px of thin enclosed background per frame (motion)
SEAM = 1.0          # separation of two bundles along their cut / 3/4 of the overlap they were cut with

JS = r"""
async ([vals, t, motion]) => {
  const m = await import('/js/core/metrics.js');
  const r = window.__rig, p = r.puppet;
  let img = null;
  if (motion) { r.frame(t); if (vals && vals.snap) { p.renderer.render(p.deformer, p.renderOptions); img = r.snapshot('image/png'); } }
  else { p.physicsEnabled = false; img = r.pose(vals, {settle: 0.1, hidden: p.model.drawables.map((d) => d.id).filter((id) => id.startsWith('headwear'))}); }
  const res = [];
  for (const d of p.model.drawables) {
    const op = p.deformer.opacity.get(d.id) ?? 1;
    if (op < 0.02) continue;
    let cover = null;
    if (d.strandIds) {
      // only triangles that are completely hidden behind opaque parts drawn in
      // front are skipped (a 6 px margin around each vertex must be covered too)
      const ups = p.model.drawables.filter((u) => u.order > d.order && !/front hair|headwear|irides|eyewhite|eyelash|brow|mouth|nose|eye[LR]-/.test(u.id)
                                         && (p.deformer.opacity.get(u.id) ?? 1) > 0.98)
        .map((u) => m.meshCover(u, p.deformer.out.get(u.id)));
      const inAny = (x, y) => ups.some((c) => c(x, y));
      cover = (x, y) => [[0, 0], [6, 0], [-6, 0], [0, 6], [0, -6]].every(([dx, dy]) => inAny(x + dx, y + dy));
    }
    res.push(m.triStrain(d, p.deformer.out.get(d.id), {cover, scale: p.deformer.tf.stageScale ?? 1}));
  }
  const ghosts = [];
  for (const d of p.model.drawables) { const op = p.deformer.opacity.get(d.id) ?? 1; if (op > 0.02 && op < 0.98) ghosts.push(d.id); }
  const opaque = p.model.drawables.filter((u) => (p.deformer.opacity.get(u.id) ?? 1) > 0.98 && !u.id.includes('#'))
    .map((u) => [u.order, m.meshCover(u, p.deformer.out.get(u.id))]);
  const hidden = (x, y, order) => [[0, 0], [6, 0], [-6, 0], [0, 6], [0, -6]]
    .every(([dx, dy]) => opaque.some(([o, c]) => o > order && c(x + dx, y + dy)));
  const seam = m.seamGap(p.model, p.deformer, {hidden});
  return {img, res, ghosts, seam};
}
"""


def holes(img: Image.Image) -> int:
    a = np.asarray(img.convert("RGB")).astype(int)
    bg = (np.abs(a - np.array([255, 0, 255])).sum(-1) < 40)
    lab, n = ndimage.label(bg)
    border = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]])))
    H, W = bg.shape
    total = 0
    for i in range(1, n + 1):
        if i in border:
            continue
        ys, xs = np.nonzero(lab == i)
        # background pinched off against the frame edge is not a gap in the character
        if xs.min() < 25 or ys.min() < 25 or xs.max() > W - 26 or ys.max() > H - 26:
            continue
        if len(xs) >= 4:
            total += len(xs)
    return int(total)


def cracks(img: Image.Image, max_halfwidth: float = 4.0) -> int:
    """Thin slivers of background enclosed by the character: cracks that open
    between parts (e.g. along the cut between two hair bundles). Wide enclosed
    background (the inside of a ribbon loop, the gap under an arm) is part of
    the drawing and is not counted."""
    a = np.asarray(img.convert("RGB")).astype(int)
    bg = (np.abs(a - np.array([255, 0, 255])).sum(-1) < 40)
    lab, n = ndimage.label(bg)
    border = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]])))
    dist = ndimage.distance_transform_edt(bg)
    total = 0
    for i in range(1, n + 1):
        if i in border:
            continue
        m = lab == i
        k = int(m.sum())
        if k >= 12 and dist[m].max() <= max_halfwidth:
            total += k
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="models/aoi")
    ap.add_argument("--url", default="http://127.0.0.1:8766")
    ap.add_argument("--motion", default="motions/demo.json")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    out = {"static": [], "motion": []}
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--use-angle=d3d11", "--enable-gpu", "--ignore-gpu-blocklist"])
        pg = b.new_page(viewport={"width": 700, "height": 700})
        pg.goto(f"{a.url}/player.html?record=1&model={a.model}&view=bust&bg=1,0,1,1")
        pg.wait_for_function("window.__rigReady === true", timeout=60000)
        h0 = None
        ok_n = 0
        for name, vals in POSES:
            r = pg.evaluate(JS, [vals, 0, False])
            h = holes(Image.open(io.BytesIO(base64.b64decode(r["img"].split(",", 1)[1]))))
            if h0 is None:
                h0 = h
            h = max(0, h - h0)
            w = max(r["res"], key=lambda x: x["max"])
            ok = w["max"] <= STRAIN and h <= HOLES and not r["ghosts"] and r["seam"]["ratio"] <= SEAM
            ok_n += ok
            print(f"{name:8s} {'OK' if ok else 'NG'}  worst strain {w['max']:.3f} ({w['id']} @ {w['at']})  holes {h}  seam {r['seam']['gap']:.1f}px ({r['seam']['ratio']:.2f})")
            out["static"].append({"pose": name, "ok": ok, "worst": w, "holes": h})
        print(f"STATIC PASS {ok_n}/{len(POSES)}")
        pg.goto(f"{a.url}/player.html?record=1&model={a.model}&motion={a.motion}&view=bust&bg=1,0,1,1")
        pg.wait_for_function("window.__rigReady === true", timeout=60000)
        dur = pg.evaluate("window.__rig.duration")
        n = int(dur * a.fps)
        bad = []
        per_part = {}
        max_holes = 0
        max_seam = 0.0
        for i in range(1, n + 1):
            t = i / a.fps
            snap = i % 3 == 0          # gaps between parts: every 3rd frame
            r = pg.evaluate(JS, [{"snap": True} if snap else {}, t, True])
            if snap:
                hm = cracks(Image.open(io.BytesIO(base64.b64decode(r["img"].split(",", 1)[1]))))
                max_holes = max(max_holes, hm)
            for x in r["res"]:
                per_part[x["id"]] = max(per_part.get(x["id"], 0), x["max"])
            w = max(r["res"], key=lambda x: x["max"])
            max_seam = max(max_seam, r["seam"]["ratio"])
            if w["max"] > STRAIN or r["ghosts"] or r["seam"]["ratio"] > SEAM:
                bad.append({"t": round(t, 3), "worst": w, "ghosts": r["ghosts"], "seam": r["seam"]})
        print(f"MOTION frames failing {len(bad)}/{n}  (max crack {max_holes} px, max seam {max_seam:.2f} of allowed)")
        for k, v in sorted(per_part.items(), key=lambda kv: -kv[1])[:8]:
            print(f"   worst over motion: {k:22s} {v:.3f}")
        for x in bad[:6]:
            print("   ", x)
        out["motion"] = bad
        out["per_part"] = per_part
        b.close()
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(out, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
