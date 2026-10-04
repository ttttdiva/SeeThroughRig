// Distortion metrics for a deformed model (used by scripts/warp_check.py, scripts/rigid_check.py and tests).
//
// For every drawable only the vertices that fully follow the head (head
// weight >= minHead) are considered, so hair tips hanging onto the body and
// physics are measured separately. For those vertices we report:
//
//   bendPx      max distance between the deformed vertices and the best
//               *affine* map of the rest vertices  -> bending / warping
//   strain99    99th percentile of the local strain of each triangle relative
//               to that affine map (|log singular value|) -> local stretch
//   strainMax   the maximum of the same
//   aniso       log(s1/s2) of the part's affine map -> uniform foreshortening
//               (not a distortion, but reported)
//   shear       angle (deg) between the mapped x and y axes minus 90

function fitAffine(P, Q, idx) {
  // least squares Q ~ A P + t over vertex indices idx
  let n = idx.length, mx = 0, my = 0, ux = 0, uy = 0;
  for (const i of idx) { mx += P[2 * i]; my += P[2 * i + 1]; ux += Q[2 * i]; uy += Q[2 * i + 1]; }
  mx /= n; my /= n; ux /= n; uy /= n;
  let sxx = 0, sxy = 0, syy = 0, axx = 0, axy = 0, ayx = 0, ayy = 0;
  for (const i of idx) {
    const px = P[2 * i] - mx, py = P[2 * i + 1] - my, qx = Q[2 * i] - ux, qy = Q[2 * i + 1] - uy;
    sxx += px * px; sxy += px * py; syy += py * py;
    axx += qx * px; axy += qx * py; ayx += qy * px; ayy += qy * py;
  }
  const det = sxx * syy - sxy * sxy || 1e-9;
  const i00 = syy / det, i01 = -sxy / det, i11 = sxx / det;
  const a = axx * i00 + axy * i01, b = axx * i01 + axy * i11;
  const c = ayx * i00 + ayy * i01, d = ayx * i01 + ayy * i11;
  return { a, b, c, d, tx: ux - (a * mx + b * my), ty: uy - (c * mx + d * my) };
}

function sv2(a, b, c, d) {
  // singular values of [[a b][c d]]
  const s1 = a * a + b * b + c * c + d * d;
  const s2 = Math.sqrt(Math.max(0, (a * a + b * b - c * c - d * d) ** 2 + 4 * (a * c + b * d) ** 2));
  return [Math.sqrt((s1 + s2) / 2), Math.sqrt(Math.max(0, (s1 - s2) / 2))];
}

export function partMetrics(d, out, { minHead = 0.95, minArea = 4, yMin = -Infinity, cover = null } = {}) {
  const P = d.positions, Q = out, head = d.attrs.head;
  const nv = P.length / 2;
  const idx = [];
  for (let i = 0; i < nv; i++) if (head[i] >= minHead && P[2 * i + 1] >= yMin) idx.push(i);
  if (idx.length < 3) return null;
  const A = fitAffine(P, Q, idx);
  let bend = 0;
  for (const i of idx) {
    const ex = A.a * P[2 * i] + A.b * P[2 * i + 1] + A.tx - Q[2 * i];
    const ey = A.c * P[2 * i] + A.d * P[2 * i + 1] + A.ty - Q[2 * i + 1];
    bend = Math.max(bend, Math.hypot(ex, ey));
  }
  // inverse of the part affine
  const det = A.a * A.d - A.b * A.c || 1e-9;
  const ia = A.d / det, ib = -A.b / det, ic = -A.c / det, id = A.a / det;
  const T = d.indices;
  const strains = [];
  let worst = null;
  for (let t = 0; t < T.length; t += 3) {
    const i0 = T[t], i1 = T[t + 1], i2 = T[t + 2];
    if (head[i0] < minHead || head[i1] < minHead || head[i2] < minHead) continue;
    if (P[2 * i0 + 1] < yMin || P[2 * i1 + 1] < yMin || P[2 * i2 + 1] < yMin) continue;
    if (cover && cover((Q[2 * i0] + Q[2 * i1] + Q[2 * i2]) / 3, (Q[2 * i0 + 1] + Q[2 * i1 + 1] + Q[2 * i2 + 1]) / 3)) continue;
    const r1x = P[2 * i1] - P[2 * i0], r1y = P[2 * i1 + 1] - P[2 * i0 + 1];
    const r2x = P[2 * i2] - P[2 * i0], r2y = P[2 * i2 + 1] - P[2 * i0 + 1];
    const rd = r1x * r2y - r2x * r1y;
    if (Math.abs(rd) < minArea) continue;
    const q1x = Q[2 * i1] - Q[2 * i0], q1y = Q[2 * i1 + 1] - Q[2 * i0 + 1];
    const q2x = Q[2 * i2] - Q[2 * i0], q2y = Q[2 * i2 + 1] - Q[2 * i0 + 1];
    // F = [q1 q2] [r1 r2]^-1
    const f00 = (q1x * r2y - q2x * r1y) / rd, f01 = (-q1x * r2x + q2x * r1x) / rd;
    const f10 = (q1y * r2y - q2y * r1y) / rd, f11 = (-q1y * r2x + q2y * r1x) / rd;
    // G = F A^-1
    const g00 = f00 * ia + f01 * ic, g01 = f00 * ib + f01 * id;
    const g10 = f10 * ia + f11 * ic, g11 = f10 * ib + f11 * id;
    const [s1, s2] = sv2(g00, g01, g10, g11);
    const gd = g00 * g11 - g01 * g10;
    const st = gd <= 0 ? 10 : Math.max(Math.abs(Math.log(s1)), Math.abs(Math.log(Math.max(s2, 1e-6))));
    strains.push(st);
    if (!worst || st > worst.s) worst = { s: st, x: (P[2 * i0] + P[2 * i1] + P[2 * i2]) / 3, y: (P[2 * i0 + 1] + P[2 * i1 + 1] + P[2 * i2 + 1]) / 3 };
  }
  strains.sort((x, y) => x - y);
  const [a1, a2] = sv2(A.a, A.b, A.c, A.d);
  const shear = Math.abs(Math.acos(Math.max(-1, Math.min(1, (A.a * A.b + A.c * A.d) / (Math.hypot(A.a, A.c) * Math.hypot(A.b, A.d) || 1)))) * 180 / Math.PI - 90);
  return {
    id: d.id,
    n: idx.length,
    bendPx: bend,
    strain99: strains.length ? strains[Math.floor(0.99 * (strains.length - 1))] : 0,
    strainMax: strains.length ? strains[strains.length - 1] : 0,
    worstAt: worst ? [Math.round(worst.x), Math.round(worst.y)] : null,
    aniso: Math.log(a1 / Math.max(a2, 1e-6)),
    shear,
  };
}

export function modelMetrics(model, deformer, opts) {
  const res = [];
  for (const d of model.drawables) {
    const op = deformer.opacity.get(d.id) ?? 1;
    if (op < 0.02) continue;
    const m = partMetrics(d, deformer.out.get(d.id), opts);
    if (m) res.push(m);
  }
  return res;
}

// Fraction of a part's vertices that lie on the face (inside one of the face
// mesh's triangles, with the face's current positions).
function insideFraction(face, faceOut, part, partOut, useRest) {
  const T = face.indices;
  const F = useRest ? face.positions : faceOut;
  const Pp = useRest ? part.positions : partOut;
  // coarse grid of face triangles for speed
  const cell = 16;
  const grid = new Map();
  for (let t = 0; t < T.length; t += 3) {
    const xs = [F[2 * T[t]], F[2 * T[t + 1]], F[2 * T[t + 2]]];
    const ys = [F[2 * T[t] + 1], F[2 * T[t + 1] + 1], F[2 * T[t + 2] + 1]];
    for (let gx = Math.floor(Math.min(...xs) / cell); gx <= Math.floor(Math.max(...xs) / cell); gx++)
      for (let gy = Math.floor(Math.min(...ys) / cell); gy <= Math.floor(Math.max(...ys) / cell); gy++) {
        const k = gx * 100003 + gy;
        if (!grid.has(k)) grid.set(k, []);
        grid.get(k).push(t);
      }
  }
  let inside = 0;
  const n = Pp.length / 2;
  for (let i = 0; i < n; i++) {
    const x = Pp[2 * i], y = Pp[2 * i + 1];
    const list = grid.get(Math.floor(x / cell) * 100003 + Math.floor(y / cell)) || [];
    for (const t of list) {
      const ax = F[2 * T[t]], ay = F[2 * T[t] + 1], bx = F[2 * T[t + 1]], by = F[2 * T[t + 1] + 1];
      const cx = F[2 * T[t + 2]], cy = F[2 * T[t + 2] + 1];
      const d1 = (x - bx) * (ay - by) - (ax - bx) * (y - by);
      const d2 = (x - cx) * (by - cy) - (bx - cx) * (y - cy);
      const d3 = (x - ax) * (cy - ay) - (cx - ax) * (y - ay);
      const neg = d1 < 0 || d2 < 0 || d3 < 0, pos = d1 > 0 || d2 > 0 || d3 > 0;
      if (!(neg && pos)) { inside++; break; }
    }
  }
  return inside / Math.max(1, n);
}

// How much each facial part stays on the face compared with the rest pose.
export function attachment(model, deformer, parts = ['ears-l', 'ears-r', 'eyewhite-l', 'eyewhite-r', 'eyebrow-l', 'eyebrow-r', 'mouth', 'nose', 'neck']) {
  const face = model.drawableById['face'];
  if (!face) return [];
  const fo = deformer.out.get('face');
  const res = [];
  for (const id of parts) {
    const d = model.drawableById[id];
    if (!d) continue;
    const r0 = insideFraction(face, fo, d, null, true);
    const r1 = insideFraction(face, fo, d, deformer.out.get(id), false);
    res.push({ id, rest: r0, pose: r1, drop: r0 - r1 });
  }
  return res;
}

export function meshCover(d, out) {
  const T = d.indices, F = out;
  const cell = 16, grid = new Map();
  for (let t = 0; t < T.length; t += 3) {
    const xs = [F[2 * T[t]], F[2 * T[t + 1]], F[2 * T[t + 2]]], ys = [F[2 * T[t] + 1], F[2 * T[t + 1] + 1], F[2 * T[t + 2] + 1]];
    for (let gx = Math.floor(Math.min(...xs) / cell); gx <= Math.floor(Math.max(...xs) / cell); gx++)
      for (let gy = Math.floor(Math.min(...ys) / cell); gy <= Math.floor(Math.max(...ys) / cell); gy++) {
        const k = gx * 100003 + gy; if (!grid.has(k)) grid.set(k, []); grid.get(k).push(t);
      }
  }
  return (x, y) => {
    for (const t of grid.get(Math.floor(x / cell) * 100003 + Math.floor(y / cell)) || []) {
      const ax = F[2 * T[t]], ay = F[2 * T[t] + 1], bx = F[2 * T[t + 1]], by = F[2 * T[t + 1] + 1], cx = F[2 * T[t + 2]], cy = F[2 * T[t + 2] + 1];
      const d1 = (x - bx) * (ay - by) - (ax - bx) * (y - by), d2 = (x - cx) * (by - cy) - (bx - cx) * (y - cy), d3 = (x - ax) * (cy - ay) - (cx - ax) * (y - ay);
      if (!((d1 < 0 || d2 < 0 || d3 < 0) && (d1 > 0 || d2 > 0 || d3 > 0))) return true;
    }
    return false;
  };
}

// Absolute per-triangle distortion (no part-level fit): |log singular value| of
// each triangle's deformation gradient. Rotation (incl. varying rotation along
// a bending strand) gives 0; shear / stretch / squash give > 0. This is the
// ARAP energy measure (Igarashi et al. 2005) and catches linear-blend shear.
export function triStrain(d, out, { minArea = 4, cover = null, scale = 1 } = {}) {
  const P = d.positions, Q = out, T = d.indices;
  const vals = [];
  let worst = null;
  for (let t = 0; t < T.length; t += 3) {
    const i0 = T[t], i1 = T[t + 1], i2 = T[t + 2];
    const r1x = P[2 * i1] - P[2 * i0], r1y = P[2 * i1 + 1] - P[2 * i0 + 1];
    const r2x = P[2 * i2] - P[2 * i0], r2y = P[2 * i2 + 1] - P[2 * i0 + 1];
    const rd = r1x * r2y - r2x * r1y;
    if (Math.abs(rd) < minArea) continue;
    const cx = (Q[2 * i0] + Q[2 * i1] + Q[2 * i2]) / 3, cy = (Q[2 * i0 + 1] + Q[2 * i1 + 1] + Q[2 * i2 + 1]) / 3;
    if (cover && cover(Q[2 * i0], Q[2 * i0 + 1]) && cover(Q[2 * i1], Q[2 * i1 + 1]) && cover(Q[2 * i2], Q[2 * i2 + 1])) continue;
    const q1x = Q[2 * i1] - Q[2 * i0], q1y = Q[2 * i1 + 1] - Q[2 * i0 + 1];
    const q2x = Q[2 * i2] - Q[2 * i0], q2y = Q[2 * i2 + 1] - Q[2 * i0 + 1];
    const f00 = (q1x * r2y - q2x * r1y) / rd, f01 = (-q1x * r2x + q2x * r1x) / rd;
    const f10 = (q1y * r2y - q2y * r1y) / rd, f11 = (-q1y * r2x + q2y * r1x) / rd;
    // a uniform scale of the whole figure (closer / further) is not distortion
    const [s1, s2] = sv2(f00 / scale, f01 / scale, f10 / scale, f11 / scale);
    const st = f00 * f11 - f01 * f10 <= 0 ? 10 : Math.max(Math.abs(Math.log(s1)), Math.abs(Math.log(Math.max(s2, 1e-6))));
    vals.push(st);
    if (!worst || st > worst.s) worst = { s: st, x: Math.round(cx), y: Math.round(cy) };
  }
  vals.sort((a, b) => a - b);
  return { id: d.id, max: vals.length ? vals[vals.length - 1] : 0, p99: vals.length ? vals[Math.floor(0.99 * (vals.length - 1))] : 0, at: worst ? [worst.x, worst.y] : null };
}

// Map a rest-space point through a drawable's deformation (barycentric in the
// rest triangle that contains it). Returns null outside the mesh.
export function meshMap(d, out) {
  const T = d.indices, P = d.positions;
  const cell = 16, grid = new Map();
  for (let t = 0; t < T.length; t += 3) {
    const xs = [P[2 * T[t]], P[2 * T[t + 1]], P[2 * T[t + 2]]], ys = [P[2 * T[t] + 1], P[2 * T[t + 1] + 1], P[2 * T[t + 2] + 1]];
    for (let gx = Math.floor(Math.min(...xs) / cell); gx <= Math.floor(Math.max(...xs) / cell); gx++)
      for (let gy = Math.floor(Math.min(...ys) / cell); gy <= Math.floor(Math.max(...ys) / cell); gy++) {
        const k = gx * 100003 + gy; if (!grid.has(k)) grid.set(k, []); grid.get(k).push(t);
      }
  }
  return (x, y) => {
    for (const t of grid.get(Math.floor(x / cell) * 100003 + Math.floor(y / cell)) || []) {
      const i0 = T[t], i1 = T[t + 1], i2 = T[t + 2];
      const ax = P[2 * i0], ay = P[2 * i0 + 1], bx = P[2 * i1], by = P[2 * i1 + 1], cx = P[2 * i2], cy = P[2 * i2 + 1];
      const den = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy);
      if (Math.abs(den) < 1e-9) continue;
      const u = ((by - cy) * (x - cx) + (cx - bx) * (y - cy)) / den;
      const v = ((cy - ay) * (x - cx) + (ax - cx) * (y - cy)) / den;
      const w = 1 - u - v;
      if (u < -1e-6 || v < -1e-6 || w < -1e-6) continue;
      return [u * out[2 * i0] + v * out[2 * i1] + w * out[2 * i2], u * out[2 * i0 + 1] + v * out[2 * i1 + 1] + w * out[2 * i2 + 1]];
    }
    return null;
  };
}

// Hair cut into bundles ("tag#k") and the parent they were cut from overlap a
// little along every cut. Where two of them overlap in the rest pose, both
// must carry the overlapping point to (nearly) the same place, otherwise a
// crack opens along the cut. The allowed separation is 3/4 of the overlap the
// two bundles were cut with. Returns the worst separation / allowed ratio.
export function seamGap(model, deformer, { hidden = null } = {}) {
  const groups = new Map();
  for (const d of model.drawables) {
    const base = d.id.split('#')[0];
    if (!model.drawables.some((e) => e.id.startsWith(base + '#'))) continue;
    if (!groups.has(base)) groups.set(base, []);
    groups.get(base).push(d);
  }
  let worst = { gap: 0, ratio: 0, a: null, b: null, at: null };
  for (const ds of groups.values()) {
    const maps = new Map(ds.map((d) => [d.id, meshMap(d, deformer.out.get(d.id))]));
    for (const a of ds) {
      const P = a.positions, Q = deformer.out.get(a.id);
      for (const b of ds) {
        if (a === b) continue;
        const mb = maps.get(b.id);
        const allowed = 0.75 * Math.min(a.overlap ?? 6, b.overlap ?? 6);
        for (let i = 0; i < P.length / 2; i += 1) {
          const q = mb(P[2 * i], P[2 * i + 1]);
          if (!q) continue;
          const g = Math.hypot(q[0] - Q[2 * i], q[1] - Q[2 * i + 1]);
          // a crack behind opaque parts drawn in front of both bundles is never seen
          if (hidden && g / allowed > worst.ratio && hidden(Q[2 * i], Q[2 * i + 1], Math.max(a.order, b.order))
              && hidden(q[0], q[1], Math.max(a.order, b.order))) continue;
          if (g / allowed > worst.ratio) worst = { gap: g, ratio: g / allowed, a: a.id, b: b.id, at: [Math.round(P[2 * i]), Math.round(P[2 * i + 1])] };
        }
      }
    }
  }
  return worst;
}
