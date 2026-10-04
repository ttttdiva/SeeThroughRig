"""Generate head-angle keyform images with the Codex CLI image tool, align them
to the neutral crop and decompose them with See-through.

    strig keyforms assets/aoi/original.png --work work/keyforms --box 90 0 950 860

Steps (all cached, rerunnable):
  1. neutral.png  = crop of the source (box) resized to 1024
  2. for each key: `codex exec` with the neutral (and reference poses for
     in-between / diagonal keys) -> <name>_v1.png
  3. align to the neutral on the shoulders/chest (similarity, RANSAC) -> <name>.png
  4. See-through decomposition -> st/<name>/<name>/optimized
The generated images must be checked by eye; bad ones can be deleted and the
command rerun to regenerate only those.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

from .config import load_env
from .decompose import run_seethrough
from .keyforms import align_to_neutral, load_rgb

COMMON = """Rules:
- Same character, same anime art style, same line weight, same colors and shading as image 1, same 1:1 framing. Nothing is zoomed or cropped.
- Shoulders, collar, chest, the knot, the ties hanging below the shoulders and the background stay exactly where they are in image 1.
- Only the head (face, bangs, side hair near the face, accessories on the head) moves. The head keeps its size.
- Neutral gentle smile exactly as in image 1, eyes open, looking in the direction the face points.
- Plain background like image 1. No text, no border."""

HEAD = "Use your image generation tool to create ONE image. Do not run any shell commands; just generate the image.\n\n"

DIR = {
    "AngleX:30": "turned about 30 degrees to the viewer's right (3/4 view: far cheek and eye narrower, nose towards the right, near ear shows more)",
    "AngleX:-30": "turned about 30 degrees to the viewer's left (3/4 view: far cheek and eye narrower, nose towards the left, near ear shows more)",
    "AngleY:30": "tilted up about 15 degrees, chin raised a little (seen slightly from below, eyes, nose and mouth move up a little); subtle",
    "AngleY:-30": "tilted down about 25 degrees, chin lowered (seen slightly from above, more of the top of the head, bangs reach the eyes)",
}


def key_name(key: str) -> str:
    return "k_" + re.sub(r"[^0-9a-zA-Z]+", "_", key).strip("_")


def parse(key: str) -> dict[str, int]:
    return {a: int(v) for a, v in (kv.split(":") for kv in key.split(","))}


def fmt(d: dict[str, int]) -> str:
    return ",".join(f"{a}:{d[a]}" for a in ("AngleX", "AngleY") if d.get(a))


def prompt_for(key: str) -> tuple[str, list[str]]:
    """Prompt text and the reference keys to attach (after the neutral)."""
    d = parse(key)
    full = {a: (30 if v > 0 else -30) for a, v in d.items()}
    half = any(abs(v) < 30 for v in d.values())
    if len(d) == 1 and not half:
        return HEAD + f"Image 1 is an anime illustration. Edit it: the girl's HEAD is {DIR[key]}, as a keyform for a Live2D-style rig.\n{COMMON}", []
    if not half:
        kx, ky = fmt({"AngleX": d["AngleX"]}), fmt({"AngleY": d["AngleY"]})
        return (HEAD + "Image 1 is the original illustration. Image 2 shows the head " + DIR[kx] + ". Image 3 shows the head " + DIR[ky]
                + ".\nCreate the COMBINED pose: the head turned like image 2 AND tilted like image 3 at the same time, as a diagonal keyform.\n"
                + COMMON), [kx, ky]
    ref = fmt(full)
    return (HEAD + "Image 1 is the original illustration (head facing front). Image 2 is the same girl with her head in a stronger pose."
            + "\nCreate the IN-BETWEEN pose exactly halfway between image 1 and image 2 (the head moves only half as much as in image 2).\n"
            + COMMON), [ref]


def codex_generate(prompt: str, images: list[Path], out: Path, log=print) -> bool:
    codex = shutil.which("codex") or "codex"
    cmd = [codex, "exec", "-s", "read-only", "--skip-git-repo-check"]
    for im in images:
        cmd += ["-i", str(im)]
    cmd += ["-"]
    logf = out.with_suffix(".log")
    with open(logf, "w", encoding="utf-8") as fh:
        subprocess.run(cmd, input=prompt, text=True, encoding="utf-8", stdout=fh, stderr=subprocess.STDOUT)
    text = logf.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"session id: ([0-9a-f-]+)", text)
    if not m:
        log(f"  ! codex: no session id in {logf}")
        return False
    gen = Path.home() / ".codex" / "generated_images" / m.group(1)
    pngs = sorted(gen.glob("*.png")) if gen.exists() else []
    if not pngs:
        log(f"  ! codex produced no image for {out.name} (see {logf})")
        return False
    shutil.copy(pngs[-1], out)
    return True


DEFAULT_KEYS = ["AngleX:30", "AngleX:-30", "AngleY:30", "AngleY:-30",
                "AngleX:30,AngleY:30", "AngleX:-30,AngleY:30", "AngleX:30,AngleY:-30", "AngleX:-30,AngleY:-30",
                "AngleX:15", "AngleX:-15", "AngleY:15", "AngleY:-15",
                "AngleX:15,AngleY:15", "AngleX:-15,AngleY:15", "AngleX:15,AngleY:-15", "AngleX:-15,AngleY:-15"]


def generate_keyforms(src: Path, work: Path, box, keys=None, size: int = 1024, seed: int = 123,
                      decompose: bool = True, log=print) -> dict[str, str]:
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    neutral = work / "neutral.png"
    if not neutral.exists():
        Image.open(src).convert("RGB").crop(tuple(box)).resize((size, size), Image.LANCZOS).save(neutral)
    n = load_rgb(neutral, size)
    keys = keys or DEFAULT_KEYS
    done: dict[str, Path] = {}
    # single-axis full keys first, then diagonals, then in-betweens (they reference earlier ones)
    order = sorted(keys, key=lambda k: (any(abs(v) < 30 for v in parse(k).values()), len(parse(k))))
    for key in order:
        name = key_name(key)
        aligned = work / f"{name}.png"
        if not aligned.exists():
            raw = work / f"{name}_raw.png"
            if not raw.exists():
                prompt, refs = prompt_for(key)
                imgs = [neutral] + [work / f"{key_name(r)}.png" for r in refs]
                if not all(p.exists() for p in imgs):
                    log(f"  ! {key}: reference images missing, skipped")
                    continue
                log(f"[keyforms] generating {key}")
                if not codex_generate(prompt, imgs, raw, log):
                    continue
            pa, _ = align_to_neutral(n, load_rgb(raw, size))
            Image.fromarray((pa * 255).astype(np.uint8)).save(aligned)
        done[key] = aligned
    out: dict[str, str] = {}
    if decompose:
        env = load_env()
        for name_path in [neutral] + list(done.values()):
            name = name_path.stem
            opt = work / "st" / name / name / "optimized"
            if not (opt / "info.json").exists():
                run_seethrough(env, name_path, work / "st" / name, resolution=size, seed=seed, log=log)
        for key, p in done.items():
            out[key] = str(work / "st" / p.stem / p.stem / "optimized")
    log(f"[keyforms] {len(done)} keyform images ready")
    return out
