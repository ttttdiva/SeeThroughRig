"""Record a short, real See-through Rig editor showcase.

The capture is made from ``editor.html`` itself. Playback settings are loaded
through the normal editor settings path, every visible slider change is made
through the real range input, and the model is advanced by the editor's own
``puppet.step`` loop at a deterministic 30 fps. No editor or runtime source
is changed by this script.

Examples::

    .venv/Scripts/python.exe scripts/record_showcase.py --preview
    .venv/Scripts/python.exe scripts/record_showcase.py
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import Locator, Page, sync_playwright


EDITOR_READY = "window.puppet && document.querySelectorAll('#left input[type=range], #right input[type=range]').length > 0"
CURSOR_SCRIPT = r"""
(() => {
  // Hold the editor's own RAF until the recorder advances it. The callback is
  // still the exact callback installed by editor.js, so puppet.step(), the
  // physics engine and the real render path remain in use.
  const callbacks = [];
  window.__editorRecord = {
    callbacks,
    request(cb) { callbacks.push(cb); return callbacks.length; },
    cancel() {},
    step(now) {
      const cb = callbacks.shift();
      if (!cb) throw new Error('editor RAF queue is empty');
      cb(now);
      return callbacks.length;
    },
  };
  window.requestAnimationFrame = (cb) => window.__editorRecord.request(cb);
  window.cancelAnimationFrame = () => window.__editorRecord.cancel();
})();
"""
CURSOR_OVERLAY = r"""
(() => {
  const cursor = document.createElement('div');
  cursor.id = '__editor-record-cursor';
  Object.assign(cursor.style, {
    position: 'fixed', left: '0px', top: '0px', width: '16px', height: '16px',
    margin: '0', border: '2px solid #ffe48a', borderRadius: '50%',
    background: 'rgba(255,228,138,.14)', boxShadow: '0 0 0 2px rgba(20,25,35,.8), 0 0 10px rgba(255,228,138,.8)',
    transform: 'translate(-50%,-50%)', pointerEvents: 'none', zIndex: '2147483647',
  });
  document.body.append(cursor);
  window.__editorRecordCursor = (x, y) => {
    cursor.style.left = `${x}px`; cursor.style.top = `${y}px`;
  };
})();
"""


def wait_for_http(url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/editor.html", timeout=1.5) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError) as error:
            last_error = error
        time.sleep(0.2)
    raise RuntimeError(f"strig serve did not become ready at {url}: {last_error}")


class LocalServer:
    """Own and clean up only the server started for this recording."""

    def __init__(self, python: Path, root: Path, port: int):
        self.python = python
        self.root = root
        self.port = port
        self.process: subprocess.Popen[bytes] | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "LocalServer":
        self.process = subprocess.Popen(
            [str(self.python), "-m", "seethrough_rig.cli", "serve", "--root", str(self.root), "--port", str(self.port)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            wait_for_http(self.url)
        except Exception:
            self.stop()
            raise
        return self

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)

    def __exit__(self, *_: object) -> None:
        self.stop()


def ffprobe(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def check_video(path: Path, width: int, height: int, fps: int, duration: float) -> dict[str, Any]:
    metadata = ffprobe(path)
    stream = next((item for item in metadata.get("streams", []) if item.get("codec_type") == "video"), None)
    if stream is None:
        raise RuntimeError("ffprobe found no video stream")
    actual_rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
    if stream.get("width") != width or stream.get("height") != height:
        raise RuntimeError(f"Unexpected video dimensions: {stream.get('width')}x{stream.get('height')}")
    if actual_rate not in {f"{fps}/1", f"{fps}/1.0"}:
        raise RuntimeError(f"Unexpected video frame rate: {actual_rate}")
    actual_duration = float(stream.get("duration") or metadata.get("format", {}).get("duration") or 0)
    if abs(actual_duration - duration) > 0.15:
        raise RuntimeError(f"Unexpected video duration: {actual_duration:.3f}s (expected {duration:.3f}s)")
    return {
        "width": stream.get("width"), "height": stream.get("height"),
        "fps": actual_rate, "frames": stream.get("nb_frames"),
        "duration": actual_duration, "codec": stream.get("codec_name"),
        "format": metadata.get("format", {}).get("format_name"),
    }


def make_poster(video: Path, poster: Path, second: float) -> None:
    poster.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", f"{second:.3f}", "-i", str(video),
         "-frames:v", "1", "-q:v", "2", "-vf", "scale=1920:1080:flags=lanczos", str(poster)],
        check=True,
    )


def make_contact_sheet(frames: list[Path], output: Path) -> None:
    if not frames:
        raise RuntimeError("No sample frames were captured for contact-sheet QA")
    concat = output.with_suffix(".txt")
    concat.write_text("\n".join(f"file '{frame.as_posix()}'" for frame in frames), encoding="utf-8")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
         "-vf", "scale=480:-2,tile=4x2:padding=8:margin=8:color=0x071126",
         "-frames:v", "1", "-q:v", "3", str(output)], check=True,
    )
    concat.unlink(missing_ok=True)
    if not output.exists() or output.stat().st_size < 1000:
        raise RuntimeError("Contact-sheet QA image was not produced")


def page_errors(page: Page, errors: list[str]) -> None:
    page.on("pageerror", lambda error: errors.append(f"pageerror: {error}"))
    page.on("console", lambda message: errors.append(f"console: {message.text}") if message.type == "error" else None)


def fulfill_initial_settings(route: Any) -> None:
    # The ordinary optional settings.json consumed by editor.js. This ensures
    # the first rendered frame already has wind and pointer gaze on; the
    # visible WindBlow input is then dragged during the recording too.
    route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps({
            "playbackSettings": {
                "version": 1,
                "wind": {"enabled": True, "wind": 0.35, "windBlow": 0.70},
                "blink": {"intervalMode": "fixed", "intervalMin": 2.8},
                "gaze": {"tracking": True, "range": 1.0, "follow": 0.7},
                "physics": {"enabled": True, "gain": 1.0},
            }
        }),
    )


def setup_page(page: Page, base_url: str, model: str, errors: list[str], with_cursor: bool = False) -> None:
    page_errors(page, errors)
    page.route("**/settings.json", fulfill_initial_settings)
    page.goto(f"{base_url}/editor.html?model={model}", wait_until="networkidle", timeout=60_000)
    page.wait_for_function(EDITOR_READY, timeout=60_000)
    if with_cursor:
        page.evaluate(CURSOR_OVERLAY)
    # These are real controls. They are prepared before frame zero so the
    # first captured frame is clean, centered, and guide-free.
    # Use the authored bust camera so the character reads large beside the
    # real editor controls. The recording keeps the app's native grid and
    # does not resize or replace any editor UI.
    page.locator("#view").select_option("bust")
    page.locator("#guides").uncheck()
    page.locator("#left").evaluate("el => el.scrollTo(0, 0)")
    page.locator("#right").evaluate("el => el.scrollTo(0, 0)")


def box_center(locator: Locator) -> tuple[float, float]:
    box = locator.bounding_box()
    if not box:
        raise RuntimeError(f"Control is not visible: {locator}")
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def range_point(locator: Locator, value: float) -> tuple[float, float]:
    box = locator.bounding_box()
    if not box:
        raise RuntimeError(f"Range input is not visible: {locator}")
    lo = float(locator.get_attribute("min") or 0)
    hi = float(locator.get_attribute("max") or 1)
    frac = 0.5 if hi <= lo else max(0.0, min(1.0, (value - lo) / (hi - lo)))
    # Avoid the browser's tiny native thumb overhang at exact endpoints.
    return box["x"] + 5 + frac * max(1.0, box["width"] - 10), box["y"] + box["height"] / 2


def dispatch_range(locator: Locator, value: float) -> None:
    locator.evaluate(
        "(el, value) => { el.value = String(value); el.dispatchEvent(new Event('input', {bubbles:true})); }",
        value,
    )


class Capture:
    def __init__(self, page: Page, ffmpeg: subprocess.Popen[bytes], fps: int, temp_root: Path,
                 sample_indices: set[int], base_ms: float | None = None):
        self.page = page
        self.ffmpeg = ffmpeg
        self.fps = fps
        self.temp_root = temp_root
        self.sample_indices = sample_indices
        self.frame_index = 0
        self.samples: list[Path] = []
        self.base_ms = float(page.evaluate("performance.now()")) + 2.0 if base_ms is None else base_ms

    def cursor(self, x: float, y: float) -> None:
        self.page.evaluate("([x,y]) => window.__editorRecordCursor?.(x,y)", [x, y])

    def frame(self, x: float | None = None, y: float | None = None) -> None:
        if x is not None and y is not None:
            self.cursor(x, y)
        timestamp = self.base_ms + self.frame_index * (1000.0 / self.fps)
        self.page.evaluate("(now) => window.__editorRecord.step(now)", timestamp)
        jpg = self.page.screenshot(type="jpeg", quality=95)
        if self.ffmpeg.stdin is None:
            raise RuntimeError("ffmpeg stdin is unavailable")
        self.ffmpeg.stdin.write(jpg)
        if self.frame_index in self.sample_indices:
            sample = self.temp_root / f"sample_{self.frame_index:05d}.jpg"
            sample.write_bytes(jpg)
            self.samples.append(sample)
        self.frame_index += 1

    def hold(self, seconds: float, cursor: tuple[float, float] | None = None,
             pointer: Callable[[int, int], tuple[float, float]] | None = None) -> None:
        count = max(1, round(seconds * self.fps))
        for index in range(count):
            point = pointer(index, count) if pointer else cursor
            # Pointer movement must reach the editor's actual stage handler;
            # the overlay alone is only a visual recording aid.
            if pointer and point:
                self.page.mouse.move(*point)
            self.frame(*(point or (None, None)))


def warm_editor(page: Page, fps: int = 30, seconds: float = 0.5) -> float:
    """Advance real editor frames before frame zero so wind is already alive."""
    count = max(1, round(seconds * fps))
    base = float(page.evaluate("performance.now()"))
    step = 1000.0 / fps
    for index in range(1, count + 1):
        page.evaluate("(now) => window.__editorRecord.step(now)", base + index * step)
    return base + count * step


def click_button(cap: Capture, selector: str) -> tuple[float, float]:
    button = cap.page.locator(selector)
    button.scroll_into_view_if_needed()
    point = box_center(button)
    button.click()
    return point


def drag_range(cap: Capture, selector: str, target: float, seconds: float = 0.7) -> tuple[float, float]:
    locator = cap.page.locator(selector)
    locator.scroll_into_view_if_needed()
    current = float(locator.input_value())
    count = max(3, round(seconds * cap.fps))
    start = range_point(locator, current)
    end = range_point(locator, target)
    cap.page.mouse.move(*start)
    cap.page.mouse.down()
    try:
        for index in range(count):
            f = (index + 1) / count
            x = start[0] + (end[0] - start[0]) * f
            y = start[1] + (end[1] - start[1]) * f
            cap.page.mouse.move(x, y)
            cap.frame(x, y)
    finally:
        cap.page.mouse.up()
    # Mouse movement is the recorded gesture; this final real input event
    # makes the intended value exact without bypassing the editor callback.
    dispatch_range(locator, target)
    final = range_point(locator, target)
    cap.cursor(*final)
    return final


def stage_point(page: Page, x_factor: float, y_factor: float) -> tuple[float, float]:
    box = page.locator("#stage").bounding_box()
    if not box:
        raise RuntimeError("stage is not visible")
    return box["x"] + box["width"] * x_factor, box["y"] + box["height"] * y_factor


def record_timeline(cap: Capture) -> None:
    page = cap.page
    wind = "#playbackSettingsLeft input[data-playback-path='wind.windBlow']"

    # Opening frame: both actual playback panels and the full, centered
    # character. WindBlow is already .70 through settings.json.
    cap.hold(0.6, cursor=stage_point(page, 0.50, 0.38))
    drag_range(cap, wind, 0.86, 0.65)
    drag_range(cap, wind, 0.70, 0.65)

    # Pointer gaze is the editor's own pointer handler, with real gaze
    # tracking enabled in the loaded shared playback settings.
    def gaze_path(index: int, count: int) -> tuple[float, float]:
        import math
        u = (index + 1) / count
        return stage_point(page, 0.50 + math.sin(u * math.pi * 2.2) * 0.27,
                           0.34 + math.sin(u * math.pi * 4.4) * 0.13)

    cap.hold(2.1, pointer=gaze_path)

    # Scroll only the left sidebar so real expression buttons and authored
    # mouth controls can be seen beside the still-centered character.
    page.locator("#exprs").scroll_into_view_if_needed()
    happy = "#exprs button:nth-child(2)"
    surprised = "#exprs button:nth-child(3)"
    worried = "#exprs button:nth-child(4)"
    shy = "#exprs button:nth-child(5)"
    mouth_open = '#params .row[title="MouthOpenY"] input'
    mouth_vowel = '#params .row[title="MouthVowel"] input'

    p = click_button(cap, happy)
    cap.hold(0.2, cursor=p)
    drag_range(cap, mouth_open, 0.80, 0.35)
    drag_range(cap, mouth_vowel, 1, 0.35)  # あ
    cap.hold(0.35, cursor=range_point(page.locator(mouth_vowel), 1))
    p = click_button(cap, surprised)
    cap.hold(0.15, cursor=p)
    drag_range(cap, mouth_vowel, 2, 0.35)  # い
    cap.hold(0.3, cursor=range_point(page.locator(mouth_vowel), 2))
    p = click_button(cap, worried)
    cap.hold(0.15, cursor=p)
    drag_range(cap, mouth_vowel, 3, 0.35)  # う
    cap.hold(0.3, cursor=range_point(page.locator(mouth_vowel), 3))
    p = click_button(cap, shy)
    cap.hold(0.15, cursor=p)
    drag_range(cap, mouth_vowel, 4, 0.35)  # え
    cap.hold(0.3, cursor=range_point(page.locator(mouth_vowel), 4))
    p = click_button(cap, "#exprs button:nth-child(1)")
    cap.hold(0.15, cursor=p)
    drag_range(cap, mouth_vowel, 5, 0.35)  # お
    cap.hold(0.3, cursor=range_point(page.locator(mouth_vowel), 5))
    # Close the continuous mouth envelope after the vowel run so later body
    # and rig shots do not hold one mouth shape for the rest of the reel.
    drag_range(cap, mouth_open, 0.0, 0.32)

    # Explicit wink, blink, and return to neutral use the two authored eye
    # range inputs. Automatic fixed interval blinking remains active too.
    eye_l = '#params .row[title="EyeLOpen"] input'
    eye_r = '#params .row[title="EyeROpen"] input'
    drag_range(cap, eye_l, 0.0, 0.28)
    cap.hold(0.22, cursor=range_point(page.locator(eye_l), 0.0))
    drag_range(cap, eye_l, 1.0, 0.24)
    drag_range(cap, eye_r, 0.0, 0.24)
    cap.hold(0.22, cursor=range_point(page.locator(eye_r), 0.0))
    drag_range(cap, eye_l, 0.0, 0.24)
    cap.hold(0.22, cursor=range_point(page.locator(eye_l), 0.0))
    drag_range(cap, eye_r, 1.0, 0.24)
    drag_range(cap, eye_l, 1.0, 0.24)

    # Head roll and asymmetrical body motion through the real parameter panel.
    body_values = [
        ('AngleY', 13.0), ('AngleZ', -18.0), ('BodyAngleX', 6.5),
        ('BodyAngleY', -5.5), ('BodyAngleZ', 7.5),
    ]
    for ident, value in body_values:
        point = drag_range(cap, f'#params .row[title="{ident}"] input', value, 0.34)
        cap.hold(0.18, cursor=point)
    for ident, value in reversed(body_values):
        point = drag_range(cap, f'#params .row[title="{ident}"] input', -value * 0.45, 0.27)
        cap.hold(0.12, cursor=point)

    # Right sidebar independently shows actual rig and chain physics sliders.
    page.locator("#rig").scroll_into_view_if_needed()
    if page.locator("#rig .row").count() < 4:
        raise RuntimeError("Rig controls did not render")
    rig_target = '#rig .row:nth-child(3) input'  # 顔 傾きの回転量
    point = drag_range(cap, rig_target, 1.15, 0.45)
    cap.hold(0.35, cursor=point)
    point = drag_range(cap, rig_target, 0.35, 0.35)
    cap.hold(0.25, cursor=point)
    # This adopted model binds physics through strands, so its legacy chain
    # selector is intentionally empty. Use the real shared physics gain input
    # in the right playback panel instead of inventing a chain control.
    physics_gain = '#playbackSettingsRight input[data-playback-path="physics.gain"]'
    page.locator("#playbackSettingsRight").scroll_into_view_if_needed()
    point = drag_range(cap, physics_gain, 1.35, 0.45)
    cap.hold(0.3, cursor=point)
    point = drag_range(cap, physics_gain, 1.0, 0.38)
    cap.hold(0.25, cursor=point)

    # End with readable top panels on both sides and a centered character.
    page.locator("#left").evaluate("el => el.scrollTo(0, 0)")
    page.locator("#right").evaluate("el => el.scrollTo(0, 0)")
    cap.hold(1.8, pointer=lambda i, n: stage_point(page, 0.50 + 0.12 * ((i % 20) / 19 - 0.5), 0.36))


def make_page(playwright: Any, base_url: str, model: str, width: int, height: int, errors: list[str]) -> tuple[Any, Page]:
    browser = playwright.chromium.launch(args=["--use-angle=d3d11", "--enable-gpu", "--ignore-gpu-blocklist"])
    # Keep the editor at a normal 1280x720 CSS viewport while recording at
    # 1.5 device scale. The encoded pixels are still exactly 1920x1080, and
    # the existing editor labels remain comfortably readable beside the large
    # authored bust view.
    css_width, css_height, dpr = 1280, 720, 1.5
    context = browser.new_context(viewport={"width": css_width, "height": css_height}, device_scale_factor=dpr)
    context.add_init_script(CURSOR_SCRIPT)
    page = context.new_page()
    setup_page(page, base_url, model, errors, with_cursor=True)
    return browser, page


def run_preview(args: argparse.Namespace, base_url: str, output: Path) -> dict[str, Any]:
    errors: list[str] = []
    with sync_playwright() as playwright:
        browser, page = make_page(playwright, base_url, args.model, args.width, args.height, errors)
        try:
            warm_at = warm_editor(page)
            page.evaluate("window.__editorRecordCursor?.(960, 370)")
            output.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output), type="jpeg", quality=95)
            settings = page.evaluate("window.puppet.getPlaybackSettings()")
            controls = page.evaluate("({left:document.querySelectorAll('#left input[type=range]').length,right:document.querySelectorAll('#right input[type=range]').length})")
        finally:
            browser.close()
    if errors:
        raise RuntimeError("Browser runtime errors:\n" + "\n".join(errors))
    if controls["left"] < 10 or controls["right"] < 10:
        raise RuntimeError(f"Expected real controls on both sides, got {controls}")
    if abs(float(settings["wind"]["windBlow"]) - 0.7) > 1e-6:
        raise RuntimeError(f"WindBlow was not enabled from the first frame: {settings['wind']['windBlow']}")
    return {"preview": str(output), "controls": controls, "playbackSettings": settings}


def record(args: argparse.Namespace, base_url: str, temp_root: Path, preview: Path) -> tuple[dict[str, Any], int, list[Path]]:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise RuntimeError("ffmpeg and ffprobe are required")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.qa_dir.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    count = round(args.duration * args.fps)
    sample_times = [0.0, 4.0, 8.0, 12.0, 16.0, max(0.0, args.duration - 0.1)]
    sample_indices = {max(0, min(count - 1, round(t * args.fps))) for t in sample_times}
    ffmpeg: subprocess.Popen[bytes] | None = None
    with sync_playwright() as playwright:
        browser, page = make_page(playwright, base_url, args.model, args.width, args.height, errors)
        try:
            # Preview is captured before the video pipe is opened and is the
            # same real editor state used for frame zero.
            warm_at = warm_editor(page)
            page.evaluate("window.__editorRecordCursor?.(960, 370)")
            page.screenshot(path=str(preview), type="jpeg", quality=95)
            settings = page.evaluate("window.puppet.getPlaybackSettings()")
            if abs(float(settings["wind"]["windBlow"]) - 0.7) > 1e-6:
                raise RuntimeError("Initial editor playback settings did not retain WindBlow 0.70")
            ffmpeg = subprocess.Popen(
                ["ffmpeg", "-v", "error", "-y", "-f", "image2pipe", "-framerate", str(args.fps),
                 "-c:v", "mjpeg", "-i", "-", "-c:v", "libx264", "-preset", args.preset,
                 "-crf", str(args.crf), "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(args.out)],
                stdin=subprocess.PIPE,
            )
            cap = Capture(page, ffmpeg, args.fps, temp_root, sample_indices, base_ms=warm_at)
            record_timeline(cap)
            if cap.frame_index > count:
                raise RuntimeError(f"Timeline emitted {cap.frame_index} frames, expected {count}")
            if cap.frame_index < count:
                # Keep the authored timeline readable while guaranteeing an
                # exact 18-20 second output even if Python's duration rounding
                # changes by one frame on a segment boundary.
                cap.hold((count - cap.frame_index) / args.fps,
                         cursor=stage_point(page, 0.50, 0.36))
            assert ffmpeg.stdin is not None
            ffmpeg.stdin.close()
            ffmpeg.wait(timeout=180)
            if ffmpeg.returncode:
                raise RuntimeError(f"ffmpeg exited with {ffmpeg.returncode}")
            metadata = check_video(args.out, args.width, args.height, args.fps, args.duration)
            if errors:
                raise RuntimeError("Browser runtime errors:\n" + "\n".join(errors))
            return metadata, count, cap.samples
        except Exception:
            if ffmpeg is not None:
                if ffmpeg.stdin and not ffmpeg.stdin.closed:
                    ffmpeg.stdin.close()
                ffmpeg.terminate()
                ffmpeg.wait(timeout=10)
            raise
        finally:
            browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None, help="existing strig serve URL; default starts one")
    parser.add_argument("--no-serve", action="store_true", help="do not start the default strig server")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--model", default="models/aoi")
    parser.add_argument("--out", type=Path, default=Path("docs/media/aoi-showcase.mp4"))
    parser.add_argument("--poster", type=Path, default=Path("docs/media/aoi-showcase.jpg"))
    parser.add_argument("--poster-time", type=float, default=8.0)
    parser.add_argument("--qa-dir", type=Path, default=Path(".local/editor-demo-qa"))
    parser.add_argument("--preview", action="store_true", help="capture the editor preview and exit")
    parser.add_argument("--preview-out", type=Path, default=None)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--crf", type=int, default=18)
    parser.add_argument("--preset", default="slow")
    args = parser.parse_args()
    if (args.width, args.height, args.fps) != (1920, 1080, 30):
        raise SystemExit("The public showcase is fixed at 1920x1080/30fps")
    if args.duration < 18 or args.duration > 20:
        raise SystemExit("Editor showcase duration must be between 18 and 20 seconds")
    if args.no_serve and args.url is None:
        raise SystemExit("--no-serve requires --url")

    repo_root = Path(__file__).resolve().parents[1]
    python = repo_root / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = Path(sys.executable)
    preview = args.preview_out or (args.qa_dir / "preview.jpg")
    temp_dir = Path(tempfile.mkdtemp(prefix="aoi-editor-demo-"))
    try:
        def run(base_url: str) -> None:
            preview_info = run_preview(args, base_url.rstrip("/"), preview)
            if args.preview:
                print(json.dumps(preview_info, ensure_ascii=False, indent=2))
                return
            metadata, count, samples = record(args, base_url.rstrip("/"), temp_dir, preview)
            contact = args.qa_dir / "contact-sheet.jpg"
            make_contact_sheet(samples, contact)
            args.qa_dir.mkdir(parents=True, exist_ok=True)
            for sample in samples:
                (args.qa_dir / sample.name).write_bytes(sample.read_bytes())
            (args.qa_dir / "metadata.json").write_text(json.dumps({
                "video": str(args.out), "preview": str(preview), "contact_sheet": str(contact),
                "frames": count, "ffprobe": metadata,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            make_poster(args.out, args.poster, min(args.poster_time, args.duration - 1 / args.fps))
            print(json.dumps({"video": str(args.out), "poster": str(args.poster), "preview": str(preview),
                              "contact_sheet_verified": True, "frames": count, "ffprobe": metadata},
                             ensure_ascii=False, indent=2))

        if args.url:
            run(args.url)
        else:
            with LocalServer(python, repo_root / "web", args.port) as server:
                run(server.url)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
