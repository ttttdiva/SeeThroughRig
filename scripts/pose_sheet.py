"""Render a grid of rig poses for visual inspection.

    python scripts/pose_sheet.py --out out/poses.png --view face
    python scripts/pose_sheet.py --set head      # predefined pose sets

Requires the dev server (``strig serve``) and Playwright's Chromium.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

SETS = {
    "head": [
        ("rest", {}),
        ("AngleX -30", {"AngleX": -30}), ("AngleX +30", {"AngleX": 30}),
        ("AngleY +30", {"AngleY": 30}), ("AngleY -30", {"AngleY": -30}),
        ("AngleZ +30", {"AngleZ": 30}),
        ("X+30 Y+30", {"AngleX": 30, "AngleY": 30}), ("X-30 Y-30", {"AngleX": -30, "AngleY": -30}),
    ],
    "body": [
        ("rest", {}),
        ("BodyX -10", {"BodyAngleX": -10}), ("BodyX +10", {"BodyAngleX": 10}),
        ("BodyZ -10", {"BodyAngleZ": -10}), ("BodyZ +10", {"BodyAngleZ": 10}),
        ("BodyY +10", {"BodyAngleY": 10}), ("Breath 1", {"Breath": 1}),
        ("all", {"AngleX": 20, "AngleZ": -10, "BodyAngleX": 8, "BodyAngleZ": 6}),
    ],
    "face": [
        ("rest", {}),
        ("half", {"EyeLOpen": 0.5, "EyeROpen": 0.5}),
        ("closed", {"EyeLOpen": 0, "EyeROpen": 0}),
        ("wink", {"EyeLOpen": 0}),
        ("look", {"EyeBallX": 1, "EyeBallY": 0.6}),
        ("mouth .5", {"MouthOpenY": 0.5}),
        ("mouth 1", {"MouthOpenY": 1}),
        ("brows", {"BrowLY": 1, "BrowRY": 1, "BrowLAngle": 1, "BrowRAngle": 1}),
    ],
    "expr": [
        ("neutral", {}),
        ("happy", {"EyeHappy": 1, "BrowHappy": 1, "MouthHappy": 1, "MouthForm": 0.6}),
        ("surprised", {"EyeSurprised": 1, "BrowSurprised": 1, "MouthSurprised": 1}),
        ("worried", {"EyeWorried": 1, "BrowWorried": 1, "MouthWorried": 1}),
        ("shy", {"EyeShy": 1, "BrowShy": 1, "MouthShy": 1}),
        ("happy talk", {"EyeHappy": 1, "BrowHappy": 1, "MouthOpenY": 0.7}),
        ("surprised X", {"EyeSurprised": 1, "BrowSurprised": 1, "MouthSurprised": 1, "AngleX": 25}),
        ("closed X", {"EyeLOpen": 0, "EyeROpen": 0, "AngleX": -25, "AngleY": 15}),
    ],
}


def launch(p):
    return p.chromium.launch(args=["--use-angle=d3d11", "--enable-gpu", "--ignore-gpu-blocklist"])


def render(url_base: str, poses, view: str, size: int, settle: float):
    imgs = []
    with sync_playwright() as p:
        browser = launch(p)
        page = browser.new_page(viewport={"width": size, "height": size})
        page.goto(f"{url_base}/player.html?record=1&view={view}")
        page.wait_for_function("window.__rigReady === true", timeout=60000)
        for item in poses:
            label, vals = item[0], item[1]
            opts = {"settle": settle, **(item[2] if len(item) > 2 else {})}
            data = page.evaluate(f"window.__rig.pose({json.dumps(vals)}, {json.dumps(opts)})")
            im = Image.open(io.BytesIO(base64.b64decode(data.split(",", 1)[1]))).convert("RGB")
            imgs.append((label, im))
        browser.close()
    return imgs


def grid(imgs, cols=4, cell=None):
    w, h = imgs[0][1].size
    if cell:
        w = h = cell
    rows = (len(imgs) + cols - 1) // cols
    out = Image.new("RGB", (cols * w, rows * (h + 26)), "white")
    d = ImageDraw.Draw(out)
    try:
        font = ImageFont.truetype("arial.ttf", 18)
    except OSError:
        font = None
    for i, (label, im) in enumerate(imgs):
        x, y = (i % cols) * w, (i // cols) * (h + 26)
        out.paste(im.resize((w, h)), (x, y + 26))
        d.text((x + 6, y + 3), label, fill="black", font=font)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="head", choices=list(SETS))
    ap.add_argument("--poses", help="JSON list of [label, {params}] instead of a set")
    ap.add_argument("--view", default="face")
    ap.add_argument("--size", type=int, default=560)
    ap.add_argument("--settle", type=float, default=1.5)
    ap.add_argument("--url", default="http://127.0.0.1:8765")
    ap.add_argument("--out", type=Path, default=Path("out/poses.png"))
    args = ap.parse_args()
    poses = json.loads(args.poses) if args.poses else SETS[args.set]
    imgs = render(args.url, poses, args.view, args.size, args.settle)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    grid(imgs).save(args.out)
    print(args.out)


if __name__ == "__main__":
    main()
