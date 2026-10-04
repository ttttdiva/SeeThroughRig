// node --test tests/js
import test from 'node:test';
import assert from 'node:assert/strict';
import { Transformer, evalKeys, evalOpacity } from '../../web/js/core/deform.js';
import { Physics } from '../../web/js/core/physics.js';
import { sampleTrack, AutoBlink, Smooth } from '../../web/js/core/motion.js';

const rig = {
  head: { center: [100, 100], pivot: [100, 180], yawScale: 1, pitchScale: 1, rollScale: 1, focal: 2000, shift: [0, 0, 0] },
  body: { axisX: 100, pivot: [100, 400], twistScale: 1, pitchScale: 1, leanScale: 1, breathLift: 3, breathWiden: 0, shift: 0 },
};

test('rest pose is identity', () => {
  const tf = new Transformer(rig);
  tf.prepare({});
  const out = [0, 0];
  tf.head(130, 90, 40, 1, out);
  assert.ok(Math.abs(out[0] - 130) < 1e-9 && Math.abs(out[1] - 90) < 1e-9);
  tf.body(130, 90, 20, 1, 1, 1, out);
  assert.ok(Math.abs(out[0] - 130) < 1e-9 && Math.abs(out[1] - 90) < 1e-9);
});

test('evalKeys interpolates and clamps', () => {
  const k = [[0, 0], [1, 10]];
  assert.equal(evalKeys(k, -1), 0);
  assert.equal(evalKeys(k, 0.5), 5);
  assert.equal(evalKeys(k, 3), 10);
});

test('evalOpacity terms multiply', () => {
  const spec = [{ param: 'A', keys: [[0, 0], [1, 1]] }, { oneMinusSum: ['B'], lo: 0, hi: 1 }];
  assert.equal(evalOpacity(spec, { A: 1, B: 0 }), 1);
  assert.equal(evalOpacity(spec, { A: 1, B: 1 }), 0);
  assert.equal(evalOpacity(spec, { A: 0, B: 0 }), 0);
});

function makeChain(extra = {}) {
  const n = 7;
  const points = Array.from({ length: n }, (_, i) => [0, i * 80]);
  return { chains: [{ id: 'c', points, attrs: {}, stiffness: 80, tipStiffness: 26, damping: 4.6, follow: 0.9, inertia: 0.9, wind: 0, ...extra }] };
}

test('chain lags behind a moving root and settles back (no blow-up)', () => {
  let rootX = 0;
  const tf = { prepare() {}, point(x, y, a, i, out) { out[0] = x + rootX; out[1] = y; } };
  const ph = new Physics(makeChain(), tf);
  let maxOff = 0, lagSeen = false;
  for (let f = 0; f < 360; f++) {
    const t = f / 60;
    rootX = t < 0.5 ? 40 * Math.sin((t * Math.PI) / 1.0) : 40;
    ph.update({ PhysicsGain: 1, Wind: 0 }, 1 / 60);
    const c = ph.chains.get('c');
    const tipOff = Math.hypot(c.off[12], c.off[13]);
    maxOff = Math.max(maxOff, tipOff);
    if (t > 0.2 && t < 0.5 && c.off[12] < -5) lagSeen = true;
  }
  const c = ph.chains.get('c');
  assert.ok(lagSeen, 'tip lags behind the root');
  assert.ok(maxOff < 60, `offset stays bounded (${maxOff.toFixed(1)})`);
  assert.ok(Math.hypot(c.off[12], c.off[13]) < 1, 'settles back to the drawn shape');
});

test('chain keeps segment lengths', () => {
  let rootX = 0;
  const tf = { prepare() {}, point(x, y, a, i, out) { out[0] = x + rootX; out[1] = y; } };
  const ph = new Physics(makeChain(), tf);
  for (let f = 0; f < 120; f++) {
    rootX = 60 * Math.sin(f / 5);
    ph.update({ PhysicsGain: 1, Wind: 0 }, 1 / 60);
  }
  const { pos } = ph.chains.get('c');
  for (let i = 1; i < 7; i++) {
    const L = Math.hypot(pos[2 * i] - pos[2 * i - 2], pos[2 * i + 1] - pos[2 * i - 1]);
    assert.ok(Math.abs(L - 80) < 0.5, `segment ${i} length ${L}`);
  }
});

test('timeline tracks ease between keys', () => {
  const keys = [[0, 0], [1, 10], [2, 10]];
  assert.equal(sampleTrack(keys, -1), 0);
  assert.ok(Math.abs(sampleTrack(keys, 0.5) - 5) < 1e-9);
  assert.equal(sampleTrack(keys, 5), 10);
});

test('auto blink closes and reopens', () => {
  const b = new AutoBlink(1);
  let minV = 1;
  for (let t = 0; t < 8; t += 1 / 120) minV = Math.min(minV, b.update(t));
  assert.equal(minV, 0);
  assert.equal(b.update(100), 1);
});

test('smooth converges to target', () => {
  const s = new Smooth(0, 0.1);
  for (let i = 0; i < 120; i++) s.update(1, 1 / 60);
  assert.ok(Math.abs(s.v - 1) < 1e-3);
});

test('rigid head mode: the head moves as one plate (distances preserved), yaw is ignored', () => {
  const r = { ...rig, head: { ...rig.head, mode: 'rigid', rollScale: 0.5, bob: 4, rigidPivot: [100, 200] } };
  const tf = new Transformer(r);
  tf.prepare({ AngleX: 30, AngleY: 20, AngleZ: 25 });
  const pts = [[60, 80, 40], [140, 90, -20], [100, 150, 10]].map(([x, y, z]) => { const o = [0, 0]; tf.head(x, y, z, 1, o); return o; });
  const d = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);
  assert.ok(Math.abs(d(pts[0], pts[1]) - Math.hypot(80, 10)) < 1e-9);
  assert.ok(Math.abs(d(pts[1], pts[2]) - Math.hypot(40, 60)) < 1e-9);
  const tf2 = new Transformer(r);
  tf2.prepare({ AngleX: 30 });
  const o = [0, 0];
  tf2.head(60, 80, 40, 1, o);
  assert.ok(Math.abs(o[0] - 60) < 1e-9 && Math.abs(o[1] - 80) < 1e-9);
});

test('strands bend without shear: each row keeps its width, root follows the head', async () => {
  const { strandTargets, evalStrand, strandPoint } = await import('../../web/js/core/deform.js');
  const r = { ...rig, head: { ...rig.head, mode: 'rigid', rollScale: 0.5, rigidPivot: [100, 200] }, body: { ...rig.body, rigid: true } };
  const tf = new Transformer(r);
  tf.prepare({ AngleZ: 30 });
  const sd = { pts: Array.from({ length: 12 }, (_, k) => [100, 150 + k * 30]), root: { zb: 0, twist: 0, lean: 0, breath: 0 }, droop: 0.6 };
  const rt = evalStrand(sd, tf, strandTargets(sd, tf));
  const a = [0, 0], b = [0, 0];
  for (const s of [0.2, 0.5, 0.9]) {
    const y = 150 + s * 330;
    strandPoint(sd, rt, 60, y, s, a);
    strandPoint(sd, rt, 140, y, s, b);
    assert.ok(Math.abs(Math.hypot(a[0] - b[0], a[1] - b[1]) - 80) < 1e-6, 'row width preserved');
  }
  const o = [0, 0];
  tf.head(100, 150, 0, 1, o);
  assert.ok(Math.abs(rt.x[0] - o[0]) < 1e-9 && Math.abs(rt.y[0] - o[1]) < 1e-9, 'root moves with the head');
});
