"""USD subdivision surfaces, face-varying interpolation and OpenSubdiv creases."""
import math
import numpy as np
from viewport_state import assign

UV_RULES={'none':'ALL','cornersOnly':'PRESERVE_CORNERS','cornersPlus1':'PRESERVE_CORNERS_AND_JUNCTIONS',
          'cornersPlus2':'PRESERVE_CORNERS_JUNCTIONS_AND_CONCAVE','boundaries':'PRESERVE_BOUNDARIES','all':'NONE'}

def sync(obj,update):
    if 'subdivision_scheme' in update:obj['usd_subdivision_scheme']=update['subdivision_scheme']
    scheme=obj.get('usd_subdivision_scheme','none');modifier=obj.modifiers.get('USD subdivision')
    if scheme in ('none',''):
        if modifier:obj.modifiers.remove(modifier)
        return
    if scheme not in ('catmullClark','bilinear'):raise ValueError('Unsupported USD subdivision scheme: '+scheme)
    if modifier is None:modifier=obj.modifiers.new('USD subdivision','SUBSURF')
    modifier.subdivision_type='SIMPLE' if scheme=='bilinear' else 'CATMULL_CLARK'
    definition=update.get('subdivision',{})
    # Store Hydra's requested refinement separately from EEVEE's tessellation
    # controls. Hydra can request very high levels for an adaptive renderer;
    # blindly using those as uniform Blender levels exhausts GPU memory.
    level=int(definition.get('refine_level',obj.get('usd_refine_level',0)))
    obj['usd_refine_level']=level
    modifier.levels=1;modifier.render_levels=1
    if 'boundary' in definition:modifier.boundary_smooth='PRESERVE_CORNERS' if definition['boundary']=='edgeAndCorner' else 'ALL'
    if 'face_varying' in definition:modifier.uv_smooth=UV_RULES.get(definition['face_varying'],'PRESERVE_CORNERS_AND_JUNCTIONS')
    mesh=obj.data
    for name,domain,indices,weights in (
        ('crease_vert','POINT',definition.get('corner_indices',[]),definition.get('corner_weights',[])),
        ('crease_edge','EDGE',definition.get('crease_indices',[]),definition.get('crease_weights',[]))):
        old=mesh.attributes.get(name)
        if old:mesh.attributes.remove(old)
        if not indices:continue
        attr=mesh.attributes.new(name,'FLOAT',domain)
        values=np.zeros(len(attr.data),dtype=np.float32)
        if domain=='POINT':
            for index,weight in zip(indices,weights):values[index]=math.sqrt(max(0.,min(10.,weight))*.1)
        else:
            lookup={tuple(sorted(e.vertices)):e.index for e in mesh.edges};offset=0;edge_offset=0
            lengths=definition.get('crease_lengths',[])
            for chain,length in enumerate(lengths):
                for i in range(length-1):
                    edge=lookup.get(tuple(sorted(indices[offset+i:offset+i+2])))
                    weight=weights[chain] if len(weights)==len(lengths) else weights[edge_offset+i]
                    if edge is not None:values[edge]=math.sqrt(max(0.,min(10.,weight))*.1)
                offset+=length;edge_offset+=length-1
        attr.data.foreach_set('value',values)


def configure(worker,config,limit_surface=True):
    """limit_surface=False shows the refined cage at each level (as Houdini's
    GL viewport does). Blender's exact limit-surface evaluation made the first
    viewport draw of a production shot take 30 s instead of under 1 s."""
    quality=config.get('geometry',{})
    viewport=int(quality.get('viewport_subdivision',1))
    render=int(quality.get('render_subdivision',2))
    budget=int(quality.get('subdivision_face_budget',500000))
    if not (0<=viewport<=6 and 0<=render<=6 and budget>=0):
        raise ValueError('Invalid EEVEE subdivision settings')
    limited=0
    for key,obj in worker.objects.items():
        modifier=obj.modifiers.get('USD subdivision')
        if modifier is None:
            for instance in worker.instances.get(key,[]):
                old=instance.modifiers.get('USD subdivision')
                if old:instance.modifiers.remove(old)
            continue
        authored=int(obj.get('usd_refine_level',0))
        levels=[min(n,authored) if authored>0 else n for n in (viewport,render)]
        if budget:
            # Each Catmull-Clark step creates four quads per previous quad.
            # Account for arbitrary initial polygon sizes on the first step.
            faces=len(obj.data.loops)
            ceiling=0
            while ceiling<6 and faces<=budget:
                ceiling+=1;faces*=4
            if max(levels)>ceiling:limited+=1
            levels=[min(n,ceiling) for n in levels]
        assign(modifier, 'levels', levels[0])
        assign(modifier, 'render_levels', levels[1])
        assign(modifier, 'use_limit_surface', limit_surface)
        # Ordinary Blender instances share mesh data, not object modifiers.
        for instance in worker.instances.get(key,[]):
            copy=instance.modifiers.get('USD subdivision') or instance.modifiers.new('USD subdivision','SUBSURF')
            for prop in ('subdivision_type','levels','render_levels','uv_smooth','boundary_smooth','use_limit_surface'):
                assign(copy,prop,getattr(modifier,prop))
    return limited
