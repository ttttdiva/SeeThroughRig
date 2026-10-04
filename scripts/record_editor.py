"""Screen-record a scripted editor session (real-time, Playwright video).

    python scripts/record_editor.py --out docs/media/editor.mp4
"""
from __future__ import annotations

import argparse
import math
import shutil
import subprocess
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright


def set_slider(page, label: str, value: float, container: str = "#right"):
    page.evaluate(
        """([label, value, container]) => {
            const rows = [...document.querySelectorAll(container + ' .row')];
            const row = rows.find(r => r.querySelector('label').textContent === label);
            const input = row.querySelector('input');
            input.value = value;
            input.dispatchEvent(new Event('input'));
        }""",
        [label, value, container],
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("docs/media/editor.mp4"))
    ap.add_argument("--url", default="http://127.0.0.1:8765")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp())
    W, H = 1600, 900
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--use-angle=d3d11", "--enable-gpu", "--ignore-gpu-blocklist"])
        ctx = browser.new_context(viewport={"width": W, "height": H}, record_video_dir=str(tmp),
                                  record_video_size={"width": W, "height": H})
        page = ctx.new_page()
        page.goto(f"{args.url}/editor.html")
        page.wait_for_timeout(2500)
        stage = page.locator("#stage").bounding_box()
        cx, cy = stage["x"] + stage["width"] / 2, stage["y"] + stage["height"] * 0.3

        # drag the face around (look-at)
        page.mouse.move(cx, cy)
        page.mouse.down()
        for i in range(160):
            a = i / 160 * 2 * math.pi * 1.5
            page.mouse.move(cx + math.cos(a) * 260, cy + math.sin(a * 1.3) * 170)
            page.wait_for_timeout(28)
        page.mouse.up()
        page.wait_for_timeout(800)

        # expressions
        for label in ["にっこり", "驚き", "困り", "照れ", "通常"]:
            page.click(f"#exprs >> text={label}")
            page.wait_for_timeout(1300)

        # talk
        page.click("#talk")
        page.wait_for_timeout(3500)

        # show the mesh and tune depth / physics
        page.click("text=メッシュ")
        page.wait_for_timeout(1200)
        page.click("text=メッシュ")
        set_slider(page, "顔の奥行き", 1.6)
        page.mouse.move(cx, cy)
        page.mouse.down()
        for i in range(50):
            page.mouse.move(cx + (1 if (i // 12) % 2 else -1) * 300, cy)
            page.wait_for_timeout(30)
        page.mouse.up()
        page.wait_for_timeout(1500)
        set_slider(page, "顔の奥行き", 1.0)

        page.select_option("#chainSel", "sideLockL")
        set_slider(page, "剛性(根元)", 25)
        set_slider(page, "剛性(先端)", 8)
        page.mouse.move(cx, cy)
        page.mouse.down()
        for i in range(60):
            page.mouse.move(cx + (1 if (i // 10) % 2 else -1) * 320, cy + 40)
            page.wait_for_timeout(30)
        page.mouse.up()
        page.wait_for_timeout(2500)
        ctx.close()
        browser.close()
    webm = next(tmp.glob("*.webm"))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(webm), "-c:v", "libx264", "-crf", "20",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(args.out)], check=True)
    shutil.rmtree(tmp, ignore_errors=True)
    print(args.out)


if __name__ == "__main__":
    main()
