import numpy as np

from seethrough_rig.build import restore_visible
from seethrough_rig.layers import Layer


def test_opt_in_boundary_restores_rgb_fringe_without_changing_aperture():
    rgba = np.full((24, 24, 4), .25, np.float32)
    rgba[..., 3] = 1
    rgba[0, :, 3] = .2
    original = rgba.copy()
    white = Layer('white', rgba, (0, 0), 1)
    source = np.full((24, 24, 3), .9, np.float32)
    restore_visible([white], source, boundary_tags=['white'])
    np.testing.assert_array_equal(white.alpha, original[..., 3])
    np.testing.assert_allclose(white.rgba[0, :, :3], .9)
    ordinary = Layer('ordinary', original.copy(), (0, 0), 1)
    restore_visible([ordinary], source)
    assert ordinary.rgba[0, 12, 0] < .9


def test_boundary_respects_occluder_and_no_restore():
    rgba = np.full((24, 24, 4), .25, np.float32)
    rgba[..., 3] = 1
    white = Layer('white', rgba, (0, 0), 1)
    occluder = np.zeros_like(rgba)
    occluder[8:16, 8:16, 3] = 1
    upper = Layer('iris', occluder, (0, 0), 1)
    nr = np.zeros((24, 24), np.float32)
    nr[20:, :] = 1
    white.meta['no_restore'] = nr
    restore_visible([white, upper], np.full((24, 24, 3), .9, np.float32),
                    tags={'white'}, boundary_tags=['white'])
    np.testing.assert_allclose(white.rgba[12, 12, :3], .25)
    np.testing.assert_allclose(white.rgba[22, 12, :3], .25)
    np.testing.assert_allclose(white.rgba[2, 12, :3], .9)
