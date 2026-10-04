"""Check that the head moves as ONE rigid plate and nothing is warped.

    python scripts/rigid_check.py --model models/aoi

For every pose (physics off):
  head group : all vertices of all head parts (head weight >= 0.99) must be
               mapped by one common similarity transform (rotation +
               translation, scale 1). Reports the max deviation in px.
  every part : per-drawable bend (deviation from its own affine map) and the
               strain of each triangle; hanging hair / neck / skin get a
               looser strain limit but must not fold.
  holes      : background gaps inside the character vs the rest pose.
Also runs the demo motion with physics on and reports the same numbers per
0.1 s for the head group.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from warp_check import holes  # noqa: E402
from PIL import Image  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

POSES = [
    ("Z+30", {"AngleZ": 30}), ("Z-30", {"AngleZ": -30}), ("Z+15", {"AngleZ": 15}),
    ("Y+30", {"AngleY": 30}), ("Y-30", {"AngleY": -30}),
    ("X+30 (ignored)", {"AngleX": 30}), ("X-30 (ignored)", {"AngleX": -30}),
    ("lean+10", {"BodyAngleZ": 10}), ("lean-10", {"BodyAngleZ": -10}), ("breath", {"Breath": 1}),
    ("mix1", {"AngleZ": 20, "AngleY": -15, "BodyAngleZ": -8, "Breath": 1, "EyeBallX": 1}),
    ("mix2", {"AngleZ": -25, "AngleY": 20, "BodyAngleZ": 10, "EyeBallX": -1, "EyeBallY": 1}),
    ("near", {"BodyAngleY": 10, "BodyAngleX": 10}), ("far", {"BodyAngleY": -10, "BodyAngleX": -10}),
    ("mix3", {"AngleZ": 18, "BodyAngleZ": -8, "BodyAngleY": 8, "BodyAngleX": -6, "Breath": 1}),
]
GROUP_PX, PART_BEND, PART_STRAIN, SOFT_STRAIN, HOLES = 0.5, 0.5, 0.02, 0.2, 40

JS = r"""
async ([vals, t, motion]) => {
  const m = await import('/js/core/metrics.js');
  const r = window.__rig, p = r.puppet;
  let img = null;
  if (motion) {
    Object.assign(p.animator.options, {idle: false, saccade: false});
    r.frame(t);
  } else {
    p.physicsEnabled = false;
    img = r.pose(vals, {settle: 0.1, hidden: p.model.drawables.map((d) => d.id).filter((id) => id.startsWith('headwear'))});
  }
  const P = [], Q = [];
  const parts = [];
  const vals_ = p.params.values;
  // the whole figure's closer / further scale and sideways shift (one uniform
  // similarity for everything) is taken out first: this check is about the
  // head moving as one plate relative to the rest
  const tf = p.deformer.tf, rb = p.model.rig.body;
  const sc = tf.stageScale ?? 1, [spx, spy] = rb.stagePivot ?? rb.pivot;
  const unstage = (o) => {
    if (sc === 1 && !tf.stageX && !tf.stageY) return o;
    const r = new Float32Array(o.length);
    for (let i = 0; i < o.length; i += 2) {
      r[i] = spx + (o[i] - spx - (tf.stageX ?? 0)) / sc;
      r[i + 1] = spy + (o[i + 1] - spy - (tf.stageY ?? 0)) / sc;
    }
    return r;
  };
  for (const d of p.model.drawables) {
    if ((p.deformer.opacity.get(d.id) ?? 1) < 0.02) continue;
    const o = unstage(p.deformer.out.get(d.id)), h = d.attrs.head;
    // eyelids closing / mouth opening are intended local motions
    const bl = (d.deformers || []).find((f) => f.type === 'blink');
    const local = (bl && (vals_[bl.eye === 'L' ? 'EyeLOpen' : 'EyeROpen'] ?? 1) < 0.999)
               || (/^mouth/.test(d.id) && (vals_.MouthOpenY ?? 0) > 0.001)
               || (d.deformers || []).some((f) => f.type === 'iris' || f.type === 'brow');
    const hang = /front hair|headwear|back hair/.test(d.id);
    const K = d.strandIds ? d.strandIds.length : 0;
    for (let i = 0; i < h.length; i++) {
      if (h[i] < 0.99 || local) continue;
      // vertices hanging on a bending strand are judged by warp_check instead
      let sw = 0; for (let k = 0; k < K; k++) sw += d.attrs.stw[i * K + k];
      if (sw > 0.001) continue;
      if (motion && hang) continue;          // swaying hair: judged separately
      P.push(d.positions[2 * i], d.positions[2 * i + 1]); Q.push(o[2 * i], o[2 * i + 1]);
    }
    const pm = m.partMetrics(d, o, {minHead: -1});
    if (pm) parts.push({...pm, local, soft: /back hair|neck|topwear|bottomwear|legwear|handwear|front hair|headwear/.test(d.id)});
  }
  // best similarity (rotation + translation) for the head group
  const n = P.length / 2;
  let mx = 0, my = 0, ux = 0, uy = 0;
  for (let i = 0; i < n; i++) { mx += P[2*i]; my += P[2*i+1]; ux += Q[2*i]; uy += Q[2*i+1]; }
  mx /= n; my /= n; ux /= n; uy /= n;
  let a = 0, b = 0;
  for (let i = 0; i < n; i++) {
    const px = P[2*i] - mx, py = P[2*i+1] - my, qx = Q[2*i] - ux, qy = Q[2*i+1] - uy;
    a += px * qx + py * qy; b += px * qy - py * qx;
  }
  const th = Math.atan2(b, a), c = Math.cos(th), s = Math.sin(th);
  let dev = 0;
  for (let i = 0; i < n; i++) {
    const px = P[2*i] - mx, py = P[2*i+1] - my;
    dev = Math.max(dev, Math.hypot(ux + c * px - s * py - Q[2*i], uy + s * px + c * py - Q[2*i+1]));
  }
  // ghosting: face art must be fully shown or fully hidden (no half-transparent double images)
  const ghosts = [];
  for (const d of p.model.drawables) {
    const op = p.deformer.opacity.get(d.id) ?? 1;
    if (op > 0.02 && op < 0.98) ghosts.push([d.id, +op.toFixed(2)]);
  }
  return {img, group: {n, dev, deg: th * 180 / Math.PI}, parts, ghosts};
}
"""


SWAY_STRAIN = 1.0    # swaying hair / ribbon ties with physics: bend is fine, folding is not


def judge(r, motion=False):
    strict = [x for x in r["parts"] if not x["soft"] and not x["local"]]
    soft = [x for x in r["parts"] if x["soft"]]
    if motion:
        sway = [dict(x, strainMax=x["strainMax"] * SOFT_STRAIN / SWAY_STRAIN) for x in soft
                if any(k in x["id"] for k in ("front hair", "headwear", "back hair"))]
        soft = [x for x in soft if not any(k in x["id"] for k in ("front hair", "headwear", "back hair"))] + sway
    wb = max(strict, key=lambda x: x["bendPx"])
    ws = max(strict, key=lambda x: x["strainMax"])
    ss = max(soft, key=lambda x: x["strainMax"])
    ok = (not r.get("ghosts") and r["group"]["dev"] <= GROUP_PX and wb["bendPx"] <= PART_BEND and ws["strainMax"] <= PART_STRAIN
          and ss["strainMax"] <= SOFT_STRAIN)
    return ok, wb, ws, ss


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="models/aoi")
    ap.add_argument("--url", default="http://127.0.0.1:8766")
    ap.add_argument("--motion", default="motions/demo.json")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    res = {"static": [], "motion": []}
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--use-angle=d3d11", "--enable-gpu", "--ignore-gpu-blocklist"])
        pg = b.new_page(viewport={"width": 700, "height": 700})
        pg.goto(f"{a.url}/player.html?record=1&model={a.model}&view=bust&bg=1,0,1,1")
        pg.wait_for_function("window.__rigReady === true", timeout=60000)
        r0 = pg.evaluate(JS, [{}, 0, False])
        h0 = holes(Image.open(io.BytesIO(base64.b64decode(r0["img"].split(",", 1)[1]))))
        npass = 0
        for name, vals in POSES:
            r = pg.evaluate(JS, [vals, 0, False])
            h = max(0, holes(Image.open(io.BytesIO(base64.b64decode(r["img"].split(",", 1)[1])))) - h0)
            ok, wb, ws, ss = judge(r)
            ok = ok and h <= HOLES
            npass += ok
            print(f"{name:15s} {'OK' if ok else 'NG'}  head-group dev {r['group']['dev']:.2f}px (rot {r['group']['deg']:+.1f} deg)"
                  f"  part bend {wb['bendPx']:.2f} ({wb['id']})  strain {ws['strainMax']:.3f} ({ws['id']})"
                  f"  soft {ss['strainMax']:.3f} ({ss['id']})  holes {h}")
            res["static"].append({"pose": name, "ok": ok, "group": r["group"], "holes": h,
                                  "bend": [wb["id"], wb["bendPx"]], "strain": [ws["id"], ws["strainMax"]],
                                  "soft": [ss["id"], ss["strainMax"]]})
        print(f"STATIC PASS {npass}/{len(POSES)}")
        pg.goto(f"{a.url}/player.html?record=1&model={a.model}&motion={a.motion}&view=bust")
        pg.wait_for_function("window.__rigReady === true", timeout=60000)
        dur = pg.evaluate("window.__rig.duration")
        t, bad = 0.0, 0
        while t < dur - 1e-6:
            t = round(t + 0.1, 2)
            r = pg.evaluate(JS, [{}, t, True])
            ok, wb, ws, ss = judge(r, motion=True)
            bad += not ok
            res["motion"].append({"t": t, "ok": ok, "ghosts": r.get("ghosts"), "group": r["group"], "bend": [wb["id"], wb["bendPx"]],
                                  "strain": [ws["id"], ws["strainMax"]], "soft": [ss["id"], ss["strainMax"]]})
        worst = max(res["motion"], key=lambda x: x["group"]["dev"])
        worst_s = max(res["motion"], key=lambda x: x["soft"][1])
        print(f"MOTION frames failing {bad}/{len(res['motion'])}; worst head-group dev {worst['group']['dev']:.2f}px @ {worst['t']}s;"
              f" worst soft strain {worst_s['soft'][1]:.3f} ({worst_s['soft'][0]}) @ {worst_s['t']}s")
        for x in [x for x in res["motion"] if not x["ok"]][:10]:
            print("   ", x)
        b.close()
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(res, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
