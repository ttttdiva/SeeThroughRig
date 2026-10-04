import test from 'node:test';
import assert from 'node:assert/strict';
import { createPlaybackSettingsPanel, PLAYBACK_SETTINGS_DEFINITION, resolvePlaybackSettings } from '../../web/js/runtime/index.js';

// DOM contract tests only, not real browser rendering/visual QA.
class Element {
  constructor(tagName, ownerDocument) {
    this.tagName = tagName; this.ownerDocument = ownerDocument; this.children = []; this.dataset = {}; this.listeners = new Map(); this.attributes = {};
  }
  append(...children) { for (const child of children) { child.parentNode = this; this.children.push(child); } }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  removeEventListener(name) { this.listeners.delete(name); }
  remove() { this.parentNode.children = this.parentNode.children.filter(child => child !== this); }
  all() { return [this, ...this.children.flatMap(child => child.all())]; }
}
const document = { createElement(tagName) { return new Element(tagName, this); } };
const container = () => document.createElement('div');
const paths = PLAYBACK_SETTINGS_DEFINITION.groups.flatMap(group => group.fields.map(field => `${group.id}.${field.id}`));
const capabilities = { coreRef: 'canonical', playbackSettings: paths };
const input = (parent, path) => parent.all().find(node => node.attributes['data-playback-path'] === path);
const number = (parent, path) => parent.all().find(node => node.attributes['data-playback-number'] === path);
function edit(node, value) {
  if (node.type === 'checkbox') node.checked = value; else node.value = String(value);
  node.listeners.get(node.type === 'range' ? 'input' : 'change')();
}

test('shared panel renders canonical fields and edits an isolated full snapshot', () => {
  const parent = container();
  const original = resolvePlaybackSettings();
  let result;
  const panel = createPlaybackSettingsPanel(parent, { settings: original, capabilities, onChange: value => result = value });
  assert.equal(parent.all().filter(node => node.attributes['data-playback-path']).length, paths.length);
  edit(input(parent, 'blink.holdDuration'), .23);
  assert.equal(result.blink.holdDuration, .23);
  assert.equal(result.version, 1);
  assert.equal(original.blink.holdDuration, .035);
  assert.ok(parent.all().filter(node => node.tagName === 'label').every(label => label.htmlFor));
  panel.dispose(); panel.dispose();
  assert.equal(parent.children.length, 0);
});

test('ordered sliders share native normalization and fixed mode disables ignored maximum', () => {
  const parent = container(); let result;
  createPlaybackSettingsPanel(parent, { capabilities, onChange: value => result = value });
  edit(input(parent, 'blink.intervalMin'), 12);
  assert.equal(result.blink.intervalMax, 12);
  edit(input(parent, 'blink.intervalMax'), 2);
  assert.equal(result.blink.intervalMin, 2);
  edit(input(parent, 'blink.intervalMode'), 'fixed');
  assert.equal(input(parent, 'blink.intervalMax').disabled, true);
  edit(input(parent, 'blink.intervalMin'), 15);
  assert.equal(result.blink.intervalMax, 2);
  edit(input(parent, 'blink.intervalMode'), 'random');
  assert.equal(result.blink.intervalMax, 15);
  assert.equal(input(parent, 'blink.intervalMax').disabled, false);
});

test('capability, busy and replaced model state disable unavailable edits without emitting', () => {
  const parent = container(); let calls = 0;
  const panel = createPlaybackSettingsPanel(parent, { capabilities, onChange: () => calls++ });
  panel.update({ disabled: true });
  edit(input(parent, 'wind.wind'), .3);
  assert.equal(calls, 0);
  panel.update({ disabled: false, capabilities: { coreRef: 'canonical', playbackSettings: ['wind.wind'] }, settings: { wind: { wind: .6 } } });
  assert.equal(input(parent, 'wind.wind').value, '0.6');
  assert.equal(input(parent, 'blink.enabled').disabled, true);
  edit(input(parent, 'wind.wind'), .7);
  assert.equal(calls, 1);
  panel.update({ capabilities: { coreRef: 'historical', playbackSettings: [] } });
  assert.equal(input(parent, 'wind.wind').disabled, true);
  const stale = input(parent, 'wind.wind');
  panel.dispose();
  assert.equal(stale.listeners.size, 0);
});

test('host rejection restores last accepted settings and reports an accessible error', () => {
  const parent = container();
  createPlaybackSettingsPanel(parent, { capabilities, onChange: () => { throw new Error('Model changed'); } });
  edit(input(parent, 'wind.wind'), .3);
  assert.equal(input(parent, 'wind.wind').value, '0');
  assert.equal(parent.all().find(node => node.attributes.role === 'status').textContent, 'Model changed');
});

test('numeric inputs share slider bounds and permit empty typing without emitting NaN', () => {
  const parent = container(); let result, count = 0;
  createPlaybackSettingsPanel(parent, { capabilities, onChange: value => { result = value; count++; } });
  const numeric = number(parent, 'blink.closeDuration');
  assert.equal(numeric.min, .01); assert.equal(numeric.max, .5);
  assert.ok(numeric.attributes['aria-label'].includes('閉じる時間'));
  for (const value of ['', 'oops', '-1', '2']) {
    numeric.value = value; numeric.listeners.get('input')();
    assert.equal(count, 0);
    assert.equal(numeric.attributes['aria-invalid'], 'true');
  }
  numeric.listeners.get('blur')();
  assert.equal(numeric.value, '0.075');
  numeric.value = '.125'; numeric.listeners.get('input')();
  assert.equal(result.blink.closeDuration, .125);
  assert.equal(input(parent, 'blink.closeDuration').value, '0.125');
  assert.equal(count, 1);
  const groups = parent.all().filter(node => node.tagName === 'details');
  assert.deepEqual(groups.map(node => node.open), [true, true, false, false, false, false]);
});

test('wind switch disables both numeric controls and preserves strengths when re-enabled', () => {
  const parent=container();let result;
  createPlaybackSettingsPanel(parent,{capabilities,settings:{wind:{wind:.35,windBlow:.7}},onChange:value=>result=value});
  edit(input(parent,'wind.enabled'),false);
  assert.equal(result.wind.enabled,false);assert.equal(result.wind.wind,.35);assert.equal(result.wind.windBlow,.7);
  for(const path of ['wind.wind','wind.windBlow']) {assert.equal(input(parent,path).disabled,true);assert.equal(number(parent,path).disabled,true);}
  edit(input(parent,'wind.enabled'),true);
  assert.equal(result.wind.enabled,true);assert.equal(result.wind.wind,.35);assert.equal(result.wind.windBlow,.7);
  for(const path of ['wind.wind','wind.windBlow']) {assert.equal(input(parent,path).disabled,false);assert.equal(number(parent,path).disabled,false);}
});

test('group filtering renders selected canonical fields without dropping hidden settings', () => {
  const parent = container(); let result;
  const original = resolvePlaybackSettings({ wind: { wind: .35 }, idle: { speed: 1.7 }, physics: { gain: .65 } });
  const panel = createPlaybackSettingsPanel(parent, {
    capabilities, settings: original, groups: ['wind', 'blink', 'breath'], onChange: value => result = value,
  });
  assert.deepEqual(parent.all().filter(node => node.attributes['data-playback-group']).map(node => node.attributes['data-playback-group']), ['wind', 'blink', 'breath']);
  assert.equal(parent.all().filter(node => node.attributes['data-playback-path']).length, paths.filter(path => /^(wind|blink|breath)\./.test(path)).length);
  assert.equal(input(parent, 'physics.gain'), undefined);
  assert.ok(parent.all().filter(node => node.tagName === 'details').every(node => node.open));
  edit(input(parent, 'wind.wind'), .7);
  assert.deepEqual(result, resolvePlaybackSettings(original, { wind: { wind: .7 } }));
  assert.equal(original.wind.wind, .35);

  const stale = input(parent, 'wind.wind');
  panel.update({ groups: ['physics'] });
  assert.equal(stale.listeners.size, 0);
  assert.equal(input(parent, 'wind.wind'), undefined);
  edit(input(parent, 'physics.gain'), .9);
  assert.equal(result.wind.wind, .7);
  assert.equal(result.idle.speed, 1.7);
  assert.equal(result.physics.gain, .9);
  panel.update({ groups: [] });
  assert.equal(parent.all().filter(node => node.attributes['data-playback-path']).length, 0);
  panel.update({ groups: undefined });
  assert.equal(parent.all().filter(node => node.attributes['data-playback-path']).length, paths.length);
  assert.equal(input(parent, 'wind.wind').value, '0.7');
  panel.dispose();
  assert.equal(parent.children.length, 0);
});

test('split panels synchronize complete drafts across alternating edits, reset and model replacement', () => {
  const left = container(), right = container(), panels = [];
  let draft = resolvePlaybackSettings({ wind: { wind: .25 }, breath: { period: 4.6 }, idle: { speed: 1.2 } });
  let changes = 0;
  const sync = settings => { draft = resolvePlaybackSettings(settings); panels.forEach(panel => panel.update({ settings: draft })); };
  const onChange = settings => { changes++; sync(settings); };
  panels.push(createPlaybackSettingsPanel(left, { capabilities, settings: draft, groups: ['wind', 'blink', 'breath'], onChange }));
  panels.push(createPlaybackSettingsPanel(right, { capabilities, settings: draft, groups: ['idle', 'gaze', 'physics'], onChange }));
  edit(input(left, 'wind.windBlow'), .75);
  edit(input(right, 'physics.gain'), .45);
  edit(input(left, 'blink.holdDuration'), .2);
  edit(input(right, 'gaze.range'), .6);
  assert.equal(changes, 4);
  assert.equal(draft.wind.wind, .25);
  assert.equal(draft.wind.windBlow, .75);
  assert.equal(draft.physics.gain, .45);
  assert.equal(draft.blink.holdDuration, .2);
  assert.equal(draft.breath.period, 4.6);
  assert.equal(draft.idle.speed, 1.2);
  assert.equal(draft.gaze.range, .6);
  assert.deepEqual(JSON.parse(JSON.stringify(draft)), draft); // Save/export use the same complete draft.

  sync(resolvePlaybackSettings());
  assert.equal(changes, 4); // Programmatic reset must not emit changes.
  assert.equal(input(left, 'wind.windBlow').value, '0');
  assert.equal(input(right, 'physics.gain').value, '1');
  sync(resolvePlaybackSettings({ wind: { wind: .1 }, physics: { gain: .8 } }));
  edit(input(left, 'breath.period'), 5);
  assert.equal(draft.physics.gain, .8);
  assert.equal(draft.wind.wind, .1);
  for (const panel of panels) panel.update({ capabilities: { coreRef: 'historical', playbackSettings: [] } });
  assert.equal(input(left, 'wind.wind').disabled, true);
  assert.equal(input(right, 'physics.gain').disabled, true);
  edit(input(right, 'physics.gain'), .3);
  assert.equal(draft.physics.gain, .8);
  panels.forEach(panel => panel.dispose());
});
