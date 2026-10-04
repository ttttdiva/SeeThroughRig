import test from 'node:test';
import assert from 'node:assert/strict';
import { partMetrics } from '../../web/js/core/metrics.js';

function grid(n = 10, s = 10) {
  const pos = [], idx = [];
  for (let j = 0; j <= n; j++) for (let i = 0; i <= n; i++) pos.push(i * s, j * s);
  for (let j = 0; j < n; j++) for (let i = 0; i < n; i++) {
    const a = j * (n + 1) + i, b = a + 1, c = a + n + 2, d = a + n + 1;
    idx.push(a, b, c, a, c, d);
  }
  const P = Float32Array.from(pos);
  return { id: 'g', positions: P, indices: Uint32Array.from(idx), attrs: { head: new Float32Array(P.length / 2).fill(1) } };
}

test('rigid motion and uniform foreshortening are not distortion', () => {
  const d = grid();
  const out = new Float32Array(d.positions.length);
  const a = 0.3, sx = 0.8;
  for (let i = 0; i < out.length; i += 2) {
    const x = d.positions[i] * sx, y = d.positions[i + 1];
    out[i] = 50 + x * Math.cos(a) - y * Math.sin(a);
    out[i + 1] = 20 + x * Math.sin(a) + y * Math.cos(a);
  }
  const m = partMetrics(d, out);
  assert.ok(m.bendPx < 1e-3 && m.strainMax < 1e-3, JSON.stringify(m));
  assert.ok(m.aniso > 0.2);
});

test('bending is detected', () => {
  const d = grid();
  const out = Float32Array.from(d.positions);
  for (let i = 0; i < out.length; i += 2) out[i] += 8 * Math.sin(d.positions[i + 1] / 30);
  const m = partMetrics(d, out);
  assert.ok(m.bendPx > 3 && m.strainMax > 0.1, JSON.stringify(m));
});

test('folding is detected', () => {
  const d = grid();
  const out = Float32Array.from(d.positions);
  for (let i = 0; i < out.length; i += 2) if (d.positions[i] > 50) out[i] = 100 - d.positions[i] * 0.5;
  const m = partMetrics(d, out);
  assert.ok(m.strainMax >= 1, JSON.stringify(m));
});
