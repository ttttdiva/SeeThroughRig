"""Create a reviewed-art candidate recipe in the same directory as its base.

python scripts/register_face_keys.py --recipe recipes/aoi_rig.json \
    --images assets/aoi/vowels --output recipes/aoi_vowels.json
Expects mouth_a.png, mouth_i.png, mouth_u.png, mouth_e.png, mouth_o.png.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from seethrough_rig.build import load_recipe
from seethrough_rig.face_keys import VOWELS, with_vowel_keys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recipe', type=Path, required=True)
    parser.add_argument('--images', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    recipe, output = args.recipe.resolve(), args.output.resolve()
    if output.parent != recipe.parent or output == recipe:
        parser.error('Output must be a new recipe in the same recipes directory')
    if output.exists():
        parser.error('Output already exists; use a new candidate filename')
    images = {v: os.path.relpath((args.images / f'mouth_{v}.png').resolve(), recipe.parent) for v in VOWELS}
    candidate = with_vowel_keys(load_recipe(recipe), images, recipe.parent)
    with output.open('x', encoding='utf-8') as stream:
        json.dump(candidate, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(f'Registered five vowel keys: {output}')
    print('Build/export and visually review this candidate before production use.')


if __name__ == '__main__':
    main()
