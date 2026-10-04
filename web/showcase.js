// Deterministic public showcase host. It uses the same WebGL runtime as the
// player, adding only a presentation HUD and camera/layout cues for recording.
import { RigRuntime } from './js/runtime/index.js';
import { Timeline } from './js/core/motion.js';

const qs = new URLSearchParams(location.search);
const modelDir = qs.get('model') || 'models/aoi';
const canvas = document.getElementById('gl');
const errorBox = document.getElementById('error');
const kicker = document.getElementById('segment-kicker');
const title = document.getElementById('segment-title');
const detail = document.getElementById('segment-detail');
const timecode = document.getElementById('timecode');
const expression = document.getElementById('expression');
const dataNodes = Object.fromEntries([...document.querySelectorAll('[data-param]')].map((el) => [el.dataset.param, el]));

const expressionLabels = {
  neutral: '通常 / normal',
  happy: 'にっこり / smile',
  surprised: '驚き / surprise',
  worried: '困り / worry',
  shy: '照れ / blush',
};
const vowelLabels = ['legacy', 'あ / a', 'い / i', 'う / u', 'え / e', 'お / o'];

function showError(error) {
  const text = error?.stack || error?.message || String(error);
  errorBox.textContent = `Showcase failed to initialize.\n${text}`;
  errorBox.style.display = 'block';
  window.__showcaseError = text;
}

function formatNumber(value, digits = 2) {
  return Number.isFinite(value) ? value.toFixed(digits) : '—';
}

function formatTime(seconds) {
  const whole = Math.max(0, Number(seconds) || 0);
  const minutes = Math.floor(whole / 60);
  const rest = whole - minutes * 60;
  return `${String(minutes).padStart(2, '0')}:${rest.toFixed(1).padStart(4, '0')}`;
}

function activeCaption(data, t) {
  const entries = data.captions || [];
  return entries.find((entry, i) => t >= entry[0] && (t < entry[1] || i === entries.length - 1)) || entries[entries.length - 1];
}

function applyCamera(data, t, renderer) {
  const keys = data.camera;
  if (!Array.isArray(keys) || !keys.length) return;
  let left = keys[0], right = keys[keys.length - 1];
  for (let i = 0; i < keys.length; i++) {
    if (keys[i][0] <= t) left = keys[i];
    if (keys[i][0] >= t) { right = keys[i]; break; }
  }
  const u = right[0] > left[0] ? Math.max(0, Math.min(1, (t - left[0]) / (right[0] - left[0]))) : 0;
  const s = u * u * (3 - 2 * u);
  renderer.camera.x = left[1] + (right[1] - left[1]) * s;
  renderer.camera.y = left[2] + (right[2] - left[2]) * s;
  renderer.camera.zoom = left[3] + (right[3] - left[3]) * s;
}

function updateHud(data, puppet, t) {
  const c = activeCaption(data, t);
  if (c) {
    kicker.textContent = c[4] || `${String((data.captions || []).indexOf(c) + 1).padStart(2, '0')} / SHOWCASE`;
    title.textContent = c[2] || '';
    detail.textContent = c[3] || '';
  }
  timecode.textContent = formatTime(t);
  const values = puppet.params.values;
  dataNodes.AngleZ.textContent = `${formatNumber(values.AngleZ, 1)}°`;
  dataNodes.AngleY.textContent = `${formatNumber(values.AngleY, 1)}°`;
  dataNodes.BodyAngleY.textContent = formatNumber(values.BodyAngleY, 1);
  dataNodes.BodyAngleX.textContent = formatNumber(values.BodyAngleX, 1);
  dataNodes.BodyAngleZ.textContent = `${formatNumber(values.BodyAngleZ, 1)}°`;
  dataNodes.Breath.textContent = formatNumber(values.Breath, 2);
  dataNodes.EyeBall.textContent = `${formatNumber(values.EyeBallX, 2)} / ${formatNumber(values.EyeBallY, 2)}`;
  dataNodes.Eyes.textContent = `${formatNumber(values.EyeLOpen, 2)} / ${formatNumber(values.EyeROpen, 2)}`;
  dataNodes.MouthOpenY.textContent = formatNumber(values.MouthOpenY, 2);
  const vowel = Math.max(0, Math.min(5, Math.round(values.MouthVowel || 0)));
  dataNodes.MouthVowel.innerHTML = `${vowel} <em>${vowelLabels[vowel]}</em>`;
  dataNodes.Wind.textContent = `${formatNumber(values.Wind, 2)} / ${formatNumber(values.WindBlow, 2)}`;
  dataNodes.PhysicsGain.textContent = formatNumber(values.PhysicsGain, 2);
  const expr = puppet.animator.exprTarget || 'neutral';
  expression.textContent = `表情: ${expressionLabels[expr] || expr}`;
}

try {
  const record = qs.has('record');
  const motionUrl = qs.get('motion') || 'motions/showcase.json';
  const data = await (await fetch(motionUrl, { cache: 'no-cache' })).json();
  const timeline = new Timeline(data);
  const puppet = await RigRuntime.create(canvas, `${modelDir}/model.json`, {
    coreRef: qs.get('coreRef') || 'canonical',
    // The public model intentionally has no optional standalone settings file.
    settings: {},
    preserveDrawingBuffer: true,
    seed: Number(qs.get('seed') || 17),
  });
  const { renderer, animator } = puppet;
  // Keep the art left of the HUD while retaining the full figure in frame.
  renderer.background = [0.027, 0.055, 0.11, 1];
  Object.assign(renderer.camera, { x: 800, y: 760, zoom: 0.82 });
  animator.playTimeline(timeline, 0);
  // Showcase motion supplies its own gaze and wind tracks. Idle/breath/blink
  // remain enabled so the recording contains the real runtime layers too.
  animator.options.lookAt = false;

  let lastFrame = 0;
  function apply(t) {
    applyCamera(data, t, renderer);
    const c = activeCaption(data, t);
    if (c && c[5]) Object.assign(renderer.camera, c[5]);
  }
  function frame(t, fps = 30) {
    const target = Math.max(0, Number(t) || 0);
    if (target + 1e-7 < lastFrame) throw new RangeError('Showcase frame time must be monotonic');
    const dt = 1 / fps;
    while (puppet.time + dt * 0.5 < target) {
      const next = puppet.time + dt;
      apply(next);
      puppet.step(dt);
      lastFrame = puppet.time;
    }
    apply(puppet.time);
    updateHud(data, puppet, puppet.time);
    return puppet.time;
  }

  apply(0);
  puppet.step(0);
  updateHud(data, puppet, 0);
  window.__showcase = {
    puppet,
    data,
    duration: Number(data.duration),
    frame,
    snapshot(type = 'image/jpeg', quality = 0.95) { return canvas.toDataURL(type, quality); },
    capabilities: puppet.capabilities,
  };
  window.__showcaseReady = true;

  if (!record) {
    let last = performance.now();
    const loop = (now) => {
      const dt = Math.max(0, Math.min(0.05, (now - last) / 1000));
      last = now;
      apply(puppet.time + dt);
      puppet.step(dt);
      updateHud(data, puppet, puppet.time);
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
  }
} catch (error) {
  showError(error);
}
