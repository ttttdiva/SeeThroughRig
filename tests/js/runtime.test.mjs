import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import zlib from 'node:zlib';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { RigRuntime, BASELINE_CORE_REF, deriveCapabilities } from '../../web/js/runtime/index.js';
import { Puppet } from '../../web/js/core/puppet.js';
import { Renderer } from '../../web/js/core/renderer.js';
import { decodeModel, applySettings } from '../../web/js/core/model.js';
import { Params } from '../../web/js/runtime-baseline/params.js';
import { Deformer } from '../../web/js/runtime-baseline/deform.js';
import { Physics } from '../../web/js/runtime-baseline/physics.js';
import { Animator } from '../../web/js/runtime-baseline/motion.js';
const root = fileURLToPath(new URL('../../', import.meta.url));
const hasBaselineHistory = (() => {
  try {
    execFileSync('git', ['-C', root, 'cat-file', '-e', BASELINE_CORE_REF + '^{commit}'], {stdio: 'ignore'});
    return true;
  } catch {
    return false;
  }
})();
export function mockGL(failure = {}) {
  let serial = 0, allocations = 0;
  const live = new Map(), uploads = [], draws = [];
  const gl = new Proxy({ live, uploads, draws }, {get(target, key) {
    if (key in target) return target[key];
    if (String(key).startsWith('create')) return () => {
      if (++allocations === failure.allocation) return null;
      const item = { id: ++serial, kind: String(key).slice(6) }; live.set(item, item.kind); return item;
    };
    if (String(key).startsWith('delete')) return item => { assert.ok(live.has(item), 'delete exactly once: ' + key); live.delete(item); };
    if (key === 'getShaderParameter') return () => !failure.shader;
    if (key === 'getProgramParameter') return () => !failure.link;
    if (key === 'getShaderInfoLog' || key === 'getProgramInfoLog') return () => 'injected compile/link failure';
    if (key === 'getAttribLocation') return (_, name) => { if (failure.location) throw Error('injected location failure'); return name === 'a_pos' ? 0 : 1; };
    if (key === 'getUniformLocation') return (_, name) => name;
    if (key === 'texImage2D') return (...args) => { if (failure.upload) throw Error('injected upload failure'); uploads.push(args.at(-1)); };
    if (key === 'drawElements') return (...args) => draws.push(args);
    if (key === 'bufferData' && failure.buffer) return () => { throw Error('injected buffer failure'); };
    if (String(key).toUpperCase() === key) return 1;
    return () => {};
  }});
  return gl;
}
const rig = {
  head: { mode: 'rigid', center: [50,50], pivot: [50,80], radius: [50,50], yawScale: 1, pitchScale: 1, rollScale: 1, focal: 2000, shift: [0,0,0], bob: 4 },
  body: { axisX: 50, pivot: [50,100], twistScale: 1, pitchScale: 1, leanScale: 1, breathLift: 3, breathWiden: 0, shift: 0, rigid: true, sway: 2, depthScale: 0.02 },
};
function drawable(id, opacity) {
  return {id, order: 1, atlas: 0, positions: [40,40,60,40,50,60], uvs: [0,0,1,0,0.5,1], indices: [0,1,2], attrs: {z:[0,0,0],head:[1,1,1],zb:[0,0,0],twist:[0,0,0],lean:[1,1,1],breath:[1,1,1]}, deformers:[{type:'mouthOpen',anchor:[50,40],minScale:0.1}], opacity};
}
export function fixture() {
  return {canvas:{width:100,height:100},rig:structuredClone(rig),atlases:[{file:'atlas.png'}],drawables:[drawable('mouth'),drawable('mouth-happy',[{param:'MouthHappy',keys:[[0,0],[1,1]]}])],chains:[],expressions:[{id:'neutral',params:{}},{id:'happy',params:{MouthHappy:1,MouthOpenY:0.7,MouthForm:0.6}}],views:{full:{x:50,y:50,zoom:1}},images:[{localUri:'native://texture'}]};
}
const canvas = () => ({width:100,height:100});
function make(options = {}, model = fixture(), Class = RigRuntime) {
  const gl = mockGL(); const runtime = Class.fromData(canvas(), model, {gl, ...options}); return {gl,runtime};
}
function snapshot(runtime) { return {P:{...runtime.params.values},out:[...runtime.deformer.out].map(([id,a])=>[id,[...a]]),physics:runtime.physics.debugPoints()}; }
async function gitFile(commit, path) {
  try {
    const object = async sha => { const buf=zlib.inflateSync(await fs.readFile(root+'/.git/objects/'+sha.slice(0,2)+'/'+sha.slice(2)));return buf.subarray(buf.indexOf(0)+1); };
    let sha=(await object(commit)).toString().match(/^tree ([a-f0-9]+)/)[1];
    for (const name of path.split('/')) {
      const tree=await object(sha);let p=0,found;
      while(p<tree.length){const e=tree.indexOf(0,p);const entry=tree.subarray(p,e).toString();if(entry.slice(entry.indexOf(' ')+1)===name)found=tree.subarray(e+1,e+21).toString('hex');p=e+21;}
      if(!found)throw Error('Missing Git tree path');sha=found;
    }
    return await object(sha);
  } catch { return execFileSync('git',['-C',root,'show',commit+':'+path],{maxBuffer:128*1024*1024}); }
}
export const checks = [
['native synchronous data factory renders and releases all GPU allocations without DOM', () => {
 const source=fixture(); const {runtime,gl}=make({},source); runtime.step(1/60); assert.equal(gl.uploads[0].localUri,'native://texture'); assert.ok(gl.draws.length); assert.equal(runtime.puppet,runtime);
 runtime.resize(200,100,2); assert.equal(runtime.renderer.canvas.width,400); assert.equal(runtime.renderer.canvas.height,200);
 runtime.dispose();runtime.dispose();assert.equal(gl.live.size,0);assert.equal(source.images.length,1);assert.throws(()=>runtime.step(1/60),/disposed/);
}],
['capabilities follow actual geometry rather than advertised names', () => {
 const {runtime}=make();assert.equal(runtime.capabilities.headYaw,false);assert.equal(runtime.capabilities.physics,false);assert.equal(runtime.capabilities.mouth,true);assert.equal(runtime.capabilities.gaze,false);assert.ok(!runtime.capabilities.parameters.includes('AngleX'));runtime.dispose();
}],
['seek backward, repeat, fps change, parameter/gaze/expression controls and pause are deterministic', () => {
 const {runtime}=make();runtime.frame(0);assert.equal(runtime.params.values.Wind,0);runtime.setExpression('happy');runtime.setGaze(0.4,-0.2);runtime.setParameters({AngleZ:12});runtime.frame(2);const expected=snapshot(runtime);runtime.frame(2);assert.deepEqual(snapshot(runtime),expected);runtime.frame(0.3);runtime.frame(2);assert.deepEqual(snapshot(runtime),expected);
 runtime.frame(2,30);runtime.frame(2,60);assert.deepEqual(snapshot(runtime),expected);runtime.pause();const time=runtime.time;runtime.step(0.5);runtime.frame(4);assert.equal(runtime.time,time);runtime.resume();runtime.frame(2);assert.deepEqual(snapshot(runtime),expected);runtime.setView('full');assert.throws(()=>runtime.setView('bad'));assert.throws(()=>runtime.step(-1));assert.throws(()=>runtime.frame(NaN));runtime.dispose();
}],
['view zoom scaling keeps compact host framing separate from model view data', () => {
 const source=fixture();source.views.face={x:50,y:35,zoom:3};const {runtime}=make({},source);runtime.setView('face',{zoomScale:0.72});assert.equal(runtime.renderer.camera.zoom,2.16);assert.equal(source.views.face.zoom,3);assert.throws(()=>runtime.setView('face',{zoomScale:0}));assert.throws(()=>runtime.setView('face',{zoomScale:NaN}));runtime.dispose();
}],
['repeated expression input does not restart pending blink-before-swap', () => {
 for (const coreRef of ['canonical', BASELINE_CORE_REF]) {
  const {runtime,gl}=make({coreRef});
  for(let i=0;i<90;i++) { runtime.setExpression('happy'); runtime.step(1/60); }
  assert.ok(runtime.params.values.MouthHappy > 0.9, coreRef + ': repeated host input completes the expression');
  runtime.setExpression('neutral');
  for(let i=0;i<90;i++) { runtime.setExpression('neutral'); runtime.step(1/60); }
  assert.ok(runtime.params.values.MouthHappy < 0.1, coreRef + ': repeated neutral input completes the return');
  runtime.dispose();assert.equal(gl.live.size,0);
 }
}],
['external speech suppresses real expression mouth art/open/form before animation and closes on stop', () => {
 const {runtime}=make();runtime.setExpression('happy');runtime.frame(1);assert.ok(runtime.params.values.MouthHappy>0.9);runtime.setMouth({open:0.2,form:-0.4,speaking:true});runtime.step(1/60);assert.equal(runtime.params.values.MouthHappy,0);assert.equal(runtime.params.values.MouthOpenY,0.2);assert.equal(runtime.params.values.MouthForm,-0.4);assert.equal(runtime.deformer.opacity.get('mouth-happy'),0);assert.equal(runtime.animator.lip.fake,null);
 runtime.setConversation('idle');runtime.step(1/60);assert.equal(runtime.params.values.MouthOpenY,0);assert.ok(runtime.params.values.MouthHappy>0.9);runtime.setConversation('speaking');runtime.step(1/60);assert.equal(runtime.params.values.MouthOpenY,0);runtime.dispose();
}],
['partial shader/link/texture/buffer/allocation failures leave no GPU resources', () => {
 for(const failure of [{shader:true},{link:true},{location:true},{upload:true},{buffer:true},{allocation:2},{allocation:5},{allocation:8},{allocation:10}]) {
  const gl=mockGL(failure);assert.throws(()=>RigRuntime.fromData(canvas(),fixture(),{gl}));assert.equal(gl.live.size,0,JSON.stringify(failure));
 }
}],
['renderer setModel releases old model buffers and textures on reload', () => {
 const gl=mockGL(),renderer=new Renderer(canvas(),{gl});const model=decodeModel(fixture());renderer.setModel(model);const live=gl.live.size;renderer.setModel(model);assert.equal(gl.live.size,live);renderer.dispose();assert.equal(gl.live.size,0);
}],
['injected fetch and loadImage apply declared settings and own images only for async create', async () => {
 let closed=0;const urls=[];const gl=mockGL();const runtime=await RigRuntime.create(canvas(),'https://rig/model.json',{gl,fetch:async url=>{urls.push(url);return {ok:true,json:async()=>url.endsWith('settings.json')?{rig:{head:{bob:9}}}:fixture()};},loadImage:async()=>({localUri:'file://atlas',close(){closed++;}}),settingsUrl:'https://rig/settings.json'});
 assert.deepEqual(urls,['https://rig/model.json','https://rig/settings.json']);assert.equal(runtime.model.rig.head.bob,9);runtime.dispose();assert.equal(closed,1);assert.equal(gl.live.size,0);
}],
['image failure closes earlier successful images; explicit settings failure rejects and cleans up', async () => {
 let closed=0;const source=fixture();source.atlases.push({file:'atlas2.png'});let i=0;
 await assert.rejects(RigRuntime.create(canvas(),'model.json',{fetch:async()=>({ok:true,json:async()=>source}),loadImage:async()=>{if(i++)throw Error('image failure');return {close(){closed++;}};},gl:mockGL()}),/image failure/);assert.equal(closed,1);
 await assert.rejects(RigRuntime.create(canvas(),'model.json',{fetch:async url=>({ok:url!=='settings.json',status:404,json:async()=>fixture()}),loadImage:async()=>({close(){closed++;}}),settingsUrl:'settings.json',gl:mockGL()}),/settings/);assert.equal(closed,2);
}],
['abort rejects immediately and releases a late image from a cancellation-ignoring loader', async () => {
 const ac=new AbortController();let finish,closed=0;const gl=mockGL();let entered;const started=new Promise(r=>entered=r);
 const pending=RigRuntime.create(canvas(),'model.json',{signal:ac.signal,gl,fetch:async()=>({ok:true,json:async()=>fixture()}),loadImage:()=>{entered();return new Promise(r=>finish=r);}});
 await started;ac.abort();await assert.rejects(pending,e=>e.name==='AbortError');finish({close(){closed++;}});await new Promise(r=>setTimeout(r,0));assert.equal(closed,1);assert.equal(gl.live.size,0);
}],
['validation rejects unsafe keys, invalid binary arrays, references, indices, UV and duplicate ids', () => {
 const mutations=[m=>m.drawables[0].indices=[0,1,9],m=>m.drawables[0].uvs=[0,0,2,0,0,1],m=>m.drawables[0].attrs.head=[1],m=>m.drawables[0].clip=['missing'],m=>m.drawables[1].id='mouth',m=>m.drawables[0].positions={dtype:'f32',b64:'AA=='},m=>m.drawables[0].positions={dtype:'f32',b64:'AAAAfwAAAAAAAAB/AAAAAA=='},m=>m.atlases[0].file='../x.png'];
 for(const mutate of mutations){const m=fixture();mutate(m);assert.throws(()=>decodeModel(m));}assert.throws(()=>applySettings(decodeModel(fixture()),JSON.parse('{"rig":{"__proto__":{"polluted":1}}}')),/Unsafe/);assert.equal({}.polluted,undefined);
}],
['abort during synchronous GPU upload also releases the completed host and owned image exactly once', async () => {
 const signal=new AbortController();let closed=0;const gl=mockGL();gl.texImage2D=()=>signal.abort();
 await assert.rejects(RigRuntime.create(canvas(),'model.json',{gl,signal:signal.signal,fetch:async()=>({ok:true,json:async()=>fixture()}),loadImage:async()=>({close(){closed++;}})}),e=>e.name==='AbortError');assert.equal(closed,1);assert.equal(gl.live.size,0);
}],
['unsupported versions and conflicting core refs reject before fetch', async () => {
 let calls=0;for(const options of [{manifest:{runtime:{abi:2,version:'0.1.0'}}},{coreRef:'wrong'},{coreRef:'canonical',manifest:{runtime:{abi:1,version:'0.1.0',core_ref:BASELINE_CORE_REF}}}])await assert.rejects(RigRuntime.create(canvas(),'model.json',{...options,fetch:()=>{calls++;throw Error('must not fetch');}}));assert.equal(calls,0);
}],
['standalone Puppet and canonical runtime share identical motion/deformation behavior', () => {
 const a=make({},fixture(),Puppet),b=make();for(let i=0;i<60;i++){a.runtime.step(1/60);b.runtime.step(1/60);}assert.deepEqual(snapshot(a.runtime),snapshot(b.runtime));a.runtime.dispose();b.runtime.dispose();
}],
['pinned baseline engine files exactly match Git and real approved model matches historical engine over motion/physics replay', async (testCtx) => {
 if (!hasBaselineHistory) { testCtx.skip('historical baseline Git object is unavailable in a fresh public checkout'); return; }
 for(const name of ['params.js','motion.js','deform.js','physics.js'])assert.deepEqual(await fs.readFile(root+'/web/js/runtime-baseline/'+name),await gitFile(BASELINE_CORE_REF,'web/js/core/'+name));
 const json=JSON.parse((await gitFile(BASELINE_CORE_REF,'web/models/aoi/model.json')).toString());const model=decodeModel(json);model.images=model.atlases.map(()=>({localUri:'baseline://atlas'}));const gl=mockGL();const runtime=RigRuntime.fromData(canvas(),model,{gl,coreRef:BASELINE_CORE_REF,profile:'comparison-v1'});
 const oldModel=decodeModel(json),params=new Params(),deformer=new Deformer(oldModel),physics=new Physics(oldModel,deformer.tf),animator=new Animator(params,oldModel,3);animator.manual.Wind=0;let t=0;
 for(let i=0;i<120;i++){t+=1/60;const P=animator.update(t,1/60).values;physics.update(P,1/60);deformer.update(P,physics);runtime.step(1/60);}
 assert.deepEqual(runtime.params.values,params.values);assert.deepEqual(runtime.physics.debugPoints(),physics.debugPoints());for(const [id,a] of deformer.out)assert.deepEqual(runtime.deformer.out.get(id),a,id);
 const expected=snapshot(runtime);runtime.frame(0.5);runtime.frame(2);assert.deepEqual(snapshot(runtime),expected);assert.equal(runtime.capabilities.coreRef,BASELINE_CORE_REF);assert.equal(runtime.capabilities.headYaw,false);assert.equal(runtime.capabilities.physics,true);assert.ok(gl.draws.length>100);runtime.dispose();assert.equal(gl.live.size,0);
}],
['conversation profile carries the breeze and the wind blowing; comparison profile and the baseline core have no wind', () => {
 const {runtime}=make({profile:{id:'conversation-v1',wind:0.35,windBlow:0.7}});runtime.step(1/60);
 assert.equal(runtime.params.values.Wind,0.35);assert.equal(runtime.params.values.WindBlow,0.7);runtime.dispose();
 const plain=make({profile:'conversation-v1'});plain.runtime.step(1/60);assert.equal(plain.runtime.params.values.WindBlow,0);plain.runtime.dispose();
 for(const profile of [{id:'comparison-v1',windBlow:0.5},{id:'comparison-v1',wind:0.35},{id:'conversation-v1',windBlow:2},{id:'conversation-v1',windBlow:NaN}])assert.throws(()=>make({profile}),/wind|bounds|finite|numeric/);
 assert.throws(()=>make({coreRef:BASELINE_CORE_REF,profile:{id:'conversation-v1',windBlow:0.5}}),/WindBlow/);
}],
];
for (const [name, run] of checks) test(name, run);


function vowelFixture() {
  const model = fixture();
  const closed = [{param:'MouthOpenY',keys:[[0.179,1],[0.18,0]]}];
  const open = {param:'MouthOpenY',keys:[[0.179,0],[0.18,1]]};
  const legacy = {param:'MouthVowel',keys:[[0,1],[1,0]]};
  model.expressions = [{id:'neutral',params:{}}];
  model.drawables = [drawable('mouth', closed), drawable('mouth-mouth_open',[open,legacy])];
  for (const [i,vowel] of ['a','i','u','e','o'].entries()) {
    model.drawables.push(drawable('mouth-vowel_'+vowel,[open,{param:'MouthVowel',keys:[[i,0],[i+1,1],[i+2,0]]}]));
  }
  return model;
}
test('vowel keys select exactly one shape, close, seek and reset to legacy', () => {
  const {runtime:r} = make({},vowelFixture());
  assert.deepEqual(r.capabilities.mouthVowels,['a','i','u','e','o']);
  const visible = () => [...r.deformer.opacity].filter(([id,alpha])=>alpha > 0.5).map(([id])=>id);
  for(const [i,vowel] of ['a','i','u','e','o'].entries()) {
    r.setMouth({open:1,speaking:true,vowel});r.step(1/30);
    assert.equal(r.params.get('MouthVowel'),i+1);
    assert.deepEqual(visible(),['mouth-vowel_'+vowel]);
    r.frame(2);assert.deepEqual(visible(),['mouth-vowel_'+vowel]);
    r.frame(0.5);assert.deepEqual(visible(),['mouth-vowel_'+vowel]);
    r.frame(0);assert.deepEqual(visible(),['mouth-vowel_'+vowel]);
    r.setMouth({open:0,speaking:false,vowel});r.step(1/30);
    assert.deepEqual(visible(),['mouth']);
  }
  r.setMouth({open:1,speaking:true});r.step(1/30);
  assert.equal(r.params.get('MouthVowel'),0);assert.deepEqual(visible(),['mouth-mouth_open']);
  r.dispose();
});
test('vowel input rejects unknown or unavailable artwork without mutating controls', () => {
  const {runtime:r} = make({},vowelFixture());
  r.setMouth({open:1,speaking:true,vowel:'a'});
  assert.throws(()=>r.setMouth({open:1,speaking:true,vowel:'x'}),/Unknown mouth vowel/);
  r.step(1/30);assert.equal(r.params.get('MouthVowel'),1);r.dispose();
  for (const coreRef of ['canonical',BASELINE_CORE_REF]) {
    const {runtime:old} = make({coreRef});
    assert.deepEqual(old.capabilities.mouthVowels,[]);
    assert.ok(!old.capabilities.parameters.includes('MouthVowel'));
    assert.throws(()=>old.setMouth({open:1,speaking:true,vowel:'i'}),/does not support/);
    old.setMouth({open:0.7,speaking:true});old.step(1/30);assert.equal(old.params.get('MouthOpenY'),0.7);old.dispose();
  }
  const broken = vowelFixture();broken.drawables[0].opacity=[];
  const {runtime:b} = make({},broken);assert.deepEqual(b.capabilities.mouthVowels,[]);b.dispose();
  const noClosed=vowelFixture();noClosed.drawables=noClosed.drawables.filter(d=>d.id!=='mouth');
  const {runtime:c}=make({},noClosed);assert.deepEqual(c.capabilities.mouthVowels,[]);c.dispose();
});
test('vowel parameter is categorical, with no interpolated double image', () => {
  const {runtime:r}=make({},vowelFixture());
  for(let n=0;n<=5;n+=0.125){r.params.set('MouthVowel',n);assert.equal(r.params.get('MouthVowel'),Math.round(n));}
  r.dispose();
});

test('vowel recipe preserves closed expression mouths and surprise override at opening boundaries', async () => {
  const { evalOpacity } = await import('../../web/js/core/deform.js');
  const { Params } = await import('../../web/js/core/params.js');
  const recipe = JSON.parse(await fs.readFile(root+'/recipes/aoi.json','utf8'));
  const term = o => Array.isArray(o) ? {param:o[0],keys:o[1]} : o;
  const old = {mouth:recipe.layers.mouth.opacity.map(term)};
  for(const [id,spec] of Object.entries(recipe.expressions.mouth))old['mouth-'+id]=spec.opacity.map(term);
  const next=structuredClone(old);
  for(const id of ['mouth-mouth_half','mouth-mouth_open'])next[id].push({param:'MouthVowel',keys:[[0,1],[1,0]]});
  for(const [i,v] of ['a','i','u','e','o'].entries()) next['mouth-vowel_'+v]=[
    {param:'MouthVowel',keys:[[i,0],[i+1,1],[i+2,0]]},
    {param:'MouthOpenY',keys:[[0.179,0],[0.18,1]]},
    {oneMinusSum:['MouthSurprised'],lo:0.499,hi:0.5},
  ];
  const evaluate=(spec,P)=>Object.fromEntries(Object.entries(spec).map(([id,terms])=>[id,evalOpacity(terms,P)]));
  const sum=ops=>Object.values(ops).reduce((a,b)=>a+b,0);
  for(const expression of [null,'Happy','Shy','Worried','Surprised']) {
    for(const open of [0,0.17899,0.179,0.17925,0.1795,0.17975,0.18,0.18001,0.529,0.5295,0.53,1]) {
      for(let vowel=0;vowel<=5;vowel++) {
        const P={MouthOpenY:open,MouthVowel:vowel,...(expression?{['Mouth'+expression]:1}:{})};
        const before=evaluate(old,P),after=evaluate(next,P);
        assert.ok(Math.abs(sum(after)-1)<1e-8,JSON.stringify({expression,open,vowel,after}));
        assert.ok(Math.abs(sum(after)-sum(before))<1e-8);
        if(vowel===0)for(const id of Object.keys(old))assert.equal(after[id],before[id]);
        if(expression==='Surprised') assert.equal(after['mouth-surprised'],1);
        if(open<=0.179 && expression!=='Surprised')assert.equal(after[expression?'mouth-'+expression.toLowerCase():'mouth'],1);
        if(vowel>0 && open>=0.18 && expression!=='Surprised'){
          const visible=Object.entries(after).filter(([,a])=>a>1e-8).map(([id])=>id);
          assert.deepEqual(visible,['mouth-vowel_'+['a','i','u','e','o'][vowel-1]]);
        }
      }
    }
  }
  // Rapid or interpolated vowel selection remains a single discrete open art.
  const params=new Params();params.set('MouthOpenY',1);
  for(let value=0;value<=5;value+=0.01){params.set('MouthVowel',value);const ops=evaluate(next,params.values);assert.equal(Object.values(ops).filter(a=>a>0).length,1);}
});
test('external vowel input suppresses expression mouth until speech stops', () => {
  const model=vowelFixture();
  model.expressions.push({id:'surprised',params:{MouthSurprised:1}});
  model.drawables.push(drawable('mouth-surprised',[{param:'MouthSurprised',keys:[[0.499,0],[0.5,1]]}]));
  const noSurprise={oneMinusSum:['MouthSurprised'],lo:0.499,hi:0.5};
  for(const d of model.drawables)if(d.id!=='mouth-surprised')d.opacity.push(noSurprise);
  const {runtime:r}=make({},model);
  r.setExpression('surprised');r.frame(2);
  assert.equal(r.deformer.opacity.get('mouth-surprised'),1);
  r.setMouth({open:0.18,speaking:true,vowel:'e'});r.frame(2);
  assert.equal(r.deformer.opacity.get('mouth-vowel_e'),1);assert.equal(r.deformer.opacity.get('mouth-surprised'),0);
  r.setMouth({open:0,speaking:false});r.frame(2);
  assert.equal(r.deformer.opacity.get('mouth-vowel_e'),0);assert.equal(r.deformer.opacity.get('mouth-surprised'),1);
  r.dispose();
});

// Exercise the actual deformer, not just the sum of recipe opacity weights.
test('authored vowels show exactly one opaque mouth throughout the opening ramp', () => {
  const {runtime:r}=make({},vowelFixture());
  for (const vowel of ['a','i','u','e','o']) {
    for (const open of [0,.17899,.179,.17925,.1795,.17975,.179999,.18,.18001,.5295,1]) {
      r.setMouth({open,speaking:true,vowel});r.step(1/60);
      const visible=[...r.deformer.opacity].filter(([id,a]) => (id==='mouth'||id.startsWith('mouth-'))&&a>0);
      assert.deepEqual(visible,[[open<.18?'mouth':'mouth-vowel_'+vowel,1]],JSON.stringify({open,vowel}));
      assert.equal(r.params.get('MouthOpenY'),open);
    }
  }
  r.setMouth({open:.1795,speaking:true});r.step(1/60);
  assert.ok(r.deformer.opacity.get('mouth')>0 && r.deformer.opacity.get('mouth')<1,'legacy interpolation preserved');
  r.dispose();
});
