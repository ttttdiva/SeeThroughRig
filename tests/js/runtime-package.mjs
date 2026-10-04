import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import { existsSync } from 'node:fs';
import crypto from 'node:crypto';
import path from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { RigRuntime as SourceRuntime, BASELINE_CORE_REF } from '../../web/js/runtime/index.js';
import { mockGL, fixture } from './runtime.test.mjs';
const root=fileURLToPath(new URL('../../',import.meta.url));
const directory=path.resolve(process.env.SEETHROUGH_RUNTIME_PACKAGE || root+'/.local/seethrough-integration-20261001/runtime-package-final');
export async function checkPackage() {
 const provenance=JSON.parse(await fs.readFile(directory+'/runtime-provenance.json','utf8'));
 const pkg=JSON.parse(await fs.readFile(directory+'/package.json','utf8'));
 assert.equal(pkg.name,'@aoitalk/seethrough-rig-runtime');assert.equal(pkg.version,'0.1.0');assert.equal(pkg.type,'module');assert.equal(pkg.license,'UNLICENSED');assert.equal(provenance.first_party_upstream_license,'unspecified');assert.deepEqual(provenance.third_party_dependencies,[]);
 const actual=await fs.readdir(directory,{recursive:true,withFileTypes:true});const files=actual.filter(f=>f.isFile()).map(f=>((f.parentPath || f.path)+'/'+f.name).replaceAll('\\','/').slice(directory.replaceAll('\\','/').length+1)).sort();
 assert.deepEqual(files,[...provenance.files.map(f=>f.path),'runtime-provenance.json'].sort());
 for(const file of provenance.files) {
  assert.ok(!/models|assets|inference|\.png|\.py$/.test(file.path));const data=await fs.readFile(directory+'/'+file.path);assert.equal(data.length,file.size);assert.equal(crypto.createHash('sha256').update(data).digest('hex'),file.sha256,file.path);
  if(file.path.startsWith('web/js/runtime-baseline/')) assert.equal(file.source_commit,BASELINE_CORE_REF);
  if(file.source==='working-tree') assert.equal(file.source_commit,null);
 }
 assert.equal(await fs.readFile(directory+'/index.d.ts','utf8'),await fs.readFile(root+'/web/js/runtime/index.d.ts','utf8'));
 const module=await import(pathToFileURL(directory+'/index.js').href);
 for(const coreRef of ['canonical',BASELINE_CORE_REF]) {
  const a=new SourceRuntime({width:100,height:100},fixture(),{gl:mockGL(),coreRef}), b=module.RigRuntime.fromData({width:100,height:100},fixture(),{gl:mockGL(),coreRef});
  a.setMouth({open:0.3,speaking:true});b.setMouth({open:0.3,speaking:true});a.frame(1);b.frame(1);assert.deepEqual(a.params.values,b.params.values);for(const [id,out] of a.deformer.out) assert.deepEqual([...out],[...b.deformer.out.get(id)]);a.dispose();b.dispose();
 }
}
test('generated package hashes, data-free sources, declarations and both engine APIs match canonical87', {skip:!existsSync(directory+'/runtime-provenance.json') ? 'Generate runtime package first; SEETHROUGH_RUNTIME_PACKAGE overrides scratch location' : false},checkPackage);
