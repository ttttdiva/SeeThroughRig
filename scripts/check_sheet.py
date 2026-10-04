"""Full-size check renders of a model (physics off) for my own review.

    python scripts/check_sheet.py models/aoi out/r1_face.png --view face --poses X+30,X-30,...
"""
import argparse, base64, io, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from warp_check import POSES
from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("model"); ap.add_argument("out", type=Path)
ap.add_argument("--view", default="face"); ap.add_argument("--size", type=int, default=800)
ap.add_argument("--poses", default="rest,X+30,X-30,Y+30,Y-30,X+30Y+30,X-30Y-30,Z-30,mix2")
ap.add_argument("--crop", default=None, help="x0,y0,x1,y1 in render px")
ap.add_argument("--cols", type=int, default=3)
ap.add_argument("--phys", action="store_true")
a = ap.parse_args()
P = dict(POSES); P["rest"] = {}
names = a.poses.split(",")
ims = []
with sync_playwright() as p:
    b = p.chromium.launch(args=["--use-angle=d3d11", "--enable-gpu", "--ignore-gpu-blocklist"])
    pg = b.new_page(viewport={"width": a.size, "height": a.size})
    pg.goto(f"http://127.0.0.1:8766/player.html?record=1&model={a.model}&view={a.view}")
    pg.wait_for_function("window.__rigReady === true", timeout=60000)
    for n in names:
        v = P[n] if n in P else json.loads(n)
        d = pg.evaluate(f"(window.__rig.puppet.physicsEnabled={'true' if a.phys else 'false'}, window.__rig.pose({json.dumps(v)},{{settle:1.0}}))")
        im = Image.open(io.BytesIO(base64.b64decode(d.split(",", 1)[1]))).convert("RGB")
        if a.crop:
            im = im.crop(tuple(int(t) for t in a.crop.split(",")))
        ImageDraw.Draw(im).text((6, 6), n, fill="black")
        ims.append(im)
    b.close()
w, h = ims[0].size
rows = (len(ims) + a.cols - 1) // a.cols
s = Image.new("RGB", (w * a.cols, h * rows), "white")
for i, im in enumerate(ims):
    s.paste(im, ((i % a.cols) * w, (i // a.cols) * h))
a.out.parent.mkdir(parents=True, exist_ok=True)
s.save(a.out)
print(a.out)
