"""Procedural MaterialX/Karma adapters using portable native EEVEE nodes.

Scalar Perlin uses Blender's signed OSL-derived noise. Color and cellular
hashes differ; their diagnostics are part of the material's public metadata.
There is no texture baking or VEX execution in this module.
"""
import itertools
import bpy
from shader_utils import feed, scalar, vector, components, combine, component_math


def socket(value): return isinstance(value,bpy.types.NodeSocket)


def select(tree, condition, yes, no, size=1):
    if not socket(condition): return yes if condition else no
    node=tree.nodes.new('ShaderNodeMix')
    node.data_type='FLOAT' if size==1 else 'VECTOR'
    node.clamp_factor=False
    feed(tree,node.inputs[0],condition)
    start=2 if size==1 else 4
    feed(tree,node.inputs[start],no); feed(tree,node.inputs[start+1],yes)
    return node.outputs[0 if size==1 else 1]


def white(tree, point, dimension, color=False):
    node=tree.nodes.new('ShaderNodeTexWhiteNoise'); node.noise_dimensions=f'{dimension}D'
    if not socket(point):point=combine(tree,components(tree,point,3))
    feed(tree,node.inputs['Vector'],point)
    return node.outputs['Color' if color else 'Value']


def perlin(tree, point, dimension, color=False, detail=0., roughness=.5, lacunarity=2.):
    node=tree.nodes.new('ShaderNodeTexNoise')
    node.noise_dimensions=f'{dimension}D'; node.noise_type='FBM'; node.normalize=False
    # An unlinked texture Vector socket uses implicit Generated coordinates,
    # even when its default_value is explicitly assigned.
    if not socket(point):point=combine(tree,components(tree,point,3))
    for name,value in (('Vector',point),('Scale',1.),('Detail',detail),('Roughness',roughness),('Lacunarity',lacunarity),('Distortion',0.)):
        feed(tree,node.inputs[name],value)
    return node.outputs['Color' if color else 'Factor']


def fractal(builder, key, point, dimension, size, octaves, lacunarity, diminish):
    tree=builder.tree
    if not socket(octaves):
        octaves=max(0,int(octaves))
        if octaves>64: raise ValueError(key+': EEVEE noise adapter supports up to 64 octaves; reduce Octaves.')
        if octaves==0: return [0.]*size if size>1 else 0.
    else:
        builder.warn(key,'Connected Octaves is evaluated as an integer in the range 0–64.')
        octaves=scalar(tree,'FLOOR',octaves)
    # Fractal vector2 and the fourth component use explicit scalar offsets in
    # MaterialX. Their offsets differ from the Perlin vector2/vector4 wrappers.
    if size==2:
        shifted=vector(tree,'ADD',point,(19,193,17) if dimension==3 else (19,193,0))
        return combine(tree,[fractal(builder,key,p,dimension,1,octaves,lacunarity,diminish) for p in (point,shifted)])
    if size==4:
        rgb=fractal(builder,key,point,dimension,3,octaves,lacunarity,diminish)
        shifted=vector(tree,'ADD',point,(19,193,17) if dimension==3 else (19,193,0))
        return combine(tree,[*components(tree,rgb),fractal(builder,key,shifted,dimension,1,octaves,lacunarity,diminish)])
    if size==3: builder.warn(key,'Color/vector Fractal uses Blender channel hashes; its RGB pattern differs from MaterialX/Karma.')
    result=[0.]*size if size>1 else 0.
    # Native fBM clamps negative roughness. Explicit octaves retain signed
    # diminish, including connected values, instead of silently changing it.
    explicit=socket(diminish) or diminish<0
    count=64 if socket(octaves) else octaves
    stride=1 if explicit else 16
    for start in range(0,count,stride):
        scale=scalar(tree,'POWER',lacunarity,start) if start else 1.
        amplitude=scalar(tree,'POWER',diminish,start) if start else 1.
        coord=vector(tree,'MULTIPLY',point,combine(tree,[scale]*3))
        detail=0. if explicit else (scalar(tree,'MINIMUM',scalar(tree,'MAXIMUM',scalar(tree,'SUBTRACT',octaves,start+1),0.),15.) if socket(octaves) else min(15,octaves-start-1))
        value=perlin(tree,coord,dimension,size==3,detail,diminish,lacunarity)
        if socket(octaves): amplitude=scalar(tree,'MULTIPLY',amplitude,scalar(tree,'GREATER_THAN',octaves,start))
        result=component_math(tree,'ADD',size,result,component_math(tree,'MULTIPLY',size,value,amplitude))
    return result


def cellular(builder, key, point, dimension, count, jitter, metric=0, positions=False):
    """F1/F2/F3 and nearest feature positions with one consistent cell hash.

    Blender 5.2 Voronoi uses PCG, while White Noise uses Jenkins. Mixing native
    Voronoi F1/F2 with an expanded F3 changes the pattern when the signature
    changes. A common adjacent-cell search preserves every output's meaning.
    """
    tree=builder.tree
    builder.warn(key,'Cell/Worley/Voronoi uses Blender random hashes; cell layout differs from MaterialX/Karma.')
    if not socket(metric) and metric not in (0,1,2,3): raise ValueError(key+': unsupported Voronoi Metric '+str(metric))
    origin=component_math(tree,'FLOOR',dimension,point)
    # Search and sort in local coordinates to avoid precision loss when points
    # are far from the origin; nearest feature positions are restored afterward.
    local=vector(tree,'SUBTRACT',point,origin)
    distances=[1.e30]*count; points=[(0.,0.,0.)]*count
    for cell in itertools.product((-1,0,1),repeat=dimension):
        offset=(*cell,0.) if dimension==2 else cell
        hashed=white(tree,vector(tree,'ADD',origin,offset),dimension,True)
        feature=component_math(tree,'ADD',dimension,offset,
            component_math(tree,'ADD',dimension,.5,component_math(tree,'MULTIPLY',dimension,jitter,component_math(tree,'SUBTRACT',dimension,hashed,.5))))
        diff=vector(tree,'SUBTRACT',feature,local)
        squared=vector(tree,'DOT_PRODUCT',diff,diff)
        absolute=components(tree,component_math(tree,'ABSOLUTE',dimension,diff),dimension)
        def distance_for(m):
            if m in (0,1): return squared
            result=absolute[0]
            for part in absolute[1:]: result=scalar(tree,'ADD' if m==2 else 'MAXIMUM',result,part)
            return result
        distance=distance_for(metric) if not socket(metric) else select(tree,scalar(tree,'COMPARE',metric,2.,0.),distance_for(2),select(tree,scalar(tree,'COMPARE',metric,3.,0.),distance_for(3),squared))
        for i in range(count):
            nearer=scalar(tree,'LESS_THAN',distance,distances[i]) if positions else None
            old=distances[i]
            distances[i]=scalar(tree,'MINIMUM',distance,old)
            if i+1<count:distance=scalar(tree,'MAXIMUM',distance,old)
            if positions:
                oldpoint=points[i]
                points[i]=select(tree,nearer,feature,oldpoint,3)
                if i+1<count:feature=select(tree,nearer,oldpoint,feature,3)
    if socket(metric): distances=[select(tree,scalar(tree,'COMPARE',metric,0.,0.),scalar(tree,'SQRT',d),d) for d in distances]
    elif metric==0: distances=[scalar(tree,'SQRT',d) for d in distances]
    return distances,[vector(tree,'ADD',origin,p) for p in points] if positions else []


def worley(builder,key,point,dimension,size,jitter,style):
    tree=builder.tree
    if not socket(style) and style not in (0,1): raise ValueError(key+': unsupported Worley Style '+str(style))
    distances,positions=cellular(builder,key,point,dimension,size if style!=1 else 1,jitter,positions=socket(style) or style==1)
    distance=distances[0] if size==1 else combine(tree,distances)
    if not socket(style) and style==0:return distance
    cell=component_math(tree,'FLOOR',dimension,positions[0])
    solid=white(tree,cell,dimension,size>1)
    if size==2:solid=combine(tree,components(tree,solid,2))
    if not socket(style): return solid
    return select(tree,scalar(tree,'COMPARE',style,1.,0.),solid,distance,size)


def translate(builder,key,kind,size):
    tree=builder.tree;v=lambda name:builder.val(key,name)
    dimension=2 if kind.endswith('2d') else 3
    point=builder.coordinate(key,'texcoord' if dimension==2 else 'position',dimension)
    if kind.startswith('karma_voronoi'):
        point=component_math(tree,'ADD',dimension,component_math(tree,'MULTIPLY',dimension,point,v('freq')),v('offset'))
        needed=any((key,p) in builder.requested for p in ('p1','p2','p3'))
        distances,points=cellular(builder,key,point,dimension,size,v('jitter'),v('metric'),needed)
        result={'distance':distances[0] if size==1 else combine(tree,distances)}
        result.update({f'p{i+1}':points[i] if i<len(points) else (0.,0.,0.) for i in range(3)})
        return result
    if kind.startswith('unifiednoise'):
        point=component_math(tree,'ADD',dimension,component_math(tree,'MULTIPLY',dimension,point,v('freq')),v('offset'))
        which=v('type')
        if not socket(which) and which not in range(4): raise ValueError(key+': unsupported Unified Noise Type '+str(which))
        angle=scalar(tree,'MULTIPLY',scalar(tree,'SUBTRACT',v('jitter'),1.),90000.)
        rotated=builder.rotate(point,angle) if dimension==2 else builder.rotate3d(point,angle,(.1,1.,0.))
        def branch(i):
            if i==0:return scalar(tree,'ADD',scalar(tree,'MULTIPLY',perlin(tree,rotated,dimension),.5),.5)
            if i==1:
                builder.warn(key,'Cell Noise uses Blender random hashes; cell values differ from MaterialX/Karma.')
                return white(tree,component_math(tree,'FLOOR',dimension,rotated),dimension)
            if i==2:return worley(builder,key,point,dimension,1,v('jitter'),v('style'))
            # MaterialX Unified 2D's fractal branch is a 3D slice, with jitter
            # driving Z. It is deliberately different from Fractal 2D.
            domain=combine(tree,[*components(tree,point,2),angle]) if dimension==2 else rotated
            return fractal(builder,key,domain,3,1,v('octaves'),v('lacunarity'),v('diminish'))
        result=branch(which) if not socket(which) else branch(0)
        if socket(which):
            for i in range(1,4): result=select(tree,scalar(tree,'COMPARE',which,i,0.),branch(i),result)
        result=scalar(tree,'ADD',v('outmin'),scalar(tree,'MULTIPLY',result,scalar(tree,'SUBTRACT',v('outmax'),v('outmin'))))
        clamped=scalar(tree,'MINIMUM',scalar(tree,'MAXIMUM',result,v('outmin')),v('outmax'))
        return {'out':select(tree,v('clampoutput'),clamped,result)}
    if kind.startswith('worleynoise'):return {'out':worley(builder,key,point,dimension,size,v('jitter'),v('style'))}
    if kind.startswith('cellnoise'):
        builder.warn(key,'Cell Noise uses Blender random hashes; cell values differ from MaterialX/Karma.')
        return {'out':white(tree,component_math(tree,'FLOOR',dimension,point),dimension)}
    if kind.startswith('fractal'):
        result=fractal(builder,key,point,dimension,size,v('octaves'),v('lacunarity'),v('diminish'))
    else:
        result=perlin(tree,point,dimension,size>1)
        if size>1:
            builder.warn(key,'Color/vector Perlin uses Blender channel hashes; its RGB pattern differs from MaterialX/Karma.')
            parts=components(tree,result,min(size,3))
            if size==4:
                shifted=vector(tree,'ADD',point,(19,73,29) if dimension==3 else (19,73,0))
                parts.append(perlin(tree,shifted,dimension))
            result=combine(tree,parts)
    result=component_math(tree,'MULTIPLY',size,result,v('amplitude'))
    if kind.startswith('noise'):result=component_math(tree,'ADD',size,result,v('pivot'))
    return {'out':result}
