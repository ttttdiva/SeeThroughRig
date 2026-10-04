"""Record the deterministic Aoi public showcase.

The page is rendered by Chromium's WebGL2 path at a fixed 30 fps.  A small
temporary HTTP server is started by default so a checkout can reproduce the
recording without a separate terminal process.

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
from typing import Any

from playwright.sync_api import Page, sync_playwright


REQUIRED_CAPABILITIES = {
    "AngleY",
    "AngleZ",
    "BodyAngleX",
    "BodyAngleY",
    "BodyAngleZ",
    "Breath",
    "EyeBallX",
    "EyeBallY",
    "EyeLOpen",
    "EyeROpen",
    "MouthOpenY",
    "MouthVowel",
    "Wind",
    "WindBlow",
    "PhysicsGain",
}


def wait_for_http(url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/showcase.html", timeout=1.5) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError) as error:
            last_error = error
        time.sleep(0.2)
    raise RuntimeError(f"HTTP server did not become ready at {url}: {last_error}")


class LocalServer:
    def __init__(self, root: Path, host: str, port: int):
        self.root = root
        self.host = host
        self.port = port
        self.process: subprocess.Popen[bytes] | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def __enter__(self) -> "LocalServer":
        self.process = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(self.port), "--bind", self.host, "--directory", str(self.root)],
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
        check=True,
        capture_output=True,
        text=True,
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
        "width": stream.get("width"),
        "height": stream.get("height"),
        "fps": actual_rate,
        "frames": stream.get("nb_frames"),
        "duration": actual_duration,
        "codec": stream.get("codec_name"),
        "format": metadata.get("format", {}).get("format_name"),
    }


def make_poster(video: Path, poster: Path, second: float) -> None:
    poster.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-ss", f"{second:.3f}", "-i", str(video),
            "-frames:v", "1", "-q:v", "2", "-vf", "scale=1920:1080:flags=lanczos", str(poster),
        ],
        check=True,
    )


def make_contact_sheet(frames: list[Path], output: Path) -> None:
    if not frames:
        raise RuntimeError("No sample frames were captured for contact-sheet QA")
    concat = output.with_suffix(".txt")
    concat.write_text("\n".join(f"file '{frame.as_posix()}'" for frame in frames), encoding="utf-8")
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
            "-vf", "scale=480:-2,tile=4x3:padding=8:margin=8:color=0x071126",
            "-frames:v", "1", "-q:v", "3", str(output),
        ],
        check=True,
    )
    concat.unlink(missing_ok=True)
    if not output.exists() or output.stat().st_size < 1000:
        raise RuntimeError("Contact-sheet QA image was not produced")


def preserve_qa(output_dir: Path, samples: list[Path], contact: Path, metadata: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for old in output_dir.glob("sample_*.jpg"):
        old.unlink()
    (output_dir / "contact-sheet.jpg").write_bytes(contact.read_bytes())
    for sample in samples:
        (output_dir / sample.name).write_bytes(sample.read_bytes())
    (output_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")


def page_errors(page: Page, errors: list[str]) -> None:
    page.on("pageerror", lambda error: errors.append(f"pageerror: {error}"))
    page.on("console", lambda message: errors.append(f"console: {message.text}") if message.type == "error" else None)


def smoke(args: argparse.Namespace, base_url: str) -> None:
    errors: list[str] = []
    args.smoke_out.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--use-angle=d3d11", "--enable-gpu", "--ignore-gpu-blocklist"])
        page = browser.new_page(viewport={"width": args.width, "height": args.height}, device_scale_factor=1)
        page_errors(page, errors)
        page.goto(f"{base_url}/showcase.html?record=1&model={args.model}&motion={args.motion}&seed={args.seed}", wait_until="networkidle", timeout=60_000)
        page.wait_for_function("window.__showcaseReady === true", timeout=60_000)
        if page.evaluate("window.__showcaseError || null"):
            raise RuntimeError(page.evaluate("window.__showcaseError"))
        page.evaluate("(value) => window.__showcase.frame(value, 30)", args.smoke_time)
        page.screenshot(path=str(args.smoke_out), type="jpeg", quality=95)
        capabilities = page.evaluate("window.__showcase.capabilities")
        browser.close()
    if errors:
        raise RuntimeError("Browser runtime errors:\n" + "\n".join(errors))
    print(json.dumps({"smoke": True, "screenshot": str(args.smoke_out), "time": args.smoke_time, "capabilities": capabilities}, ensure_ascii=False, indent=2))


def record(args: argparse.Namespace, base_url: str, temp_root: Path) -> tuple[dict[str, Any], int, list[Path]]:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise RuntimeError("ffmpeg and ffprobe are required")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.poster:
        args.poster.parent.mkdir(parents=True, exist_ok=True)
    frames_dir = args.frames_dir
    if frames_dir:
        frames_dir.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    samples: list[Path] = []
    sample_times = [0.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 35.0, 40.0, max(0.0, args.duration - 0.1)]
    sample_indices = {max(1, min(round(t * args.fps), round(args.duration * args.fps))): t for t in sample_times}
    ffmpeg: subprocess.Popen[bytes] | None = None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--use-angle=d3d11", "--enable-gpu", "--ignore-gpu-blocklist"])
        page = browser.new_page(viewport={"width": args.width, "height": args.height}, device_scale_factor=1)
        page_errors(page, errors)
        query = f"showcase.html?record=1&model={args.model}&motion={args.motion}&seed={args.seed}"
        page.goto(f"{base_url}/{query}", wait_until="networkidle", timeout=60_000)
        page.wait_for_function("window.__showcaseReady === true", timeout=60_000)
        if page.evaluate("window.__showcaseError || null"):
            raise RuntimeError(page.evaluate("window.__showcaseError"))
        capabilities = page.evaluate("window.__showcase.capabilities")
        supported = set(capabilities.get("parameters", []))
        missing = sorted(REQUIRED_CAPABILITIES - supported)
        if missing:
            raise RuntimeError(f"Showcase model is missing required capabilities: {', '.join(missing)}")
        if "AngleX" in supported:
            raise RuntimeError("Showcase model unexpectedly reports unsupported AngleX")
        if sorted(capabilities.get("mouthVowels", [])) != ["a", "e", "i", "o", "u"]:
            raise RuntimeError(f"Expected five authored vowels, got {capabilities.get('mouthVowels')}")
        duration = float(page.evaluate("window.__showcase.duration"))
        if args.duration is not None and abs(duration - args.duration) > 1e-6:
            raise RuntimeError(f"Motion duration mismatch: page={duration} CLI={args.duration}")
        duration = args.duration if args.duration is not None else duration
        count = round(duration * args.fps)
        print(f"WebGL ready: model={args.model} duration={duration:.3f}s frames={count}")
        print(f"Capabilities: {', '.join(sorted(supported))}")
        ffmpeg = subprocess.Popen(
            [
                "ffmpeg", "-v", "error", "-y", "-f", "image2pipe", "-framerate", str(args.fps),
                "-c:v", "mjpeg", "-i", "-", "-c:v", "libx264", "-preset", args.preset,
                "-crf", str(args.crf), "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(args.out),
            ],
            stdin=subprocess.PIPE,
        )
        assert ffmpeg.stdin is not None
        start = time.monotonic()
        try:
            for index in range(1, count + 1):
                t = index / args.fps
                page.evaluate("(value) => window.__showcase.frame(value, 30)", t)
                jpg = page.screenshot(type="jpeg", quality=95)
                ffmpeg.stdin.write(jpg)
                if index in sample_indices:
                    sample_path = temp_root / f"sample_{index:05d}.jpg"
                    sample_path.write_bytes(jpg)
                    samples.append(sample_path)
                if frames_dir and index % args.fps == 0:
                    (frames_dir / f"f{index:05d}.jpg").write_bytes(jpg)
                if index == 1 or index % (args.fps * 5) == 0 or index == count:
                    elapsed = time.monotonic() - start
                    print(f"  frame {index}/{count} t={t:.1f}s ({elapsed:.1f}s elapsed)", file=sys.stderr)
            ffmpeg.stdin.close()
            ffmpeg.wait(timeout=180)
            if ffmpeg.returncode:
                raise RuntimeError(f"ffmpeg exited with {ffmpeg.returncode}")
        except Exception:
            if ffmpeg.stdin and not ffmpeg.stdin.closed:
                ffmpeg.stdin.close()
            ffmpeg.terminate()
            ffmpeg.wait(timeout=10)
            raise
        finally:
            browser.close()
    if errors:
        raise RuntimeError("Browser runtime errors:\n" + "\n".join(errors))
    metadata = check_video(args.out, args.width, args.height, args.fps, duration)
    return metadata, count, samples


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None, help="existing web root URL; default starts 127.0.0.1:8877")
    parser.add_argument("--no-serve", action="store_true", help="do not start the default local HTTP server")
    parser.add_argument("--port", type=int, default=8877)
    parser.add_argument("--model", default="models/aoi")
    parser.add_argument("--motion", default="motions/showcase.json")
    parser.add_argument("--out", type=Path, default=Path("docs/media/aoi-showcase.mp4"))
    parser.add_argument("--poster", type=Path, default=Path("docs/media/aoi-showcase.jpg"))
    parser.add_argument("--poster-time", type=float, default=5.0)
    parser.add_argument("--qa-dir", type=Path, default=None, help="preserve sample frames/contact sheet for visual QA")
    parser.add_argument("--smoke", action="store_true", help="render one WebGL frame and exit")
    parser.add_argument("--smoke-time", type=float, default=5.0)
    parser.add_argument("--smoke-out", type=Path, default=Path("docs/media/aoi-showcase.jpg"))
    parser.add_argument("--frames-dir", type=Path, default=None, help="also save one JPEG per second here")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--duration", type=float, default=45.0)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--crf", type=int, default=18)
    parser.add_argument("--preset", default="slow")
    args = parser.parse_args()
    if args.width != 1920 or args.height != 1080 or args.fps != 30:
        raise SystemExit("The public showcase is fixed at 1920x1080/30fps")
    if args.duration < 35 or args.duration > 50:
        raise SystemExit("Showcase duration must be between 35 and 50 seconds")
    if args.no_serve and args.url is None:
        raise SystemExit("--no-serve requires --url")

    repo_root = Path(__file__).resolve().parents[1]
    temp_dir = Path(tempfile.mkdtemp(prefix="aoi-showcase-"))
    try:
        if args.url:
            if args.smoke:
                smoke(args, args.url.rstrip("/"))
                return
            metadata, count, samples = record(args, args.url.rstrip("/"), temp_dir)
        else:
            with LocalServer(repo_root / "web", "127.0.0.1", args.port) as server:
                if args.smoke:
                    smoke(args, server.url)
                    return
                metadata, count, samples = record(args, server.url, temp_dir)
        contact = temp_dir / "contact-sheet.jpg"
        make_contact_sheet(samples, contact)
        if args.qa_dir:
            preserve_qa(args.qa_dir, samples, contact, metadata)
        make_poster(args.out, args.poster, min(args.poster_time, args.duration - 1 / args.fps))
        print(json.dumps({"video": str(args.out), "poster": str(args.poster), "contact_sheet_verified": True, "frames": count, "ffprobe": metadata}, ensure_ascii=False, indent=2))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
