"""Locations of the external See-through installation.

Everything can be overridden with environment variables so the tool is not tied
to one machine:

    SEETHROUGH_ROOT     official repository checkout
    SEETHROUGH_PYTHON   python executable of the environment that has its deps
    SEETHROUGH_MODELS   directory that holds the HF cache (hub/xet) and model dirs
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SeeThroughEnv:
    root: Path
    python: Path
    models: Path

    @property
    def layerdiff(self) -> Path:
        return _first_dir(self.models / "models" / "layerdiff")

    @property
    def depth(self) -> Path:
        return _first_dir(self.models / "models" / "depth")

    def check(self) -> list[str]:
        problems = []
        for label, p in [("root", self.root), ("python", self.python), ("models", self.models)]:
            if not p.exists():
                problems.append(f"{label} not found: {p}")
        script = self.root / "inference" / "scripts" / "inference_psd_quantized.py"
        if not script.exists():
            problems.append(f"inference script not found: {script}")
        return problems


def _first_dir(parent: Path) -> Path:
    if not parent.exists():
        return parent
    dirs = sorted(d for d in parent.iterdir() if d.is_dir())
    return dirs[0] if dirs else parent


def load_env() -> SeeThroughEnv:
    return SeeThroughEnv(
        root=Path(os.environ.get("SEETHROUGH_ROOT", r"F:\AI\sources\see-through")),
        python=Path(os.environ.get("SEETHROUGH_PYTHON", r"F:\AI\runtimes\see-through\Scripts\python.exe")),
        models=Path(os.environ.get("SEETHROUGH_MODELS", r"F:\AI\models\see-through")),
    )
