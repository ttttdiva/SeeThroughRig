// Player: plays a motion (or idles) and exposes a deterministic API for
// frame-by-frame recording and pose inspection (window.__rig).
//
//   player.html?model=models/aoi&motion=motions/demo.json&view=bust
//   player.html?...&record=1   -> no realtime loop; the recorder drives frames

import { RigRuntime as Puppet } from './runtime/index.js';
import { loadStandaloneSettings } from './standalone-settings.js';
import { Timeline } from './core/motion.js';

const qs = new URLSearchParams(location.search);
const modelDir = qs.get('model') || 'models/aoi';
const record = qs.has('record');
const canvas = document.getElementById('gl');
const cap = document.getElementById('cap');

const puppet = await Puppet.create(canvas, `${modelDir}/model.json`, {
  coreRef: qs.get('coreRef') || 'canonical',
  settings: await loadStandaloneSettings(`${modelDir}/settings.json`),
  preserveDrawingBuffer: true,
  seed: +(qs.get('seed') || 3),
});
const { renderer, animator, model } = puppet;
const views = model.views || {};
const setView = (k) => views[k] && Object.assign(renderer.camera, views[k]);
setView(qs.get('view') || 'bust');
// ?wind=0.7 : wind blowing (0..1)
if (qs.get('wind')) animator.manual.WindBlow = +qs.get('wind');
renderer.background = (qs.get('bg') || '0.933,0.941,0.957,1').split(',').map(Number);

let timeline = null;
if (qs.get('motion')) {
  const data = await (await fetch(qs.get('motion'), { cache: 'no-cache' })).json();
  timeline = new Timeline(data);
  animator.playTimeline(timeline, 0);
}

// captions / camera cues from the motion file
function applyCues(t) {
  const data = timeline?.data;
  if (!data) return;
  let c = null;
  for (const e of data.captions || []) if (t >= e[0] && t < e[1]) c = e;
  if (c) {
    cap.style.display = 'block';
    cap.innerHTML = `${c[2]}${c[3] ? `<small>${c[3]}</small>` : ''}`;
  } else cap.style.display = 'none';
  if (data.camera) {
    // camera keys: [t, x, y, zoom]
    const keys = data.camera;
    let k0 = keys[0], k1 = keys[0];
    for (let i = 0; i < keys.length; i++) {
      if (keys[i][0] <= t) k0 = keys[i];
      if (keys[i][0] >= t) { k1 = keys[i]; break; }
      k1 = keys[i];
    }
    const u = k1[0] > k0[0] ? (t - k0[0]) / (k1[0] - k0[0]) : 0;
    const s = u * u * (3 - 2 * u);
    renderer.camera.x = k0[1] + (k1[1] - k0[1]) * s;
    renderer.camera.y = k0[2] + (k1[2] - k0[2]) * s;
    renderer.camera.zoom = k0[3] + (k1[3] - k0[3]) * s;
  }
}

window.__rig = {
  puppet,
  duration: timeline?.data.duration ?? 10,
  // advance the simulation to absolute time t (seconds) with fixed substeps
  frame(t, fps = 60) {
    const dt = 1 / fps;
    while (puppet.time + dt * 0.5 < t) {
      applyCues(puppet.time + dt);
      puppet.step(dt);
    }
    return true;
  },
  // render a static pose: {AngleX: 20, ...}; settles physics first
  // then/thenSteps: switch to another pose and advance a few frames (to see physics lag)
  pose(values, { settle = 1.5, auto = false, hidden = [], then = null, thenSteps = 6, wire = false } = {}) {
    puppet.renderOptions.wireframe = wire;
    Object.assign(animator.options, { idle: auto, breath: auto, blink: auto, saccade: auto, lookAt: false });
    animator.manual = { ...values };
    puppet.renderOptions.hidden = new Set(hidden);
    puppet.physics.reset();
    for (let i = 0; i < settle * 60; i++) puppet.step(1 / 60);
    if (then) {
      animator.manual = { ...then };
      for (let i = 0; i < thenSteps; i++) puppet.step(1 / 60);
    }
    return canvas.toDataURL('image/png');
  },
  setView,
  snapshot(type = 'image/jpeg', q = 0.95) {
    return canvas.toDataURL(type, q);
  },
};

if (!record) {
  let last = performance.now();
  const loop = (now) => {
    const dt = Math.max(0, Math.min(0.05, (now - last) / 1000));
    last = now;
    applyCues(puppet.time + dt);
    puppet.step(dt);
    requestAnimationFrame(loop);
  };
  requestAnimationFrame(loop);
}
window.__rigReady = true;
