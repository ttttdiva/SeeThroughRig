"""Face key registration and extraction tests; synthetic art, not visual QA."""
import copy
import json
from pathlib import Path
import numpy as np
import pytest
from PIL import Image, ImageDraw
from seethrough_rig.build import build_variants, load_recipe, opacity_term
from seethrough_rig.expressions import load_variants
from seethrough_rig.face_keys import VOWELS, with_vowel_keys
from seethrough_rig.layers import Layer

ROOT = Path(__file__).resolve().parents[1]


def test_registration_preserves_legacy_and_eye_keys():
    original = load_recipe(ROOT / 'recipes/aoi_rig.json')
    before = copy.deepcopy(original)
    out = with_vowel_keys(original, {v:f'mouth_{v}.png' for v in VOWELS})
    assert original == before
    assert out['expressions']['eyes'] == original['expressions']['eyes']
    assert out['layers'] == original['layers']
    assert out['rig'] == original['rig']
    for name in ('mouth_half','mouth_open'):
        assert out['expressions']['mouth'][name]['opacity'][:-1] == original['expressions']['mouth'][name]['opacity']
    for i,v in enumerate(VOWELS, 1):
        terms = out['expressions']['mouth']['vowel_'+v]['opacity']
        assert opacity_term(terms[0]) == {'param':'MouthVowel','keys':[[i-1,0],[i,1],[i+1,0]]}
    with pytest.raises(ValueError, match='already'):
        with_vowel_keys(out,{v:f'{v}.png' for v in VOWELS})


def test_registration_rejects_incomplete_or_invalid_assets(tmp_path):
    recipe={'layers':{'mouth':{}},'expressions':{'box':[0,0,64,64]}}
    with pytest.raises(ValueError, match='five'):
        with_vowel_keys(recipe,{'a':'a.png'})
    names={v:f'{v}.png' for v in VOWELS}
    with pytest.raises(FileNotFoundError):
        with_vowel_keys(recipe,names,tmp_path)
    for v in VOWELS: Image.new('RGB',(64,64),'white').save(tmp_path/names[v])
    with_vowel_keys(recipe,names,tmp_path)
    Image.new('RGB',(65,64),'white').save(tmp_path/'o.png')
    with pytest.raises(ValueError,match='identical'):
        with_vowel_keys(recipe,names,tmp_path)
    with pytest.raises(ValueError,match='identical'):
        load_variants([0,0,64,64],{v:str(tmp_path/names[v]) for v in VOWELS},np.ones((64,64,3),np.float32))


def rectangle(tag,box):
    x,y,w,h=box
    rgba=np.ones((h,w,4),np.float32)
    rgba[:,:,:3]=0
    return Layer(tag,rgba,(x,y),1)


def test_five_vowels_and_left_right_eye_keys_extract_nonempty_art(tmp_path):
    size=128
    src=np.ones((size,size,3),np.float32)
    layers={
        'mouth':rectangle('mouth',(57,80,14,4)),
        'eyewhite-l':rectangle('eyewhite-l',(35,40,15,10)),
        'eyewhite-r':rectangle('eyewhite-r',(78,40,15,10)),
    }
    paths={}
    for i,v in enumerate(VOWELS):
        im=Image.new('RGB',(size,size),'white');draw=ImageDraw.Draw(im)
        draw.ellipse((55-i,77-i,72+i,88+i),fill=(100,10+i*20,40))
        path=tmp_path/f'{v}.png';im.save(path);paths[v]=path.name
    for name,height in [('eye_half',3),('eye_closed',1)]:
        im=Image.new('RGB',(size,size),'white');draw=ImageDraw.Draw(im)
        for x in (35,78): draw.rectangle((x,45,x+15,45+height),fill='black')
        im.save(tmp_path/f'{name}.png')
    recipe={'layers':{'mouth':{}},'expressions':{'box':[0,0,size,size],
        'images':{name:f'{name}.png' for name in ('eye_half','eye_closed')},
        'eyes':{name:{'opacity':[['Eye{S}Open',[[0,1],[1,0]]]],'blinks':False} for name in ('eye_half','eye_closed')}}}
    out=with_vowel_keys(recipe,paths,tmp_path)
    ev=out['expressions'];vs=load_variants(ev['box'],{k:str(tmp_path/p) for k,p in ev['images'].items()},src)
    result=build_variants(vs,ev,layers,{'head_radius':[45,50]},lambda *_:None)
    assert len(result)==9
    assert {spec['id'] for _,spec in result} == {f'eye{s}-{n}' for s in ('L','R') for n in ('eye_half','eye_closed')} | {f'mouth-vowel_{v}' for v in VOWELS}
    for layer,spec in result:
        assert layer.alpha.max()>0.99
        assert np.isfinite(layer.rgba).all()
        assert spec['deformers']==[]
        assert spec['opacity']
