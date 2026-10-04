import test from 'node:test';
import assert from 'node:assert/strict';
import { Renderer } from '../../web/js/core/renderer.js';

function alphaGL(uploadPremultiplies, failure = {}) {
  let serial = 0, requested = false, normalizing = false;
  const live = new Set(), events = [], shaders = [], constants = new Map();
  const state = {viewport:[7,9,100,80], program:null, active:null, mask:[true,false,true,false], enabled:new Set(), framebuffer:null, vao:null};
  const gl = new Proxy({live,events,shaders,state}, {get(target, name) {
    if (name in target) return target[name];
    if (String(name).toUpperCase() === name) {if (!constants.has(name)) constants.set(name,++serial);return constants.get(name);}
    if (name === 'pixelStorei') return (p,v) => {requested=v;events.push(['unpack',p,v]);};
    if (name === 'getParameter') return p => {
      if ([gl.FRAMEBUFFER_BINDING,gl.VERTEX_ARRAY_BINDING,gl.TEXTURE_BINDING_2D].includes(p)) throw Error('Expo unsupported binding query');
      if (p === gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL) return uploadPremultiplies && requested;
      if (p === gl.MAX_TEXTURE_SIZE) return 8192;
      if (p === gl.VIEWPORT) return [...state.viewport];
      if (p === gl.CURRENT_PROGRAM) return state.program;
      if (p === gl.ACTIVE_TEXTURE) return state.active;
      if (p === gl.COLOR_WRITEMASK) return [...state.mask];
      throw Error('Unexpected query '+p);
    };
    if (name === 'shaderSource') return (shader,source) => {shaders.push(source);shader.normalization=source.includes('texelFetch');if(shader.normalization)normalizing=true;};
    if (name === 'getShaderParameter') return shader => !(failure.shader && shader.normalization);
    if (name === 'getShaderInfoLog') return () => 'injected normalization shader failure';
    if (name === 'getProgramParameter') return () => true;
    if (name === 'getUniformLocation') return (_,key) => key;
    if (name === 'getAttribLocation') return () => 0;
    if (name === 'texImage2D') return (...args) => {normalizing=args.length===9;events.push([normalizing?'target':'upload']);if (normalizing&&failure.target) throw Error('injected target failure');};
    if (name === 'bindTexture') return (_,texture) => {events.push(['texture',texture]);};
    if (name === 'bindFramebuffer') return (_,v) => {state.framebuffer=v;events.push(['framebuffer',v]);};
    if (name === 'bindVertexArray') return v => {state.vao=v;};
    if (name === 'checkFramebufferStatus') return () => failure.framebuffer ? -1 : gl.FRAMEBUFFER_COMPLETE;
    if (name === 'useProgram') return v => {state.program=v;};
    if (name === 'activeTexture') return v => {state.active=v;};
    if (name === 'viewport') return (...v) => {state.viewport=v;};
    if (name === 'colorMask') return (...v) => {state.mask=v;};
    if (name === 'isEnabled') return flag => state.enabled.has(flag);
    if (name === 'enable') return flag => state.enabled.add(flag);
    if (name === 'disable') return flag => state.enabled.delete(flag);
    if (name === 'drawArrays') return () => {
      assert.ok(state.framebuffer);assert.ok(state.vao);assert.deepEqual(state.viewport,[0,0,64,32]);
      assert.deepEqual(state.mask,[true,true,true,true]);assert.equal(state.enabled.size,0);
      events.push(['normalize']);if(failure.draw)throw Error('injected draw failure');
    };
    if (name === 'copyTexSubImage2D') return () => {events.push(['copy']);if(failure.copy)throw Error('injected copy failure');};
    if (name === 'generateMipmap') return () => events.push(['mipmap']);
    if (String(name).startsWith('create')) return () => {
      if (failure.allocation === String(name).slice(6) && (normalizing || name === 'createFramebuffer')) return null;
      const item={id:++serial};live.add(item);return item;
    };
    if (String(name).startsWith('delete')) return item => {assert.equal(live.delete(item),true,'exactly one deletion');};
    return () => {};
  }});
  state.active=gl.TEXTURE0+3;state.enabled.add(gl.BLEND);state.enabled.add(gl.SCISSOR_TEST);
  return gl;
}
const model = () => ({canvas:{width:100,height:100},atlases:[{width:64,height:32}],images:[{localUri:'file://atlas.png'}],drawables:[],drawableById:{}});
const stateOf = gl => ({viewport:[...gl.state.viewport],program:gl.state.program,active:gl.state.active,mask:[...gl.state.mask],enabled:[...gl.state.enabled].sort(),framebuffer:gl.state.framebuffer,vao:gl.state.vao});

for (const uploadPremultiplies of [true,false]) {
  test(`alpha upload ${uploadPremultiplies?'honors':'ignores'} unpack: normalize before filtering, once per load`,()=>{
    const gl=alphaGL(uploadPremultiplies),r=new Renderer({width:100,height:100},{gl}),before=stateOf(gl);
    r.setModel(model());assert.deepEqual(stateOf(gl),before);
    const steps=gl.events.filter(e=>['upload','target','normalize','copy','mipmap'].includes(e[0])).map(e=>e[0]);
    assert.deepEqual(steps,uploadPremultiplies?['upload','mipmap']:['upload','target','normalize','copy','mipmap']);
    if(!uploadPremultiplies){const shader=gl.shaders.find(s=>s.includes('texelFetch'));assert.match(shader,/precision highp float/);assert.match(shader,/texelFetch\(u_tex, ivec2\(gl_FragCoord.xy\), 0\)/);assert.match(shader,/vec4\(c.rgb \* c.a, c.a\)/);}
    assert.ok(gl.shaders.filter(s=>s.includes('texture(u_tex')).every(s=>!s.includes('c.rgb *= c.a')),'no post-filter multiplication');
    const live=gl.live.size,count=gl.events.filter(e=>e[0]==='normalize').length;
    r.render({out:new Map(),opacity:new Map()});r.render({out:new Map(),opacity:new Map()});
    assert.equal(gl.events.filter(e=>e[0]==='normalize').length,count,'no per-frame normalization');
    r.setModel(model());assert.equal(gl.live.size,live);r.dispose();r.dispose();assert.equal(gl.live.size,0);
  });
}
for(const failure of [{shader:true},{allocation:'Texture'},{allocation:'Framebuffer'},{allocation:'VertexArray'},{target:true},{framebuffer:true},{draw:true},{copy:true}]){
  test('normalization failure restores owned canvas state and releases all allocations: '+JSON.stringify(failure),()=>{
    const gl=alphaGL(false,failure),r=new Renderer({width:100,height:100},{gl}),before=stateOf(gl);
    assert.throws(()=>r.setModel(model()));assert.deepEqual(stateOf(gl),before);
    assert.equal(r.textures.length,0);assert.equal(r.model,null);
    for(const key of Object.keys(failure))delete failure[key];
    r.setModel(model());r.dispose();assert.equal(gl.live.size,0);
  });
}
test('invalid native atlas dimensions fail before normalization allocation',()=>{
  for(const dimensions of [{},{width:0,height:32},{width:64.5,height:32},{width:9000,height:32}]){
    const gl=alphaGL(false),r=new Renderer({width:100,height:100},{gl});const m=model();m.atlases=[dimensions];
    assert.throws(()=>r.setModel(m),/dimensions/);assert.ok(!gl.events.some(e=>e[0]==='target'));r.dispose();assert.equal(gl.live.size,0);
  }
});
