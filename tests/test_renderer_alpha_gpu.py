"""Optional real Mesa/EGL shader regression; install moderngl to run on Linux.

Uses the canonical GLSL and copy operation, not a numerical stand-in. Synthetic
pixels test filtering/alpha correctness; actual Native visual QA is separate.
"""
import ctypes
import re
from pathlib import Path

import numpy as np
import pytest

moderngl = pytest.importorskip("moderngl")
SOURCE = (Path(__file__).resolve().parents[1] / "web/js/core/renderer.js").read_text()


def shader(name):
    return re.search(r"const " + name + r" = `(.*?)`;", SOURCE, re.S)[1]


@pytest.fixture
def gl():
    try:
        ctx = moderngl.create_standalone_context(backend="egl")
        native = ctypes.CDLL("libGL.so.1")
    except Exception as error:
        pytest.skip(f"Standalone EGL unavailable: {error}")
    yield ctx, native
    ctx.release()


def normalize(ctx, native, pixels):
    h, w = pixels.shape[:2]
    source = ctx.texture((w, h), 4, pixels.tobytes())
    source.filter = (moderngl.NEAREST, moderngl.NEAREST)
    target = ctx.texture((w, h), 4)
    framebuffer = ctx.framebuffer(color_attachments=[target])
    program = ctx.program(vertex_shader=shader("PREMULTIPLY_VS"), fragment_shader=shader("PREMULTIPLY_FS"))
    vao = ctx.vertex_array(program, [])
    framebuffer.use()
    ctx.disable(moderngl.BLEND | moderngl.DEPTH_TEST | moderngl.CULL_FACE)
    source.use(0)
    program["u_tex"].value = 0
    vao.render(vertices=3)
    # This is the renderer's exact copy-back operation, after the draw finishes
    # in GL command order, with source bound and the separate target attached.
    native.glCopyTexSubImage2D(0x0DE1, 0, 0, 0, 0, 0, w, h)
    assert native.glGetError() == 0
    vao.release(); program.release(); framebuffer.release(); target.release()
    return source


def test_normalization_preserves_orientation_and_multiplies_every_texel_once(gl):
    ctx, native = gl
    rng = np.random.default_rng(42)
    pixels = rng.integers(0, 256, (32, 64, 4), dtype=np.uint8)
    pixels[0, :4] = [[255, 30, 90, 0], [200, 100, 50, 128], [3, 2, 1, 255], [20, 240, 60, 64]]
    texture = normalize(ctx, native, pixels)
    actual = np.frombuffer(texture.read(), np.uint8).reshape(pixels.shape)
    expected = pixels.copy()
    expected[..., :3] = np.rint(pixels[..., :3].astype(np.float32) * pixels[..., 3:4] / 255).astype(np.uint8)
    assert np.abs(actual.astype(int) - expected.astype(int)).max() <= 1
    np.testing.assert_array_equal(actual[..., 3], pixels[..., 3])
    assert not actual[pixels[..., 3] == 0].any()
    texture.release()


def sample(ctx, texture, size=(23, 11), post_multiply=False):
    texture.build_mipmaps()
    texture.filter = (moderngl.LINEAR_MIPMAP_LINEAR, moderngl.LINEAR)
    texture.repeat_x = texture.repeat_y = False
    program = ctx.program(vertex_shader=shader("PREMULTIPLY_VS"), fragment_shader="""#version 300 es
precision highp float;
uniform sampler2D u_tex;
uniform vec2 u_size;
uniform bool u_post;
out vec4 o;
void main() { vec4 c=texture(u_tex,gl_FragCoord.xy/u_size); if(u_post)c.rgb*=c.a; o=c; }
""")
    program["u_tex"].value = 0; program["u_size"].value = size; program["u_post"].value = post_multiply
    framebuffer = ctx.simple_framebuffer(size, components=4)
    framebuffer.use(); texture.use(0)
    vao = ctx.vertex_array(program, []); vao.render(vertices=3)
    result = np.frombuffer(framebuffer.read(components=4), np.uint8).reshape(size[1], size[0], 4).copy()
    vao.release(); program.release(); framebuffer.release()
    return result


def test_minified_transparent_black_edge_matches_preupload_reference(gl):
    ctx, native = gl
    pixels = np.zeros((32, 64, 4), np.uint8)
    pixels[5:27, 7:57] = [160, 210, 90, 255]
    pixels[4, 7:57] = [160, 210, 90, 128]
    normalized = normalize(ctx, native, pixels)
    expected = pixels.copy()
    expected[..., :3] = np.rint(pixels[..., :3].astype(np.float32) * pixels[..., 3:4] / 255).astype(np.uint8)
    reference = ctx.texture((64, 32), 4, expected.tobytes())
    straight = ctx.texture((64, 32), 4, pixels.tobytes())
    correct, oracle, old = sample(ctx, normalized), sample(ctx, reference), sample(ctx, straight, post_multiply=True)
    assert np.abs(correct.astype(int) - oracle.astype(int)).max() <= 1
    assert (oracle[..., :3].astype(int) - old[..., :3].astype(int)).max() >= 20
    for texture in [normalized, reference, straight]: texture.release()
