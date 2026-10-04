import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import { RigRuntime, createPlaybackSettingsPanel } from '../../web/js/runtime/index.js';
import { Timeline } from '../../web/js/core/motion.js';
import { mockGL } from './runtime.test.mjs';

// Execute the actual editor event handlers with DOM/mocked GL, not visual QA.
class Element {
  constructor(tagName, ownerDocument) {
    this.tagName = tagName; this.ownerDocument = ownerDocument; this.children = [];
    this.dataset = {}; this.attributes = {}; this.listeners = new Map();
    this.value = ''; this.width = 100; this.height = 100;
    const classes = new Set();
    this.classList = {
      add: value => classes.add(value), contains: value => classes.has(value),
      toggle: (value, on) => on ? classes.add(value) : classes.delete(value),
    };
  }
  append(...children) { for (const child of children) { child.parentNode = this; this.children.push(child); } }
  add(child) { this.append(child); }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  removeEventListener(name) { this.listeners.delete(name); }
  remove() { this.parentNode.children = this.parentNode.children.filter(child => child !== this); }
  getContext() { return {}; }
  all() { return [this, ...this.children.flatMap(child => child.all())]; }
}

async function openEditor() {
  const elements = new Map();
  const document = {
    createElement(tagName) { return new Element(tagName, this); },
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, this.createElement('div'));
      return elements.get(id);
    },
  };
  document.getElementById('bg').value = '1,1,1';
  const window = {}, gl = mockGL();
  const model = JSON.parse(await fs.readFile(new URL('../../web/models/aoi/model.json', import.meta.url), 'utf8'));
  model.images = model.atlases.map(() => ({}));
  const source = (await fs.readFile(new URL('../../web/js/editor.js', import.meta.url), 'utf8'))
    .replace(/^import .+;\n/gm, '');
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  const run = new AsyncFunction('Puppet', 'createPlaybackSettingsPanel', 'loadStandaloneSettings', 'Timeline',
    'document', 'location', 'window', 'Option', 'requestAnimationFrame', source);
  await run({ create: async (canvas, url, options) => RigRuntime.fromData(canvas, model, { ...options, gl }) },
    createPlaybackSettingsPanel, async () => ({}), Timeline, document, { search: '' }, window,
    function Option(text, value) { return Object.assign(document.createElement('option'), { text, value }); }, () => {});
  return { runtime: window.puppet, document, gl };
}

function edit(document, path, value) {
  const controls = ['playbackSettingsLeft', 'playbackSettingsRight'].flatMap(id => document.getElementById(id).all());
  const control = controls.find(node => node.attributes['data-playback-path'] === path);
  assert.ok(control && !control.disabled, path + ' must be editable');
  if (control.type === 'checkbox') control.checked = value; else control.value = String(value);
  control.listeners.get(control.type === 'range' ? 'input' : 'change')();
  for (const node of controls.filter(node => node.attributes.role === 'status')) assert.equal(node.textContent, '');
}

test('editor expression buttons retain selected and pending expressions across playback edits', async () => {
  const { runtime, document, gl } = await openEditor();
  const buttons = document.getElementById('exprs').children;
  const button = id => buttons[runtime.model.expressions.findIndex(expression => expression.id === id)];
  const advance = () => { for (let i = 0; i < 120; i++) runtime.step(1 / 60); };
  try {
    button('happy').onclick();
    advance();
    assert.ok(runtime.params.get('MouthHappy') > .9);
    for (const [path, value] of [['wind.wind', .6], ['blink.enabled', false], ['breath.period', 5], ['idle.speed', 1.2]]) {
      edit(document, path, value);
      advance();
      assert.equal(runtime.animator.exprTarget, 'happy', path);
      assert.ok(runtime.params.get('MouthHappy') > .9, path);
      assert.ok(button('happy').classList.contains('active'));
      assert.equal(button('neutral').classList.contains('active'), false);
    }
    button('neutral').onclick();
    edit(document, 'blink.enabled', true);
    advance();
    assert.equal(runtime.animator.exprTarget, null);
    assert.ok(runtime.params.get('MouthHappy') < .1);
    assert.ok(button('neutral').classList.contains('active'));

    // Edit before the blink-before-swap transition completes, including repeated selection.
    button('happy').onclick(); button('happy').onclick();
    edit(document, 'wind.wind', .3);
    advance();
    assert.equal(runtime.animator.exprTarget, 'happy');
    assert.ok(runtime.params.get('MouthHappy') > .9);
  } finally {
    runtime.dispose();
    assert.equal(gl.live.size, 0);
  }
});
