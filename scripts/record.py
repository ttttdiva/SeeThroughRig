"""Record a motion to video, frame by frame (deterministic, not realtime).

    python scripts/record.py --motion motions/demo.json --out docs/media/demo.mp4

Requires the dev server (``strig serve``), Playwright's Chromium and ffmpeg.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--motion", default="motions/demo.json", help="path relative to web/")
    ap.add_argument("--model", default="models/aoi")
    ap.add_argument("--view", default="bust")
    ap.add_argument("--out", type=Path, default=Path("docs/media/demo.mp4"))
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--duration", type=float, default=None, help="default: the motion's duration")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--url", default="http://127.0.0.1:8765")
    ap.add_argument("--crf", type=int, default=18)
    ap.add_argument("--frames-dir", type=Path, default=None, help="also dump PNG frames here")
    args = ap.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--use-angle=d3d11", "--enable-gpu", "--ignore-gpu-blocklist"])
        page = browser.new_page(viewport={"width": args.width, "height": args.height}, device_scale_factor=1)
        page.goto(f"{args.url}/player.html?record=1&model={args.model}&motion={args.motion}&view={args.view}")
        page.wait_for_function("window.__rigReady === true", timeout=60000)
        duration = args.duration or page.evaluate("window.__rig.duration")
        n = int(round((duration - args.start) * args.fps))
        ff = subprocess.Popen([
            "ffmpeg", "-v", "error", "-y", "-f", "image2pipe", "-framerate", str(args.fps), "-c:v", "mjpeg", "-i", "-",
            "-c:v", "libx264", "-preset", "slow", "-crf", str(args.crf), "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(args.out),
        ], stdin=subprocess.PIPE)
        # warm up physics before the first frame
        page.evaluate(f"window.__rig.frame({args.start})")
        t0 = time.time()
        for i in range(n):
            t = args.start + (i + 1) / args.fps
            page.evaluate(f"window.__rig.frame({t})")
            # a page screenshot (not canvas.toDataURL) so HTML captions are included
            jpg = page.screenshot(type="jpeg", quality=95)
            ff.stdin.write(jpg)
            if args.frames_dir and i % args.fps == 0:
                args.frames_dir.mkdir(parents=True, exist_ok=True)
                (args.frames_dir / f"f{i:05d}.jpg").write_bytes(jpg)
            if i % (args.fps * 5) == 0:
                el = time.time() - t0
                print(f"  frame {i}/{n}  t={t:.1f}s  ({el:.0f}s elapsed)", file=sys.stderr)
        ff.stdin.close()
        ff.wait()
        browser.close()
    print(args.out)


if __name__ == "__main__":
    main()
