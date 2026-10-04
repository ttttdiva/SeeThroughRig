"""Generate the canonical data-free ESM runtime, including exact pinned Git engine.

Usage: .venv/Scripts/python.exe scripts/package_runtime.py --output PATH --source-ref <commit>
No models/artwork/inference dependencies are copied. Existing notices are retained.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "46c0037779fb5e59715030925d66abdc6c09ca12"
CORE = ("model.js", "puppet.js", "motion.js", "renderer.js", "params.js", "deform.js", "physics.js")
PINNED = ("motion.js", "params.js", "deform.js", "physics.js")
ADAPTER = ("index.js", "index.d.ts", "host.js", "engine.js")
PLAYBACK = ("playback-settings.js", "playback-settings-schema.js", "playback-settings.schema.json", "playback-settings-panel.js")

def git(*args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(ROOT), *args], stderr=subprocess.PIPE)

def build(output: Path, source_ref: str | None = None) -> dict:
    output = output.resolve()
    if output == ROOT or ROOT in output.parents and any(p in {"web", "scripts", "tests", "seethrough_rig"} for p in output.relative_to(ROOT).parts[:1]):
        raise ValueError("Output must not overwrite source directories")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Output directory must be empty; use a fresh staging directory")
    generator_head = git("rev-parse", "HEAD").decode().strip()
    head = git("rev-parse", "--verify", (source_ref or "HEAD") + "^{commit}").decode().strip()
    def source_bytes(path: str) -> bytes:
        return git("show", head + ":" + path) if source_ref else (ROOT / path).read_bytes()
    pinned = git("rev-parse", BASELINE + "^{commit}").decode().strip()
    if pinned != BASELINE:
        raise ValueError("Pinned baseline resolved to unexpected commit")
    payload: dict[str, bytes] = {}
    origins: dict[str, dict] = {}
    def add(path: str, data: bytes, source_path: str, commit: str | None, source: str):
        payload[path] = data
        origins[path] = {"source_path": source_path, "source_commit": commit, "source": source}
    for f in CORE:
        path = "web/js/core/" + f
        data = source_bytes(path)
        try:
            committed = git("show", head + ":" + path)
        except subprocess.CalledProcessError:
            committed = None
        add(path, data, path, head if committed == data else None, "git" if committed == data else "working-tree")
    for f in PINNED:
        source_path = "web/js/core/" + f
        data = git("show", BASELINE + ":" + source_path)
        add("web/js/runtime-baseline/" + f, data, source_path, BASELINE, "git")
    adapter_files = ADAPTER
    # Earlier immutable runtime commits predate shared settings; retain the
    # ability to distribute those exact releases without reading future files.
    if b"./playback-settings.js" in source_bytes("web/js/runtime/index.js"):
        adapter_files += PLAYBACK
        schema = json.loads(source_bytes("web/js/runtime/playback-settings.schema.json"))
        expected = "// Generated from playback-settings.schema.json. Regenerate with scripts/generate_playback_schema.py.\nexport default " + json.dumps(schema, ensure_ascii=False, indent=2) + ";\n"
        if source_bytes("web/js/runtime/playback-settings-schema.js") != expected.encode():
            raise ValueError("Playback schema module is stale; run scripts/generate_playback_schema.py")
    for f in adapter_files:
        path = "web/js/runtime/" + f
        data = source_bytes(path)
        try:
            committed = git("show", head + ":" + path)
        except subprocess.CalledProcessError:
            committed = None
        add(path, data, path, head if committed == data else None, "git" if committed == data else "working-tree")
    package = json.loads(source_bytes("web/js/runtime/package.json"))
    if package["name"] != "@aoitalk/seethrough-rig-runtime" or package["version"] != "0.1.0":
        raise ValueError("Unexpected runtime package identity")
    add("package.json", (json.dumps(package, indent=2, ensure_ascii=False) + "\n").encode(), "web/js/runtime/package.json", head if source_ref else None, "generated")
    add("index.js", b"export * from './web/js/runtime/index.js';\n", "web/js/runtime/index.js", head if source_ref else None, "generated")
    add("index.d.ts", source_bytes("web/js/runtime/index.d.ts"), "web/js/runtime/index.d.ts", head if source_ref else None, "git" if source_ref else "working-tree")
    notice = """# First-party source notice
This package contains See-through Rig repository source and retains existing source comments/notices.
No LICENSE was present in the upstream repository at packaging time. First-party upstream license is unspecified.
The package.json UNLICENSED value supplies no license grant. No third-party license or MIT license is assumed.
Baseline motion/physics/deformation/parameters are exact Git source from commit """ + BASELINE + "\n"
    add("FIRST_PARTY_NOTICE.md", notice.encode(), "generated", None, "generated")
    add("README.md", source_bytes("docs/runtime.md"), "docs/runtime.md", head if source_ref else None, "git" if source_ref else "working-tree")
    files = [{"path": p, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), **origins[p]} for p, data in sorted(payload.items())]
    provenance = {"package": package["name"], "version": "0.1.0", "abi": 1, "source_head": head, "generator_commit": generator_head, "source_mode": "git" if source_ref else "working-tree", "engines": [{"id": "canonical", "source": "git" if source_ref else "working-tree", "head": head}, {"id": "baseline46c", "core_ref": BASELINE, "source": "git"}], "first_party_upstream_license": "unspecified", "third_party_dependencies": [], "files": files}
    payload["runtime-provenance.json"] = (json.dumps(provenance, indent=2, ensure_ascii=False) + "\n").encode()
    # Validate/gather every source before publishing. Directory renames are not
    # supported by some restricted Windows hosts; publish files into a new empty
    # directory and write the provenance completion marker last.
    output.mkdir(parents=True, exist_ok=True)
    order = [p for p in payload if p not in {"package.json", "runtime-provenance.json"}]
    order += ["package.json", "runtime-provenance.json"]
    for p in order:
        target = output / p
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(payload[p])
    return {"package_path": str(output), "name": package["name"], "version": "0.1.0", "abi": 1, "baseline_core_ref": BASELINE, "file_count": len(payload), "provenance": str(output / "runtime-provenance.json")}

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-ref", help="Build from an immutable Git commit, ignoring worktree/CRLF differences; required for release")
    args = parser.parse_args()
    try:
        print(json.dumps({"ok": True, **build(args.output, args.source_ref)}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
