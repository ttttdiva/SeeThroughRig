"""Register discrete vowel artwork without changing the legacy mouth path.

The input images use the existing expressions.box coordinate space. This is a
recipe transform, not image generation or phoneme recognition. Assets must be
reviewed before promoting a candidate model to the production model path.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from PIL import Image

VOWELS = ('a', 'i', 'u', 'e', 'o')


def with_vowel_keys(recipe: dict, images: dict[str, str], base_dir: Path | None = None) -> dict:
    """Return a separate candidate recipe; reject missing/mismatched input art."""
    if set(images) != set(VOWELS):
        raise ValueError('Exactly five vowel images are required: a, i, u, e, o')
    out = deepcopy(recipe)
    ev = out.get('expressions', {})
    if not ev.get('box') or 'mouth' not in out.get('layers', {}):
        raise ValueError('A source-space expressions.box and neutral mouth layer are required')
    if any(name.startswith('vowel_') for name in ev.get('mouth', {})):
        raise ValueError('Vowel keys are already registered')
    if base_dir is not None:
        sizes = set()
        for path in [*ev.get('images', {}).values(), *images.values()]:
            with Image.open(base_dir / path) as image:
                image.load()
                sizes.add(image.size)
        if len(sizes) != 1:
            raise ValueError('All expression images must have identical dimensions and alignment')
    # Zero preserves every existing expression and MouthOpenY binding.
    legacy = ['MouthVowel', [[0, 1], [1, 0]]]
    # Keep the neutral/expression closed mouth when articulation is closed.
    # Existing half/open images disappear only while a vowel is selected.
    for name, spec in ev.get('mouth', {}).items():
        if name in ('mouth_half', 'mouth_open'):
            spec.setdefault('opacity', []).append(deepcopy(legacy))
    for index, vowel in enumerate(VOWELS, 1):
        name = 'vowel_' + vowel
        ev.setdefault('images', {})[name] = images[vowel]
        ev.setdefault('mouth', {})[name] = {
            'opacity': [
                ['MouthVowel', [[index - 1, 0], [index, 1], [index + 1, 0]]],
                ['MouthOpenY', [[0.179, 0], [0.18, 1]]],
                {'oneMinusSum': ['MouthSurprised'], 'lo': 0.499, 'hi': 0.5},
            ],
            'open': False,
        }
    out['expressions'] = ev
    return out
