// WebGL2 renderer: textured triangle meshes with premultiplied alpha and
// stencil-based clipping masks (used for irises clipped by the eye whites).

const VS = `#version 300 es
in vec2 a_pos;
in vec2 a_uv;
uniform vec4 u_view;   // scale.xy, offset.xy : model px -> clip
out vec2 v_uv;
void main() {
  v_uv = a_uv;
  gl_Position = vec4(a_pos * u_view.xy + u_view.zw, 0.0, 1.0);
}`;

const FS = `#version 300 es
precision mediump float;
in vec2 v_uv;
uniform sampler2D u_tex;
uniform float u_opacity;
uniform float u_alphaCut;   // >0: mask pass, discard low alpha
uniform vec4 u_tint;        // rgb multiply, a = mix amount (for highlighting)
out vec4 o;
void main() {
  vec4 c = texture(u_tex, v_uv);
  if (u_alphaCut > 0.0 && c.a < u_alphaCut) discard;
  c.rgb = mix(c.rgb, u_tint.rgb * c.a, u_tint.a);
  o = c * u_opacity;
}`;

const LINE_FS = `#version 300 es
precision mediump float;
uniform vec4 u_color;
out vec4 o;
void main() { o = u_color; }`;

// Normalize native straight-alpha uploads before any interpolation/mipmapping.
// Multiplying a filtered sample instead produces dark seams when a PNG decoder
// clears RGB in transparent texels (the hair bundles deliberately overlap).
const PREMULTIPLY_VS = `#version 300 es
void main() {
  vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
  gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}`;
const PREMULTIPLY_FS = `#version 300 es
precision highp float;
precision highp int;
uniform highp sampler2D u_tex;
out vec4 o;
void main() {
  vec4 c = texelFetch(u_tex, ivec2(gl_FragCoord.xy), 0);
  o = vec4(c.rgb * c.a, c.a);
}`;

function compile(gl, type, src) {
  const s = gl.createShader(type);
  if (!s) throw new Error('WebGL shader allocation failed');
  try {
    gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) || 'GLSL 300 es compilation failed');
    return s;
  } catch (e) { gl.deleteShader(s); throw e; }
}
function program(gl, vs, fs) {
  const p = gl.createProgram();
  if (!p) throw new Error('WebGL program allocation failed');
  const shaders = [];
  try {
    shaders.push(compile(gl, gl.VERTEX_SHADER, vs)); shaders.push(compile(gl, gl.FRAGMENT_SHADER, fs));
    for (const s of shaders) gl.attachShader(p, s);
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p) || 'WebGL link failed');
    return p;
  } catch (e) { gl.deleteProgram(p); throw e; }
  finally { for (const s of shaders) { try { gl.detachShader?.(p, s); } catch { /* failed program may already be deleted */ } gl.deleteShader(s); } }
}

export class Renderer {
  constructor(canvas, { preserveDrawingBuffer = false, alpha = true, gl: injectedGL = null, pixelRatio = globalThis.devicePixelRatio || 1 } = {}) {
    this.canvas = canvas;
    const gl = injectedGL || canvas.getContext?.('webgl2', { stencil: true, premultipliedAlpha: true, alpha, antialias: true, preserveDrawingBuffer });
    if (!gl || !gl.createVertexArray) throw new Error('WebGL2-equivalent context is required');
    if (!Number.isFinite(pixelRatio) || pixelRatio <= 0) throw new Error('Invalid pixelRatio');
    this.pixelRatio = pixelRatio;
    this._buffers = []; this._vaos = []; this.textures = []; this.meshes = new Map();
    this.disposed = false;
    this.gl = gl;
    try {
      this.prog = program(gl, VS, FS);
      this.lineProg = program(gl, VS, LINE_FS);
    } catch (e) { this.dispose(); throw e; }
    try {
    this.loc = {
      pos: gl.getAttribLocation(this.prog, 'a_pos'),
      uv: gl.getAttribLocation(this.prog, 'a_uv'),
      view: gl.getUniformLocation(this.prog, 'u_view'),
      tex: gl.getUniformLocation(this.prog, 'u_tex'),
      opacity: gl.getUniformLocation(this.prog, 'u_opacity'),
      alphaCut: gl.getUniformLocation(this.prog, 'u_alphaCut'),
      tint: gl.getUniformLocation(this.prog, 'u_tint'),
    };
    this.lineLoc = {
      pos: gl.getAttribLocation(this.lineProg, 'a_pos'),
      view: gl.getUniformLocation(this.lineProg, 'u_view'),
      color: gl.getUniformLocation(this.lineProg, 'u_color'),
    };
    this.meshes = new Map();
    this.textures = [];
    this.background = [0, 0, 0, 0];
    this.camera = { x: 0, y: 0, zoom: 1 };
    } catch (e) { this.dispose(); throw e; }
  }

  setModel(model) {
    if (this.disposed) throw new Error('Renderer disposed');
    const gl = this.gl;
    this.releaseModel();
    this.model = model;
    try {
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, true);
    // Browsers premultiply DOM image uploads, but Expo GL's localUri loader
    // returns straight-alpha pixels and does not implement this unpack flag.
    // Probe the resulting state rather than assuming the request took effect.
    // Both paths must reach the premultiplied blend before texture filtering.
    const straightAlpha = gl.getParameter(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL) === false;
    for (const [i, img] of model.images.entries()) {
      const t = this.allocate('Texture');
      gl.bindTexture(gl.TEXTURE_2D, t);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img);
      if (straightAlpha) this.premultiplyTexture(t, model.atlases?.[i]?.width ?? img.width, model.atlases?.[i]?.height ?? img.height);
      gl.bindTexture(gl.TEXTURE_2D, t);
      gl.generateMipmap(gl.TEXTURE_2D);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    }
    for (const d of model.drawables) {
      const vao = this.allocate('VertexArray');
      gl.bindVertexArray(vao);
      const pos = this.allocate('Buffer');
      gl.bindBuffer(gl.ARRAY_BUFFER, pos);
      gl.bufferData(gl.ARRAY_BUFFER, d.positions, gl.DYNAMIC_DRAW);
      gl.enableVertexAttribArray(this.loc.pos);
      gl.vertexAttribPointer(this.loc.pos, 2, gl.FLOAT, false, 0, 0);
      const uv = this.allocate('Buffer');
      gl.bindBuffer(gl.ARRAY_BUFFER, uv);
      gl.bufferData(gl.ARRAY_BUFFER, d.uvs, gl.STATIC_DRAW);
      gl.enableVertexAttribArray(this.loc.uv);
      gl.vertexAttribPointer(this.loc.uv, 2, gl.FLOAT, false, 0, 0);
      const idx = this.allocate('Buffer');
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, idx);
      const indices = d.indices instanceof Uint32Array ? d.indices : new Uint32Array(d.indices);
      gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, indices, gl.STATIC_DRAW);
      gl.bindVertexArray(null);
      // wireframe edges (bound only while drawing the overlay)
      const edges = [];
      for (let i = 0; i < indices.length; i += 3) {
        const [a, b, c] = [indices[i], indices[i + 1], indices[i + 2]];
        edges.push(a, b, b, c, c, a);
      }
      const eb = this.allocate('Buffer');
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, eb);
      gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, new Uint32Array(edges), gl.STATIC_DRAW);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, null);
      this.meshes.set(d.id, { vao, pos, uv, idx, count: indices.length, edges: eb, edgeCount: edges.length });
    }
    } catch (e) { this.releaseModel(); throw e; }
  }

  premultiplyTexture(source, width, height) {
    const gl = this.gl;
    const limit = gl.getParameter(gl.MAX_TEXTURE_SIZE);
    if (![width, height].every(n => Number.isInteger(n) && n > 0 && n <= limit)) throw new Error('Native atlas requires valid texture dimensions');
    const viewport = gl.getParameter(gl.VIEWPORT);
    const previousProgram = gl.getParameter(gl.CURRENT_PROGRAM);
    const activeTexture = gl.getParameter(gl.ACTIVE_TEXTURE);
    const colorMask = Array.from(gl.getParameter(gl.COLOR_WRITEMASK), Boolean);
    const flags = [gl.BLEND, gl.STENCIL_TEST, gl.DEPTH_TEST, gl.CULL_FACE, gl.SCISSOR_TEST];
    const enabled = flags.map(flag => gl.isEnabled(flag));
    let target, framebuffer, vao, prog;
    try {
      prog = program(gl, PREMULTIPLY_VS, PREMULTIPLY_FS);
      target = gl.createTexture(); framebuffer = gl.createFramebuffer(); vao = gl.createVertexArray();
      if (!target || !framebuffer || !vao) throw new Error('Alpha normalization allocation failed');
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, source);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
      gl.bindTexture(gl.TEXTURE_2D, target);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, width, height, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
      gl.bindFramebuffer(gl.FRAMEBUFFER, framebuffer);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, target, 0);
      if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE) throw new Error('Alpha normalization framebuffer incomplete');
      for (const flag of flags) gl.disable(flag);
      gl.colorMask(true, true, true, true);
      gl.viewport(0, 0, width, height);
      gl.useProgram(prog);
      gl.uniform1i(gl.getUniformLocation(prog, 'u_tex'), 0);
      gl.bindTexture(gl.TEXTURE_2D, source);
      gl.bindVertexArray(vao);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      // Read from the separate target; never sample a framebuffer attachment.
      // Keep the original texture identity and atlas indexing owned by setModel.
      gl.copyTexSubImage2D(gl.TEXTURE_2D, 0, 0, 0, 0, 0, width, height);
    } finally {
      // Renderer owns the canvas/default target and leaves VAO unbound. Expo GL
      // throws for framebuffer/VAO/texture binding queries, so do not query them.
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      gl.bindVertexArray(null);
      gl.useProgram(previousProgram);
      gl.viewport(...viewport);
      gl.colorMask(...colorMask);
      flags.forEach((flag, i) => enabled[i] ? gl.enable(flag) : gl.disable(flag));
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, source);
      gl.activeTexture(activeTexture);
      if (vao) gl.deleteVertexArray(vao);
      if (framebuffer) gl.deleteFramebuffer(framebuffer);
      if (target) gl.deleteTexture(target);
      if (prog) gl.deleteProgram(prog);
    }
  }

  allocate(kind) {
    const value = this.gl['create' + kind]();
    if (!value) throw new Error('GL allocation failed: ' + kind);
    (kind === 'Texture' ? this.textures : kind === 'Buffer' ? this._buffers : this._vaos).push(value);
    return value;
  }
  releaseModel() {
    const gl = this.gl;
    for (const [items, method] of [[this.textures, 'deleteTexture'], [this._buffers, 'deleteBuffer'], [this._vaos, 'deleteVertexArray']]) {
      for (const item of items || []) { try { gl[method](item); } catch { /* finish cleanup */ } }
      if (items) items.length = 0;
    }
    this.meshes?.clear(); this.model = null;
  }
  dispose() {
    if (this.disposed) return;
    this.disposed = true; this.releaseModel();
    for (const p of [this.prog, this.lineProg]) if (p) { try { this.gl.deleteProgram(p); } catch { /* finish cleanup */ } }
    this.prog = this.lineProg = null;
  }

  resize(width, height, pixelRatio = this.pixelRatio) {
    if (!Number.isFinite(pixelRatio) || pixelRatio <= 0) throw new Error('Invalid pixelRatio');
    this.pixelRatio = pixelRatio;
    const dpr = pixelRatio;
    const w = Math.max(1, Math.round((width ?? this.canvas.clientWidth ?? this.canvas.width / dpr) * dpr));
    const h = Math.max(1, Math.round((height ?? this.canvas.clientHeight ?? this.canvas.height / dpr) * dpr));
    if (!Number.isFinite(w) || !Number.isFinite(h)) throw new Error('Invalid canvas dimensions');
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w;
      this.canvas.height = h;
    }
  }

  // camera: model-space centre (x, y) and zoom = canvas px per model px (relative to fit)
  viewVec() {
    const { width: W, height: H } = this.canvas;
    const m = this.model.canvas;
    const fit = Math.min(W / m.width, H / m.height);
    const s = fit * this.camera.zoom;
    const cx = this.camera.x || m.width / 2;
    const cy = this.camera.y || m.height / 2;
    return [(2 * s) / W, (-2 * s) / H, (-2 * s * cx) / W, (2 * s * cy) / H];
  }

  screenToModel(px, py) {
    const [sx, sy, ox, oy] = this.viewVec();
    const dpr = this.pixelRatio;
    const cxp = ((px * dpr) / this.canvas.width) * 2 - 1;
    const cyp = 1 - ((py * dpr) / this.canvas.height) * 2;
    return [(cxp - ox) / sx, (cyp - oy) / sy];
  }

  render(deformer, { hidden = new Set(), highlight = null, wireframe = false } = {}) {
    if (this.disposed) throw new Error('Renderer disposed');
    const gl = this.gl;
    gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    gl.clearColor(...this.background);
    gl.clearStencil(0);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.STENCIL_BUFFER_BIT);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.useProgram(this.prog);
    const view = this.viewVec();
    gl.uniform4fv(this.loc.view, view);
    gl.uniform1i(this.loc.tex, 0);
    gl.activeTexture(gl.TEXTURE0);

    const model = this.model;
    for (const d of model.drawables) {
      const mesh = this.meshes.get(d.id);
      gl.bindBuffer(gl.ARRAY_BUFFER, mesh.pos);
      gl.bufferSubData(gl.ARRAY_BUFFER, 0, deformer.out.get(d.id));
    }

    for (const d of model.drawables) {
      if (d.isMask || hidden.has(d.id)) continue;
      const op = deformer.opacity.get(d.id) ?? 1;
      if (op <= 0.002) continue;
      if (d.clip && d.clip.length) {
        gl.enable(gl.STENCIL_TEST);
        gl.clear(gl.STENCIL_BUFFER_BIT);
        gl.colorMask(false, false, false, false);
        gl.stencilFunc(gl.ALWAYS, 1, 0xff);
        gl.stencilOp(gl.KEEP, gl.KEEP, gl.REPLACE);
        gl.uniform1f(this.loc.alphaCut, 0.5);
        for (const mid of d.clip) {
          const md = model.drawableById[mid];
          if (md) this.drawMesh(md, 1);
        }
        gl.colorMask(true, true, true, true);
        gl.stencilFunc(gl.EQUAL, 1, 0xff);
        gl.stencilOp(gl.KEEP, gl.KEEP, gl.KEEP);
        gl.uniform1f(this.loc.alphaCut, 0);
        this.drawMesh(d, op, highlight === d.id);
        gl.disable(gl.STENCIL_TEST);
      } else {
        gl.uniform1f(this.loc.alphaCut, 0);
        this.drawMesh(d, op, highlight === d.id);
      }
    }

    if (wireframe) {
      gl.useProgram(this.lineProg);
      gl.uniform4fv(this.lineLoc.view, view);
      for (const d of model.drawables) {
        if (hidden.has(d.id)) continue;
        if (wireframe !== true && wireframe !== d.id) continue;
        const op = deformer.opacity.get(d.id) ?? 1;
        if (op <= 0.002 && !d.isMask) continue;
        const mesh = this.meshes.get(d.id);
        gl.bindVertexArray(mesh.vao);
        gl.uniform4fv(this.lineLoc.color, highlight === d.id ? [1, 0.3, 0.2, 0.9] : [0.1, 0.5, 1.0, 0.35]);
        gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, mesh.edges);
        gl.drawElements(gl.LINES, mesh.edgeCount, gl.UNSIGNED_INT, 0);
        gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, mesh.idx);
      }
      gl.bindVertexArray(null);
    }
    gl.endFrameEXP?.();
  }

  drawMesh(d, opacity, highlighted = false) {
    const gl = this.gl;
    const mesh = this.meshes.get(d.id);
    gl.bindTexture(gl.TEXTURE_2D, this.textures[d.atlas]);
    gl.uniform1f(this.loc.opacity, opacity);
    gl.uniform4fv(this.loc.tint, highlighted ? [1, 0.55, 0.4, 0.45] : [1, 1, 1, 0]);
    gl.bindVertexArray(mesh.vao);
    gl.drawElements(gl.TRIANGLES, mesh.count, gl.UNSIGNED_INT, 0);
    gl.bindVertexArray(null);
  }
}
