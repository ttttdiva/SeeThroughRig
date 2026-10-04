"""Grid meshes that cover the opaque part of a layer."""
from __future__ import annotations

import cv2
import numpy as np

from .layers import Layer


def grid_mesh(layer: Layer, spacing: float, alpha_thr: float = 0.004, dilate_px: int = 2):
    """Build a regular grid mesh over the layer content.

    ``spacing`` is in source px. Returns (positions[N,2] in source px, uv_px[N,2] in
    raster px of the layer, triangles[M,3]).
    """
    a = layer.alpha
    h, w = a.shape
    mask = (a > alpha_thr).astype(np.uint8)
    if dilate_px > 0:
        mask = cv2.dilate(mask, np.ones((2 * dilate_px + 1, 2 * dilate_px + 1), np.uint8))
    step = max(2.0, spacing * layer.density)
    nx = max(1, int(np.ceil(w / step)))
    ny = max(1, int(np.ceil(h / step)))
    xs = np.linspace(0, w, nx + 1)
    ys = np.linspace(0, h, ny + 1)
    # cell occupancy: any covered pixel inside the cell
    integral = cv2.integral(mask)
    keep = np.zeros((ny, nx), bool)
    for j in range(ny):
        y0, y1 = int(np.floor(ys[j])), int(np.ceil(ys[j + 1]))
        for i in range(nx):
            x0, x1 = int(np.floor(xs[i])), int(np.ceil(xs[i + 1]))
            s = integral[y1, x1] - integral[y0, x1] - integral[y1, x0] + integral[y0, x0]
            keep[j, i] = s > 0
    vid = -np.ones((ny + 1, nx + 1), np.int64)
    verts = []
    tris = []
    for j in range(ny):
        for i in range(nx):
            if not keep[j, i]:
                continue
            corners = [(j, i), (j, i + 1), (j + 1, i + 1), (j + 1, i)]
            ids = []
            for (jj, ii) in corners:
                if vid[jj, ii] < 0:
                    vid[jj, ii] = len(verts)
                    verts.append((xs[ii], ys[jj]))
                ids.append(vid[jj, ii])
            a0, a1, a2, a3 = ids
            # one diagonal direction everywhere: alternating diagonals turn a
            # sheared straight line into a visible zig-zag
            tris += [(a0, a1, a2), (a0, a2, a3)]
    uv = np.array(verts, np.float32).reshape(-1, 2)
    pos = np.empty_like(uv)
    pos[:, 0] = layer.origin[0] + uv[:, 0] / layer.density
    pos[:, 1] = layer.origin[1] + uv[:, 1] / layer.density
    return pos, uv, np.array(tris, np.uint32).reshape(-1, 3)
