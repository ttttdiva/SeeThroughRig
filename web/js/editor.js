import { RigRuntime as Puppet, createPlaybackSettingsPanel } from './runtime/index.js';
import { loadStandaloneSettings } from './standalone-settings.js';
import { Timeline } from './core/motion.js';

const qs = new URLSearchParams(location.search);
const modelDir = qs.get('model') || 'models/aoi';
const $ = (id) => document.getElementById(id);

const canvas = $('gl');
const overlay = $('overlay');
const status = (msg) => ($('status').textContent = msg);

const loadedSettings = await loadStandaloneSettings(`${modelDir}/settings.json`);
const puppet = await Puppet.create(canvas, `${modelDir}/model.json`, { coreRef: qs.get('coreRef') || 'canonical', settings: loadedSettings });
window.puppet = puppet;
const { model, animator, params, renderer } = puppet;

// ------------------------------------------------------------------ views
const views = model.views || {};
for (const [k, v] of Object.entries(views)) $('view').add(new Option(`表示: ${k}`, k));
function setView(k) {
  const v = views[k];
  if (v) Object.assign(renderer.camera, v);
}
$('view').onchange = (e) => setView(e.target.value);
setView(views.bust ? 'bust' : Object.keys(views)[0]);
$('view').value = views.bust ? 'bust' : Object.keys(views)[0];
$('bg').onchange = (e) => (renderer.background = e.target.value.split(',').map(Number));
renderer.background = $('bg').value.split(',').map(Number);
$('wire').onchange = (e) => (puppet.renderOptions.wireframe = e.target.checked);

// ------------------------------------------------------------------ helpers
function slider(parent, label, min, max, step, value, oninput, fmt = (v) => (+v).toFixed(2)) {
  const row = document.createElement('div');
  row.className = 'row';
  const l = document.createElement('label');
  l.textContent = label;
  l.title = label;
  const r = document.createElement('input');
  Object.assign(r, { type: 'range', min, max, step, value });
  const o = document.createElement('output');
  o.textContent = fmt(value);
  r.oninput = () => {
    o.textContent = fmt(r.value);
    oninput(+r.value);
  };
  row.append(l, r, o);
  parent.append(row);
  return { row, input: r, out: o, set(v) { r.value = v; o.textContent = fmt(v); } };
}

// This exact metadata-driven panel is also embedded by AoiTalk Web. Live values
// are saved with this model, never a global browser preference.
const playbackPanels = [];
function syncPlaybackPanels() {
  const settings = puppet.getPlaybackSettings();
  for (const panel of playbackPanels) panel.update({ settings });
  for (const [id, [group, field]] of Object.entries(playbackParameters)) paramUI[id]?.set(settings[group][field]);
}
for (const [id, groups, label] of [
  ['playbackSettingsLeft', ['wind', 'blink', 'breath'], '風・瞬き・呼吸の再生設定'],
  ['playbackSettingsRight', ['idle', 'gaze', 'physics'], '待機・視線・物理の再生設定'],
]) {
  playbackPanels.push(createPlaybackSettingsPanel($(id), {
    settings: puppet.getPlaybackSettings(), capabilities: puppet.capabilities, groups, label,
    onChange: (settings) => {
      puppet.setPlaybackSettings(settings);
      syncPlaybackPanels();
    },
  }));
}

// ------------------------------------------------------------------ params
const paramUI = {};
const playbackParameters = { Wind: ['wind', 'wind'], WindBlow: ['wind', 'windBlow'], PhysicsGain: ['physics', 'gain'] };
let lastGroup = null;
for (const d of params.defs) {
  if (d.group !== lastGroup) {
    const h = document.createElement('h3');
    h.textContent = { head: '頭', body: '体', eye: '目', brow: '眉', mouth: '口', expr: '表情パーツ', physics: '物理' }[d.group] || d.group;
    $('params').append(h);
    lastGroup = d.group;
  }
  const step = d.id === 'MouthVowel' ? 1 : d.max - d.min > 5 ? 0.5 : 0.01;
  const playbackPath = playbackParameters[d.id];
  const initialValue = playbackPath ? puppet.getPlaybackSettings()[playbackPath[0]][playbackPath[1]] : d.default;
  paramUI[d.id] = slider($('params'), d.label, d.min, d.max, step, initialValue, (v) => {
    const path = playbackParameters[d.id];
    if (path && puppet.capabilities.playbackSettings?.includes(path.join('.'))) {
      puppet.setPlaybackSettings({ [path[0]]: { [path[1]]: v } });
      syncPlaybackPanels();
    } else animator.manual[d.id] = v;
  });
  paramUI[d.id].row.title = d.id;
}
$('resetParams').onclick = () => {
  const settings = puppet.getPlaybackSettings();
  animator.manual = Object.fromEntries(Object.entries(playbackParameters)
    .filter(([id]) => params.byId[id]).map(([id, [group, field]]) => [id, settings[group][field]]));
  for (const d of params.defs) paramUI[d.id].set(animator.manual[d.id] ?? d.default);
};

// ------------------------------------------------------------------ preview controls
$('talk').onclick = () => animator.lip.startFake(Math.floor(Math.random() * 1000), 4);
$('mic').onclick = async () => {
  try {
    await animator.lip.useMic();
    $('mic').classList.add('active');
  } catch (e) {
    status('マイクを使えません: ' + e.message);
  }
};
$('demo').onclick = async () => {
  const data = await (await fetch('motions/demo.json', { cache: 'no-cache' })).json();
  animator.playTimeline(new Timeline(data), puppet.time);
};

// ------------------------------------------------------------------ expressions
for (const ex of model.expressions || []) {
  const b = document.createElement('button');
  b.textContent = ex.label;
  b.onclick = () => {
    puppet.setExpression(ex.id === 'neutral' ? null : ex.id);
    for (const x of $('exprs').children) x.classList.toggle('active', x === b);
  };
  $('exprs').append(b);
}

// ------------------------------------------------------------------ rig tuning
const rig = model.rig;
const rigUI = [
  ['head', 'yawScale', '顔 左右の回転量', 0, 1.5, 0.01],
  ['head', 'pitchScale', '顔 上下の回転量', 0, 1.5, 0.01],
  ['head', 'rollScale', '顔 傾きの回転量', 0, 1.5, 0.01],
  ['head', 'depthScale', '顔の奥行き', 0, 2, 0.01],
  ['head', 'focal', 'パース（焦点距離）', 300, 6000, 10],
  ['body', 'twistScale', '体 ひねり量', 0, 4, 0.01],
  ['body', 'pitchScale', '体 前後量', 0, 3, 0.01],
  ['body', 'leanScale', '体 傾き量', 0, 3, 0.01],
  ['body', 'breathLift', '呼吸 上下(px)', 0, 10, 0.1],
  ['body', 'breathWiden', '呼吸 広がり', 0, 0.03, 0.001],
];
rig.head.depthScale ??= 1;
for (const [grp, key, label, min, max, step] of rigUI) {
  slider($('rig'), label, min, max, step, rig[grp][key], (v) => (rig[grp][key] = v), (v) => (+v).toFixed(step < 0.01 ? 3 : 2));
}
for (const side of Object.keys(rig.eyes || {})) {
  const e = rig.eyes[side];
  slider($('rig'), `目玉${side} 可動X`, 0, 30, 0.5, e.irisRange[0], (v) => (e.irisRange[0] = v));
  slider($('rig'), `目玉${side} 可動Y`, 0, 20, 0.5, e.irisRange[1], (v) => (e.irisRange[1] = v));
}

const chainKeys = [
  ['stiffness', '剛性(根元)', 5, 400, 1],
  ['tipStiffness', '剛性(先端)', 1, 300, 1],
  ['damping', '減衰', 0, 20, 0.1],
  ['follow', '追従(伝播)', 0, 1, 0.01],
  ['inertia', '慣性', 0.2, 3, 0.01],
  ['wind', '風の強さ', 0, 120, 1],
  ['maxOffset', '最大変位(px)', 5, 400, 1],
];
for (const c of model.chains) $('chainSel').add(new Option(c.id, c.id));
function showChain(id) {
  const c = model.chains.find((x) => x.id === id);
  $('chain').innerHTML = '';
  c.maxOffset ??= 200;
  for (const [k, label, min, max, step] of chainKeys) {
    c[k] ??= { tipStiffness: c.stiffness * 0.35, follow: 0.9, inertia: 1, wind: 30 }[k] ?? 0;
    slider($('chain'), label, min, max, step, c[k], (v) => (c[k] = v));
  }
}
$('chainSel').onchange = (e) => showChain(e.target.value);
if (model.chains.length) showChain(model.chains[0].id);

// ------------------------------------------------------------------ layers
for (const d of [...model.drawables].reverse()) {
  const row = document.createElement('div');
  row.className = 'layer';
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.checked = true;
  cb.onchange = () => (cb.checked ? puppet.renderOptions.hidden.delete(d.id) : puppet.renderOptions.hidden.add(d.id));
  const name = document.createElement('span');
  name.textContent = d.id;
  const n = document.createElement('small');
  n.textContent = d.vertexCount;
  row.append(cb, name, n);
  row.onmouseenter = () => (puppet.renderOptions.highlight = d.id);
  row.onmouseleave = () => (puppet.renderOptions.highlight = null);
  $('layers').append(row);
}

// ------------------------------------------------------------------ save / export
function collectSettings() {
  const pick = (o, keys) => Object.fromEntries(keys.filter((k) => o[k] !== undefined).map((k) => [k, o[k]]));
  return {
    ...structuredClone(loadedSettings),
    ...(puppet.capabilities.coreRef === 'canonical' && puppet.profile.id !== 'comparison-v1' ? { playbackSettings: puppet.getPlaybackSettings() } : {}),
    rig: {
      ...loadedSettings.rig,
      head: { ...loadedSettings.rig?.head, ...pick(rig.head, ['yawScale', 'pitchScale', 'rollScale', 'depthScale', 'focal']) },
      body: { ...loadedSettings.rig?.body, ...pick(rig.body, ['twistScale', 'pitchScale', 'leanScale', 'breathLift', 'breathWiden']) },
      eyes: { ...loadedSettings.rig?.eyes, ...Object.fromEntries(Object.entries(rig.eyes || {}).map(([k, e]) => [k, { ...loadedSettings.rig?.eyes?.[k], irisRange: e.irisRange }])) },
    },
    chains: { ...loadedSettings.chains, ...Object.fromEntries(model.chains.map((c) => [c.id, { ...loadedSettings.chains?.[c.id], ...pick(c, chainKeys.map((k) => k[0])) }])) },
  };
}
$('save').onclick = async () => {
  const res = await fetch(new URL(`api/settings?model=${encodeURIComponent(modelDir)}`, location.href), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(collectSettings(), null, 2),
  }).catch((e) => ({ ok: false, statusText: e.message }));
  status(res.ok ? '保存しました' : `保存失敗: ${res.statusText}（strig serve で起動してください）`);
};
$('export').onclick = () => {
  const blob = new Blob([JSON.stringify(collectSettings(), null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'settings.json';
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 0);
};

// ------------------------------------------------------------------ interaction
const stage = $('stage');
let drag = null;
stage.addEventListener('contextmenu', (e) => e.preventDefault());
stage.addEventListener('pointerdown', (e) => {
  stage.setPointerCapture(e.pointerId);
  drag = { button: e.button, x: e.clientX, y: e.clientY, cam: { ...renderer.camera }, look: animator.options.lookAt };
  if (e.button === 0) animator.options.lookAt = true;
});
stage.addEventListener('pointermove', (e) => {
  const r = stage.getBoundingClientRect();
  // look target relative to the face position on screen
  const [sx, sy] = modelToScreen(rig.head.center[0], rig.head.center[1]);
  animator.look.x = (e.clientX - r.left - sx) / (r.width * 0.35);
  animator.look.y = -(e.clientY - r.top - sy) / (r.height * 0.35);
  if (drag && drag.button === 2) {
    const s = viewScale();
    renderer.camera.x = drag.cam.x - (e.clientX - drag.x) / s;
    renderer.camera.y = drag.cam.y - (e.clientY - drag.y) / s;
  }
});
function endDrag() {
  if (drag && drag.button === 0) animator.options.lookAt = drag.look;
  drag = null;
}
stage.addEventListener('pointerup', endDrag);
stage.addEventListener('pointercancel', endDrag);
stage.addEventListener('lostpointercapture', endDrag);
stage.addEventListener('wheel', (e) => {
  e.preventDefault();
  renderer.camera.zoom = Math.min(12, Math.max(0.3, renderer.camera.zoom * Math.exp(-e.deltaY * 0.0012)));
}, { passive: false });

function viewScale() {
  const m = model.canvas;
  const dpr = window.devicePixelRatio || 1;
  return (Math.min(canvas.width / m.width, canvas.height / m.height) * renderer.camera.zoom) / dpr;
}
function modelToScreen(x, y) {
  const s = viewScale();
  const dpr = window.devicePixelRatio || 1;
  return [canvas.width / dpr / 2 + (x - renderer.camera.x) * s, canvas.height / dpr / 2 + (y - renderer.camera.y) * s];
}

// ------------------------------------------------------------------ overlay (guides)
const octx = overlay.getContext('2d');
function drawOverlay() {
  const dpr = window.devicePixelRatio || 1;
  const w = Math.round(overlay.clientWidth * dpr), h = Math.round(overlay.clientHeight * dpr);
  if (overlay.width !== w || overlay.height !== h) {
    overlay.width = w;
    overlay.height = h;
  }
  octx.setTransform(dpr, 0, 0, dpr, 0, 0);
  octx.clearRect(0, 0, overlay.width, overlay.height);
  if (!$('guides').checked) return;
  const dot = (x, y, c, r = 4) => {
    const [sx, sy] = modelToScreen(x, y);
    octx.fillStyle = c;
    octx.beginPath();
    octx.arc(sx, sy, r, 0, Math.PI * 2);
    octx.fill();
  };
  dot(rig.head.center[0], rig.head.center[1], '#ff5a7a');
  dot(rig.head.pivot[0], rig.head.pivot[1], '#ffb35a');
  dot(rig.body.pivot[0], rig.body.pivot[1], '#5affb0');
  if (puppet.physicsEnabled) {
    for (const c of puppet.physics.debugPoints()) {
      octx.strokeStyle = 'rgba(90,167,255,.8)';
      octx.beginPath();
      for (let i = 0; i < c.pos.length; i += 2) {
        const [sx, sy] = modelToScreen(c.pos[i], c.pos[i + 1]);
        i ? octx.lineTo(sx, sy) : octx.moveTo(sx, sy);
      }
      octx.stroke();
      for (let i = 0; i < c.pos.length; i += 2) dot(c.pos[i], c.pos[i + 1], 'rgba(90,167,255,.9)', 2.5);
    }
  }
}

// ------------------------------------------------------------------ loop
let last = performance.now();
let frames = 0, fpsT = last;
function frame(now) {
  const dt = Math.max(0, Math.min(0.05, (now - last) / 1000));
  last = now;
  const P = puppet.step(dt);
  drawOverlay();
  frames++;
  if (now - fpsT > 500) {
    $('fps').textContent = `${Math.round((frames * 1000) / (now - fpsT))} fps`;
    frames = 0;
    fpsT = now;
    for (const d of params.defs) if (!(d.id in animator.manual)) paramUI[d.id].out.textContent = (+P[d.id]).toFixed(2);
  }
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
