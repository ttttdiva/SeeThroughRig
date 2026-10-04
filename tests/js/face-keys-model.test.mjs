import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import { decodeModel } from '../../web/js/core/model.js';
import { Deformer, evalOpacity } from '../../web/js/core/deform.js';
import { Params } from '../../web/js/core/params.js';
import { deriveCapabilities } from '../../web/js/runtime/index.js';

test('adopted model has real mouths exclusive at every opening boundary', async () => {
  const raw = await fs.readFile(new URL('../../web/models/aoi/model.json', import.meta.url));
  // The source checkout keeps the review candidate for provenance.  The
  // public projection intentionally ships one canonical model, so the byte
  // equality check is conditional on that optional candidate being present.
  try {
    const candidate = await fs.readFile(new URL('../../web/models/aoi_vowels/model.json', import.meta.url));
    assert.deepEqual(raw, candidate);
  } catch (error) {
    assert.equal(error?.code, 'ENOENT');
  }
  const model = decodeModel(JSON.parse(raw));
  assert.deepEqual(deriveCapabilities(model).mouthVowels, ['a','i','u','e','o']);
  const deformer = new Deformer(model), params = new Params();
  for (const vowel of [0,1,2,3,4,5]) {
    for (const open of [0,.179,.17925,.1795,.17975,.179999,.18,.180001,.5295,1]) {
      params.set('MouthVowel',vowel); params.set('MouthOpenY',open);
      deformer.update(params.values);
      const mouths = model.drawables.filter(d => d.id==='mouth' || d.id.startsWith('mouth-'));
      if (vowel===0) {
        for (const d of mouths) assert.equal(deformer.opacity.get(d.id),evalOpacity(d.opacity,params.values));
      } else {
        const visible = mouths.filter(d => deformer.opacity.get(d.id)>0).map(d => [d.id,deformer.opacity.get(d.id)]);
        assert.deepEqual(visible,[[open<.18?'mouth':'mouth-vowel_'+['a','i','u','e','o'][vowel-1],1]]);
      }
      for (const positions of deformer.out.values()) assert.ok(positions.every(Number.isFinite));
    }
  }
});
