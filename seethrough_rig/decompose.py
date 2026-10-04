"""Run the official See-through CLI as an external process.

A *pass* is one run of the CLI on one image. The default set is:

* ``full``  the whole illustration at 1280 (body + head layers)
* ``head``  a square crop around the face, decomposed again so that facial
  parts come out at roughly twice the source pixel density.

More passes (e.g. another resolution or seed) can be added; the build recipe
decides which pass each layer is taken from. Everything is recorded in
``<work>/decompose.json`` so the builder can map layers back to source pixels.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from PIL import Image

from .config import SeeThroughEnv, load_env


def run_seethrough(env: SeeThroughEnv, src: Path, save_dir: Path, resolution: int = 1280,
                   seed: int = 42, steps: int = 30, log=print) -> Path:
    """Run inference_psd_quantized.py and return the ``optimized`` output dir."""
    problems = env.check()
    if problems:
        raise RuntimeError("See-through environment is incomplete:\n  " + "\n  ".join(problems))
    save_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(env.python), "-u", "inference/scripts/inference_psd_quantized.py",
        "--srcp", str(src.resolve()),
        "--save_dir", str(save_dir.resolve()),
        "--resolution", str(resolution),
        "--seed", str(seed),
        "--num_inference_steps", str(steps),
        "--quant_mode", "nf4",
        # NF4 + group offload crashed with an illegal memory access on this
        # Windows setup; plain NF4 peaks at ~7 GB, so offload is not needed.
        "--no_group_offload",
        "--tblr_split",
        "--repo_id_layerdiff", str(env.layerdiff),
        "--repo_id_depth", str(env.depth),
    ]
    penv = dict(os.environ)
    penv["PYTHONPATH"] = str(env.root / "common")
    penv["HF_HOME"] = str(env.models)
    penv["HF_HUB_CACHE"] = str(env.models / "hub")
    penv["HF_XET_CACHE"] = str(env.models / "xet")
    penv["HF_HUB_OFFLINE"] = "1"
    penv["PYTHONIOENCODING"] = "utf-8"
    log(f"[see-through] {src.name} @ {resolution} seed={seed}")
    t0 = time.time()
    logf = save_dir / "seethrough.log"
    with open(logf, "w", encoding="utf-8") as fh:
        proc = subprocess.run(cmd, cwd=env.root, env=penv, stdout=fh, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        tail = logf.read_text(encoding="utf-8", errors="replace")[-3000:]
        raise RuntimeError(f"See-through failed (exit {proc.returncode}). Log tail:\n{tail}")
    out = save_dir / src.stem / "optimized"
    if not (out / "info.json").exists():
        raise RuntimeError(f"See-through finished but {out}/info.json is missing")
    log(f"[see-through] done in {time.time() - t0:.0f}s -> {out}")
    return out


def _frame_to_src_box(info: dict, image_size: tuple[int, int], xyxy) -> list[float]:
    w, h = image_size
    side = max(w, h)
    s = side / info["frame_size"][0]
    padx, pady = (side - w) / 2, (side - h) / 2
    return [xyxy[0] * s - padx, xyxy[1] * s - pady, xyxy[2] * s - padx, xyxy[3] * s - pady]


def head_crop_box(full_dir: Path, image_size: tuple[int, int]) -> list[int]:
    """Square crop (x0, y0, x1, y1) in source pixels around the face.

    Tight enough that the head pass samples the face at ~2x source density,
    loose enough that eyes, brows, ears and mouth are fully inside.
    """
    info = json.loads((full_dir / "info.json").read_text(encoding="utf-8"))
    parts = info["parts"]
    face = _frame_to_src_box(info, image_size, parts["face"]["xyxy"])
    fw, fh = face[2] - face[0], face[3] - face[1]
    boxes = [face]
    for tag in ("ears-l", "ears-r", "ears", "eyebrow-l", "eyebrow-r"):
        if tag in parts:
            boxes.append(_frame_to_src_box(info, image_size, parts[tag]["xyxy"]))
    x0 = min(b[0] for b in boxes) - 0.12 * fw
    x1 = max(b[2] for b in boxes) + 0.12 * fw
    y0 = face[1] - 0.08 * fh
    y1 = face[3] + 0.12 * fh
    size = max(x1 - x0, y1 - y0)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    w, h = image_size
    side = int(round(min(size, w, h)))
    x0 = int(round(cx - side / 2))
    y0 = int(round(cy - side / 2))
    x0 = min(max(x0, 0), w - side)
    y0 = min(max(y0, 0), h - side)
    return [x0, y0, x0 + side, y0 + side]


def _load_meta(work: Path) -> dict:
    p = work / "decompose.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"passes": {}}


def _save_meta(work: Path, meta: dict):
    (work / "decompose.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def run_pass(work: Path, name: str, kind: str, resolution: int, seed: int = 42, reuse: bool = True, log=print) -> dict:
    """Run (or reuse) one pass. kind: 'full' or 'head'."""
    env = load_env()
    meta = _load_meta(work)
    src = work / "source.png"
    size = Image.open(src).size
    pdir = work / "passes" / name
    if kind == "full":
        img_path = src
        box = [0, 0, size[0], size[1]]
    else:
        full = next((p for p in meta["passes"].values() if p["kind"] == "full"), None)
        if full is None:
            raise RuntimeError("run a full pass before the head pass")
        box = head_crop_box(Path(full["dir"]), size)
        img_path = work / f"{name}.png"
        Image.open(src).convert("RGBA").crop(box).save(img_path)
    out = pdir / img_path.stem / "optimized"
    if not (reuse and (out / "info.json").exists()):
        out = run_seethrough(env, img_path, pdir, resolution, seed=seed, log=log)
    else:
        log(f"[decompose] reusing pass '{name}' at {out}")
    meta["source_size"] = list(size)
    meta["passes"][name] = {"kind": kind, "dir": str(out.resolve()), "box": box,
                            "resolution": resolution, "seed": seed}
    _save_meta(work, meta)
    return meta


def decompose(src: Path, work: Path, resolution: int = 1280, head_resolution: int = 1280,
              extra_full: list[int] | None = None, reuse: bool = True, log=print) -> dict:
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    img = Image.open(src).convert("RGBA")
    img.save(work / "source.png")
    meta = _load_meta(work)
    meta["source"] = str(Path(src).resolve())
    _save_meta(work, meta)
    run_pass(work, "full", "full", resolution, reuse=reuse, log=log)
    for r in extra_full or []:
        run_pass(work, f"full{r}", "full", r, reuse=reuse, log=log)
    return run_pass(work, "head", "head", head_resolution, reuse=reuse, log=log)
