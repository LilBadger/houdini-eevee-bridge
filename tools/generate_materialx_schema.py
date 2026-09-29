"""Extract portable node signatures/defaults from Houdini's MaterialX library."""
import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET

OPERATIONS = set('standard_surface surfacematerial image tiledimage texcoord geompropvalue normalmap bump constant add subtract multiply divide power min max absval floor ceil round sign sqrt sin cos tan asin acos atan2 ln exp clamp invert mix remap range separate2 separate3 separate4 combine2 combine3 combine4 convert extract dotproduct crossproduct normalize magnitude rotate2d rotate3d place2d luminance position transformpoint transformvector noise2d noise3d fractal2d fractal3d cellnoise2d cellnoise3d worleynoise2d worleynoise3d unifiednoise2d unifiednoise3d'.split())
OPERATIONS.update('colorcorrect normal surface_unlit displacement'.split())
TYPES = {'float','integer','boolean','color3','color4','vector2','vector3','vector4','string','filename','surfaceshader','material',
         'displacementshader'}

def generate(library, output):
    definitions = {}
    for file in (library/'stdlib/stdlib_defs.mtlx', library/'bxdf/standard_surface.mtlx', library/'pbrlib/pbrlib_defs.mtlx'):
        for nd in ET.parse(file).getroot().findall('nodedef'):
            definitions[nd.attrib['name']] = nd
    def entry(nd):
        result = entry(definitions[nd.get('inherit')]) if nd.get('inherit') else {'inputs':{}, 'outputs':{}}
        result['node'] = nd.attrib['node']
        for tag in ('input','output'):
            for v in nd.findall(tag):
                p = {'type':v.attrib['type']}
                if 'value' in v.attrib:
                    raw = v.attrib['value']; kind = p['type']
                    p['value'] = (None if raw == '' and kind not in ('string','filename') else raw if kind in ('string','filename') else raw == 'true' if kind == 'boolean' else
                                  int(raw) if kind == 'integer' else [float(x) for x in raw.split(',')] if ',' in raw else float(raw))
                if v.get('defaultgeomprop'): p['geomprop'] = v.get('defaultgeomprop')
                result[tag+'s'][v.attrib['name']] = p
        return result
    nodes = {key:entry(nd) for key,nd in definitions.items() if nd.get('node') in OPERATIONS}
    nodes = {key:value for key,value in nodes.items()
             if all(p['type'] in TYPES for p in (*value['inputs'].values(),*value['outputs'].values()))}
    nodes = {key:value for key,value in nodes.items() if key not in ('ND_geompropvalue_color4','ND_geompropvalue_vector4')}
    # Karma's USD shader identifiers are not part of MaterialX's stdlib.
    # Signatures/defaults verified with Houdini 22's VOPs and Sdr registry.
    for dimension in (2,3):
        for dtype in ('float','vector2','vector3'):
            nodes[f'kma_voronoinoise{dimension}d_{dtype}'] = {
                'node':f'karma_voronoi{dimension}d',
                'inputs':{
                    'texcoord' if dimension==2 else 'position':{'type':f'vector{dimension}','geomprop':'UV0' if dimension==2 else 'Pobject'},
                    'jitter':{'type':'float','value':1.}, 'metric':{'type':'integer','value':0},
                    'freq':{'type':f'vector{dimension}','value':[1.]*dimension},
                    'offset':{'type':f'vector{dimension}','value':[0.]*dimension}},
                'outputs':{'distance':{'type':dtype}, **{f'p{i}':{'type':f'vector{dimension}'} for i in (1,2,3)}}}
    for signature,dtype in (('float','float'),('color','color3')):
        nodes['kma_rampconst_'+signature]={'node':'karma_ramp','inputs':{'t':{'type':'float','value':0.}},'outputs':{'out':{'type':dtype}}}
    for signature in ('kma_hair','kma_fur_2'):
        nodes[signature]={'node':'karma_hair','inputs':{},'outputs':{'out':{'type':'surfaceshader'}}}
    output.write_text(json.dumps(nodes, indent=2)+'\n', encoding='utf-8')
    print('Wrote',len(nodes),'MaterialX node signatures to',output)

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('library',type=Path)
    parser.add_argument('--output',type=Path,default=Path(__file__).resolve().parents[1]/'worker/materialx_nodes.json')
    args=parser.parse_args(); generate(args.library,args.output)
