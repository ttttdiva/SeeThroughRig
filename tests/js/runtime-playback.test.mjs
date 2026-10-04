import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import { RigRuntime, BASELINE_CORE_REF, DEFAULT_PLAYBACK_SETTINGS, PLAYBACK_SETTINGS_DEFINITION, resolvePlaybackSettings, validatePlaybackSettings, playbackSettingsFields, updatePlaybackSetting, filterPlaybackSettings } from '../../web/js/runtime/index.js';
import { Animator, AutoBlink } from '../../web/js/core/motion.js';
import { Params } from '../../web/js/core/params.js';
import { decodeModel } from '../../web/js/core/model.js';
import { fixture, mockGL } from './runtime.test.mjs';
function playbackFixture() {
  const m = fixture();
  for (const parameter of ['EyeLOpen','EyeROpen','EyeBallX','EyeBallY']) {
    const d = structuredClone(m.drawables[0]); d.id=parameter; d.opacity=[{param:parameter,keys:[[-1,0],[1,1]]}]; m.drawables.push(d);
  }
  m.chains=[{id:'hair',points:[[50,40],[50,50],[50,60]],attrs:structuredClone(m.drawables[0].attrs),wind:30}];
  m.drawables[0].deformers.push({type:'chain',chain:'hair',t:[0,0.5,1],w:[1,1,1]});
  return m;
}
function make(options={},model=playbackFixture()) {return RigRuntime.fromData({width:100,height:100},model,{gl:mockGL(),...options});}
function pose(runtime) {return {P:{...runtime.params.values},geometry:[...runtime.deformer.out].map(([id,p])=>[id,[...p]]),physics:runtime.physics.debugPoints()};}

test('canonical JSON metadata and portable JS data module are byte-for-data identical', async()=>{
  const json=JSON.parse(await fs.readFile(new URL('../../web/js/runtime/playback-settings.schema.json',import.meta.url),'utf8'));
  assert.deepEqual(PLAYBACK_SETTINGS_DEFINITION,json); assert.ok(Object.isFrozen(DEFAULT_PLAYBACK_SETTINGS.blink));
});
test('strict schema validates every numeric boundary and rejects unsafe or unknown values',()=>{
  for(const group of PLAYBACK_SETTINGS_DEFINITION.groups) for(const field of group.fields) {
    if(field.type==='number') {
      for(const value of [field.min,field.max]) assert.equal(updatePlaybackSetting(DEFAULT_PLAYBACK_SETTINGS,group.id,field.id,value)[group.id][field.id],value);
      for(const value of [field.min-0.001,field.max+0.001,NaN,Infinity,'1',true,null]) assert.throws(()=>validatePlaybackSettings({[group.id]:{[field.id]:value}}),undefined,group.id+'.'+field.id);
    }
    if(field.type==='boolean') for(const value of [0,1,'true',null]) assert.throws(()=>validatePlaybackSettings({[group.id]:{[field.id]:value}}));
  }
  for(const value of [null,[],{version:2},{unknown:{}},{blink:{unknown:1}},JSON.parse('{"__proto__":{}}'),{blink:{intervalMode:'maybe'}}]) assert.throws(()=>validatePlaybackSettings(value));
  assert.throws(()=>resolvePlaybackSettings({blink:{intervalMin:7}}),/must be <=/);
  assert.throws(()=>resolvePlaybackSettings({gaze:{intervalMin:4}}),/must be <=/);
  assert.equal(resolvePlaybackSettings({blink:{intervalMode:'fixed',intervalMin:7,intervalMax:1}}).blink.intervalMin,7);
  const fixed=resolvePlaybackSettings({blink:{intervalMode:'fixed',intervalMin:7,intervalMax:1}});
  assert.equal(updatePlaybackSetting(fixed,'blink','intervalMode','random').blink.intervalMax,7);
  assert.equal(resolvePlaybackSettings(fixed,{blink:{intervalMin:12,intervalMax:2}}).blink.intervalMin,12,'partial patch inherits existing fixed mode before conditional bounds');
  assert.throws(()=>resolvePlaybackSettings(fixed,{blink:{intervalMode:'random'}}),/must be <=/,'changing mode still validates final ordered bounds');
  assert.throws(()=>resolvePlaybackSettings({blink:{intervalMin:12,intervalMax:2}}),/must be <=/,'absent mode resolves to random default and rejects');
});
test('playback layers preserve model ownership and resolve profile/model/tuning/manifest/host order',()=>{
  const model=playbackFixture(); model.playbackSettings={version:1,wind:{wind:.2},breath:{period:4}};
  const options={profile:{id:'conversation-v1',wind:.1},settings:{playbackSettings:{version:1,wind:{wind:.3}}},manifest:{runtime:{abi:1,version:'0.1.0'},playbackSettings:{version:1,wind:{wind:.4}}},playbackSettings:{wind:{wind:.5}}};
  const before=structuredClone(model), optionsBefore=structuredClone(options),runtime=make(options,model);
  assert.equal(runtime.getPlaybackSettings().wind.wind,.5);assert.equal(runtime.getPlaybackSettings().breath.period,4);
  assert.deepEqual(model,before);assert.deepEqual(options,optionsBefore);
  const settings=runtime.getPlaybackSettings();settings.wind.wind=1;assert.equal(runtime.getPlaybackSettings().wind.wind,.5);
  runtime.dispose();
  assert.throws(()=>make({},Object.assign(playbackFixture(),{playbackSettings:{wind:{wind:0}}})),/version/);
});
test('live edits are transactional, capability gated, and deterministic over seeded seek/replay',()=>{
  const runtime=make();const animator=runtime.animator;
  runtime.setPlaybackSettings({blink:{intervalMin:.3,intervalMax:.7,durationRandomness:.7,doubleProbability:.5},gaze:{intervalMin:.2,intervalMax:.7,range:.4},breath:{period:2,amplitude:.5},idle:{speed:.8,headAmplitude:.5,bodyAmplitude:.7},wind:{wind:.6},physics:{gain:.3}});
  assert.equal(runtime.animator,animator,'normal live edits retain animator for editor bindings');
  runtime.setExpression('happy'); runtime.frame(4); const expected=pose(runtime);
  runtime.frame(1);runtime.frame(4);assert.deepEqual(pose(runtime),expected);runtime.frame(4);assert.deepEqual(pose(runtime),expected);
  const other=make({playbackSettings:runtime.getPlaybackSettings()});other.setExpression('happy');other.frame(4);assert.deepEqual(pose(other),expected);
  const old=runtime.getPlaybackSettings();assert.throws(()=>runtime.setPlaybackSettings({blink:{intervalMin:10}}));assert.deepEqual(runtime.getPlaybackSettings(),old);
  runtime.dispose();other.dispose();
});
test('absent geometry and immutable comparison/historical cores expose disabled controls',()=>{
  const bare=make({},fixture()); assert.ok(!bare.capabilities.playbackSettings.includes('wind.wind'));assert.ok(!bare.capabilities.playbackSettings.includes('wind.enabled'));assert.throws(()=>bare.setPlaybackSettings({wind:{enabled:false}}),/Unsupported/);assert.throws(()=>bare.setPlaybackSettings({wind:{wind:1}}),/Unsupported/);
  const windGroup=playbackSettingsFields(bare.capabilities).find(x=>x.id==='wind');assert.equal(windGroup.supported,false);
  const external=resolvePlaybackSettings({wind:{wind:1},breath:{period:4}});const filtered=filterPlaybackSettings(external,bare.capabilities);assert.equal(filtered.wind,undefined);bare.setPlaybackSettings(filtered);assert.equal(bare.getPlaybackSettings().breath.period,4);bare.dispose();
  for(const options of [{profile:'comparison-v1'},{coreRef:BASELINE_CORE_REF},{coreRef:BASELINE_CORE_REF,profile:'comparison-v1'}]) {
    const runtime=make(options);assert.deepEqual(runtime.capabilities.playbackSettings,[]);assert.throws(()=>runtime.setPlaybackSettings({breath:{enabled:false}}),/fixed/);runtime.dispose();
  }
  const m=playbackFixture();m.playbackSettings={version:1,blink:{enabled:false},idle:{enabled:false},breath:{period:1}};
  const locked=make({profile:'comparison-v1'},m), plain=make({profile:'comparison-v1'});
  locked.frame(5);plain.frame(5);assert.deepEqual(pose(locked),pose(plain));locked.dispose();plain.dispose();
});
test('blink scheduler is seeded, partition independent, bounded and fixed interval honors double gap',()=>{
  const settings=resolvePlaybackSettings({blink:{intervalMode:'fixed',intervalMin:.2,closeDuration:.02,holdDuration:0,openDuration:.03,doubleProbability:1,doubleGap:.05}}).blink;
  const a=new AutoBlink(9),b=new AutoBlink(9);a.configure(settings);b.configure(settings);
  assert.equal(a.update(.2),1);assert.ok(Math.abs(a.update(.21)-.5)<1e-12);assert.ok(a.update(.22)<1e-12);assert.equal(a.update(.251),1);assert.ok(Math.abs(a.next-.3)<1e-12);assert.equal(a.update(.3),1);
  const randomized={...settings,intervalMode:'random',intervalMin:.2,intervalMax:1,durationRandomness:.8,doubleProbability:.4};
  a.configure(randomized,0);b.configure(randomized,0); // reset separately below for identical RNG state
  const sample=(step,seed)=>{const x=new AutoBlink(seed);x.configure(randomized);const out=[];for(let time=step;time<20;time+=step)x.update(time);for(const t of [20,20.1,20.7,22,50])out.push(x.update(t));return {out,next:x.next,durations:x.durations};};
  assert.deepEqual(sample(.01,7),sample(.07,7));assert.notDeepEqual(sample(.01,7),sample(.01,8));
  const extreme=new AutoBlink(1);extreme.configure({...settings,closeDuration:.01,openDuration:.01,durationRandomness:.8});for(let i=0;i<1000;i++){const value=extreme.update(i*.05);assert.ok(value>=0&&value<=1);if(extreme.durations){assert.ok(extreme.durations.close>0);assert.ok(extreme.durations.open>0);}}
});
test('expression waits for actual long randomized close and safely completes when blinking is disabled',()=>{
  const model=decodeModel(playbackFixture()),a=new Animator(new Params(),model,14);
  a.setPlaybackSettings(resolvePlaybackSettings({blink:{closeDuration:.5,durationRandomness:.7}}));a.setExpression('happy');a.update(.01,.01);
  const closedAt=a.blink.closedAt;assert.ok(closedAt>.1);
  for(let t=.02;t<closedAt;t+=.01){a.update(t,.01);assert.equal(a.exprTarget,null);}
  a.update(closedAt,.01);assert.equal(a.exprTarget,'happy');assert.equal(a.params.get('EyeLOpen'),0);
  a.setExpression('neutral');a.update(closedAt+.01,.01);a.setPlaybackSettings(resolvePlaybackSettings({blink:{enabled:false}}),closedAt+.01);a.update(closedAt+.02,.01);assert.equal(a.exprTarget,'neutral');assert.equal(a.params.get('EyeLOpen'),1);
});
test('disabled motion stays off, pointer tracking stops live, and physics toggles without disposal',()=>{
  const runtime=make();runtime.setPlaybackSettings({blink:{enabled:false},breath:{enabled:false},idle:{enabled:false},gaze:{enabled:false},physics:{enabled:false}});runtime.setGaze(1,1);runtime.frame(3);
  for(const id of ['AngleX','AngleY','AngleZ','BodyAngleX','BodyAngleY','BodyAngleZ','Breath','EyeBallX','EyeBallY'])assert.equal(runtime.params.get(id),0,id);
  assert.equal(runtime.params.get('EyeLOpen'),1);assert.equal(runtime.physicsEnabled,false);
  runtime.setPlaybackSettings({gaze:{enabled:true,tracking:true}});runtime.setGaze(1,1);runtime.step(.1);assert.equal(runtime.animator.options.lookAt,true);
  runtime.setPlaybackSettings({gaze:{tracking:false},physics:{enabled:true,gain:0}});assert.equal(runtime.animator.options.lookAt,false);runtime.step(.1);assert.equal(runtime.physicsEnabled,true);assert.equal(runtime.params.get('PhysicsGain'),0);runtime.dispose();
});
test('breeze and blowing wind share bounded live settings on real strand geometry',()=>{
  const model=playbackFixture(),d=model.drawables[0];
  model.strands=[{id:'strand',pts:[[50,40],[50,60],[50,80]],root:{zb:0,twist:0,lean:0,breath:0},droop:.6,physics:{wind:.02,windPush:.05}}];
  d.strandIds=['strand'];d.attrs.stw=[1,1,1];d.attrs.sts=[0,.5,1];
  const runtime=make({},model),originalRandom=Math.random;
  try {
    Math.random=()=>{throw Error('Per-frame unseeded randomness is prohibited');};
    assert.ok(runtime.capabilities.playbackSettings.includes('wind.windBlow'));
    runtime.setPlaybackSettings({wind:{wind:1,windBlow:1},physics:{gain:2}});runtime.frame(2);
    assert.equal(runtime.params.get('Wind'),1);assert.equal(runtime.params.get('WindBlow'),1);assert.equal(runtime.params.get('PhysicsGain'),2);
    const expected=pose(runtime);runtime.frame(0);runtime.frame(2);assert.deepEqual(pose(runtime),expected);
    for(const points of runtime.deformer.out.values())assert.ok([...points].every(Number.isFinite));
    runtime.setPlaybackSettings({wind:{enabled:false}});
    assert.equal(runtime.getPlaybackSettings().wind.wind,1);assert.equal(runtime.getPlaybackSettings().wind.windBlow,1);
    assert.equal(runtime.params.get('Wind'),0);assert.equal(runtime.params.get('WindBlow'),0);
    runtime.setParameters({Wind:1,WindBlow:1});runtime.step(.1);
    assert.equal(runtime.params.get('Wind'),0);assert.equal(runtime.params.get('WindBlow'),0);
    runtime.frame(0);assert.equal(runtime.params.get('Wind'),0);assert.equal(runtime.params.get('WindBlow'),0);
    runtime.frame(2);const disabled=pose(runtime);runtime.frame(0);runtime.frame(2);assert.deepEqual(pose(runtime),disabled);
    runtime.setPlaybackSettings({wind:{enabled:true}});runtime.step(.1);
    assert.equal(runtime.params.get('Wind'),1);assert.equal(runtime.params.get('WindBlow'),1);
    runtime.setPlaybackSettings({wind:{wind:0,windBlow:0}});runtime.step(.1);assert.equal(runtime.params.get('WindBlow'),0);
  } finally { Math.random=originalRandom;runtime.dispose(); }
});
