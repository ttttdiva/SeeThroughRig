"""Command line entry point: ``strig <command>``."""
from __future__ import annotations

import argparse
from pathlib import Path


def main(argv=None):
    ap = argparse.ArgumentParser(prog="strig", description="See-through based 2D puppet rigging tool")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("decompose", help="run See-through on an illustration (full + head pass)")
    p.add_argument("src", type=Path)
    p.add_argument("--work", type=Path, default=Path("work/aoi"))
    p.add_argument("--resolution", type=int, default=1280)
    p.add_argument("--head-resolution", type=int, default=1280)
    p.add_argument("--extra-full", type=int, nargs="*", default=[],
                   help="additional full-image passes at these resolutions (layers can be picked per tag)")
    p.add_argument("--force", action="store_true", help="re-run even if outputs exist")

    p = sub.add_parser("build", help="build model.json + atlases from a recipe")
    p.add_argument("recipe", type=Path)
    p.add_argument("--out", type=Path, default=Path("web/models/aoi"))
    p.add_argument("--dump-only", action="store_true", help="only write the keyform fitting inputs")

    p = sub.add_parser("keyforms", help="generate head-angle keyform images with Codex and decompose them")
    p.add_argument("src", type=Path)
    p.add_argument("--work", type=Path, default=Path("work/keyforms_auto"))
    p.add_argument("--box", type=int, nargs=4, required=True, help="source crop x0 y0 x1 y1 (square)")
    p.add_argument("--keys", nargs="*", default=None, help="e.g. AngleX:30 AngleX:30,AngleY:-30")
    p.add_argument("--no-decompose", action="store_true")

    p = sub.add_parser("serve", help="serve the editor / player")
    p.add_argument("--root", type=Path, default=Path("web"))
    p.add_argument("--port", type=int, default=8765)

    args = ap.parse_args(argv)
    if args.cmd == "decompose":
        from .decompose import decompose
        decompose(args.src, args.work, args.resolution, args.head_resolution, args.extra_full, reuse=not args.force)
    elif args.cmd == "build":
        from .build import build
        build(args.recipe, args.out, dump_only=args.dump_only)
    elif args.cmd == "keyforms":
        from .genkeys import generate_keyforms
        res = generate_keyforms(args.src, args.work, args.box, args.keys, decompose=not args.no_decompose)
        for k, v in res.items():
            print(f'      "{k}": "{v}",')
    elif args.cmd == "serve":
        from .server import serve
        serve(args.root, args.port)


if __name__ == "__main__":
    main()
