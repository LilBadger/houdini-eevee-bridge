"""Translate a supported MaterialX surface graph into native EEVEE nodes.

Node signatures/defaults come from Houdini's MaterialX standard library. This is
an adapter to Blender's BSDFs, not a MaterialX runtime or a VEX interpreter.
Unsupported reachable nodes are diagnosed rather than silently disconnected.
"""
import json
import math
from pathlib import Path
import bpy
from mathutils import Matrix
from shader_utils import feed, scalar, vector, components, combine, component_math, image_file, matrix_vector, primvar, Primvar
import materialx_noise
import karma_material

SCHEMA = json.loads(Path(__file__).with_name('materialx_nodes.json').read_text())
SIZES = {'float':1,'integer':1,'boolean':1,'vector2':2,'vector3':3,'color3':3,'vector4':4,'color4':4}
MATH = {'add':'ADD','subtract':'SUBTRACT','multiply':'MULTIPLY','divide':'DIVIDE',
        'power':'POWER','min':'MINIMUM','max':'MAXIMUM','absval':'ABSOLUTE',
        'floor':'FLOOR','ceil':'CEIL','round':'ROUND','sign':'SIGN','sqrt':'SQRT',
        'sin':'SINE','cos':'COSINE','tan':'TANGENT','asin':'ARCSINE','acos':'ARCCOSINE',
        'atan2':'ARCTAN2','exp':'EXPONENT','ln':'LOGARITHM'}
SURFACE_INPUTS = {
    'metalness':'Metallic', 'specular_roughness':'Roughness', 'diffuse_roughness':'Diffuse Roughness',
    'specular_IOR':'IOR', 'specular_color':'Specular Tint', 'specular_anisotropy':'Anisotropic',
    'specular_rotation':'Anisotropic Rotation', 'transmission':'Transmission Weight',
    'subsurface':'Subsurface Weight', 'subsurface_radius':'Subsurface Radius',
    'subsurface_scale':'Subsurface Scale', 'subsurface_anisotropy':'Subsurface Anisotropy',
    'coat':'Coat Weight', 'coat_color':'Coat Tint', 'coat_roughness':'Coat Roughness',
    'coat_IOR':'Coat IOR', 'coat_normal':'Coat Normal', 'sheen':'Sheen Weight',
    'sheen_color':'Sheen Tint', 'sheen_roughness':'Sheen Roughness',
    'emission':'Emission Strength', 'emission_color':'Emission Color',
    'thin_film_thickness':'Thin Film Thickness', 'thin_film_IOR':'Thin Film IOR',
    'thin_walled':'Thin Wall', 'normal':'Normal', 'tangent':'Tangent'}


class TranslationError(ValueError):
    pass


class Builder:
    def __init__(self, tree, definition, basis=None):
        self.tree = tree
        self.nodes = {n['id']:n for n in definition['nodes']}
        self.links = {(c,d):(a,b) for a,b,c,d in definition.get('links',[])}
        self.requested = set(self.links.values())
        self.cache, self.visiting, self.warnings = {}, set(), []
        self.basis=Matrix.Identity(3) if basis is None else basis.to_3x3()

    def warn(self, key, text):
        message = key+': '+text
        if message not in self.warnings: self.warnings.append(message)

    def val(self, key, name, default=None):
        link = self.links.get((key,name))
        if link: return self.output(*link)
        item = self.nodes[key]
        param = SCHEMA[item['type']]['inputs'].get(name,{})
        return item.get('parameters',{}).get(name,param.get('value',default))

    def changed(self, key, name):
        item = self.nodes[key]
        return (key,name) in self.links or (name in item.get('parameters',{}) and
               item['parameters'][name] != SCHEMA[item['type']]['inputs'].get(name,{}).get('value'))

    def uv(self, key, visited=None):
        visited = set() if visited is None else visited
        if key in visited: return None
        visited.add(key)
        item=self.nodes[key]
        if item['type'].startswith('ND_texcoord_'):
            index=item.get('parameters',{}).get('index',0)
            return 'st' if index==0 else 'st'+str(index)
        if item['type'].startswith('ND_geompropvalue_'):
            return item.get('parameters',{}).get('geomprop','st')
        for name in ('texcoord','in','in1'):
            link=self.links.get((key,name))
            if link:
                result=self.uv(link[0],visited)
                if result: return result
        return None

    def output(self, key, port='out'):
        if key not in self.nodes: raise TranslationError('Missing node '+key)
        if key in self.visiting: raise TranslationError('Cyclic MaterialX graph at '+key)
        if (key,port) not in self.requested:
            self.requested.add((key,port));self.cache.pop(key,None)
        if key not in self.cache:
            self.visiting.add(key)
            try: self.cache[key] = self.build(key)
            finally: self.visiting.remove(key)
        if port not in self.cache[key]: raise TranslationError('Unsupported output '+key+'.'+port)
        return self.cache[key][port]

    def build(self, key):
        item=self.nodes[key]; identifier=item['type']; p=item.get('parameters',{})
        if identifier not in SCHEMA:
            if not identifier or 'auto_' in key:
                raise TranslationError('Unsupported VEX/custom shader at '+key+'; EEVEE cannot execute compiled Houdini VEX. Use a MaterialX noise graph or bake the shader to textures.')
            raise TranslationError('Unsupported MaterialX node '+identifier+' at '+key)
        schema=SCHEMA[identifier]; kind=schema['node']; tree=self.tree
        dtype=next(iter(schema['outputs'].values()))['type']; size=SIZES.get(dtype,1)
        v=lambda name,default=None:self.val(key,name,default)
        op=lambda operation,*args:component_math(tree,operation,size,*args)
        if kind=='standard_surface':
            return {'out':self.surface(key)}
        if kind=='karma_ramp':return {'out':karma_material.ramp(self,key,size)}
        if kind=='karma_hair':return {'out':karma_material.hair(self,key)}
        if kind=='surface_unlit':
            emission=tree.nodes.new('ShaderNodeEmission')
            feed(tree,emission.inputs['Color'],v('emission_color'));feed(tree,emission.inputs['Strength'],v('emission'))
            transparent=tree.nodes.new('ShaderNodeBsdfTransparent')
            transmission=vector(tree,'MULTIPLY',v('transmission_color'),combine(tree,[v('transmission')]*3))
            # MaterialX unlit: emission*opacity plus transmitted light through
            # both cutouts and the authored transmission lobe.
            feed(tree,transparent.inputs['Color'],vector(tree,'ADD',combine(tree,[scalar(tree,'SUBTRACT',1.,v('opacity'))]*3),vector(tree,'MULTIPLY',transmission,combine(tree,[v('opacity')]*3))))
            feed(tree,emission.inputs['Strength'],scalar(tree,'MULTIPLY',v('emission'),v('opacity')))
            add=tree.nodes.new('ShaderNodeAddShader');feed(tree,add.inputs[0],emission.outputs[0]);feed(tree,add.inputs[1],transparent.outputs[0])
            return {'out':add.outputs[0]}
        if kind=='colorcorrect':
            # Exact operation order/defaults from NG_colorcorrect_color3.
            color=v('in')
            if self.changed(key,'hue'):
                hsv=tree.nodes.new('ShaderNodeSeparateColor');hsv.mode='HSV';feed(tree,hsv.inputs[0],color)
                rgb=tree.nodes.new('ShaderNodeCombineColor');rgb.mode='HSV'
                feed(tree,rgb.inputs[0],scalar(tree,'FRACT',scalar(tree,'ADD',hsv.outputs[0],v('hue'))))
                feed(tree,rgb.inputs[1],hsv.outputs[1]);feed(tree,rgb.inputs[2],hsv.outputs[2]);color=rgb.outputs[0]
            lum=vector(tree,'DOT_PRODUCT',color,(.2722287,.6740818,.0536895))
            c=lambda operation,*values:component_math(tree,operation,3,*values)
            color=c('ADD',lum,c('MULTIPLY',c('SUBTRACT',color,lum),v('saturation')))
            color=c('MULTIPLY',c('SIGN',color),c('POWER',c('ABSOLUTE',color),scalar(tree,'DIVIDE',1.,v('gamma'))))
            color=c('MULTIPLY',c('ADD',c('MULTIPLY',color,scalar(tree,'SUBTRACT',1.,v('lift'))),v('lift')),v('gain'))
            color=c('ADD',c('MULTIPLY',c('SUBTRACT',color,v('contrastpivot')),v('contrast')),v('contrastpivot'))
            color=c('MULTIPLY',color,scalar(tree,'POWER',2.,v('exposure')))
            return {'out':combine(tree,[*components(tree,color),components(tree,v('in'),4)[3]]) if size==4 else color}
        if kind=='surfacematerial':
            return {'out':v('surfaceshader')}
        if kind=='displacement':
            # MaterialX scalar displacement is along the normal; vector displacement is
            # in (dPdu, dPdv, N) tangent space. Neither has a midlevel.
            vector_input=schema['inputs']['displacement']['type']=='vector3'
            node=tree.nodes.new('ShaderNodeVectorDisplacement' if vector_input else 'ShaderNodeDisplacement')
            if vector_input: node.space='TANGENT'
            feed(tree,node.inputs['Vector' if vector_input else 'Height'],v('displacement'))
            node.inputs['Midlevel'].default_value=0.
            feed(tree,node.inputs['Scale'],v('scale'))
            return {'out':node.outputs['Displacement']}
        if kind=='constant':
            return {'out':v('value')}
        if kind.startswith(('noise','fractal','cellnoise','worleynoise','unifiednoise','karma_voronoi')):
            return materialx_noise.translate(self,key,kind,size)
        if kind=='position': return {'out':self.position(v('space'))}
        if kind=='normal':
            geometry=tree.nodes.new('ShaderNodeNewGeometry');normal=geometry.outputs['Normal']
            space=v('space')
            if space=='world':normal=matrix_vector(tree,self.basis.inverted(),normal)
            elif space in ('object','model'):
                node=tree.nodes.new('ShaderNodeVectorTransform');node.vector_type='NORMAL';node.convert_from='WORLD';node.convert_to='OBJECT';feed(tree,node.inputs[0],normal);normal=node.outputs[0]
            else:raise TranslationError('Unsupported normal space '+str(space))
            return {'out':normal}
        if kind in ('transformpoint','transformvector'):
            return {'out':self.transform(v('in'),v('fromspace'),v('tospace'),'POINT' if kind=='transformpoint' else 'VECTOR')}
        if kind in ('image','tiledimage'):
            return {'out':self.texture(key,kind,dtype)}
        if kind=='texcoord':
            node=tree.nodes.new('ShaderNodeUVMap')
            index=v('index'); node.uv_map='st' if index==0 else 'st'+str(index)
            return {'out':node.outputs['UV']}
        if kind=='geompropvalue':
            name=v('geomprop')
            if size==4: raise TranslationError('Four-channel geometry primvars are not exported at '+key)
            if dtype=='vector2' or name in ('st','uv'):
                node=tree.nodes.new('ShaderNodeUVMap'); node.uv_map=name
                out=node.outputs['UV']
            else:
                node=primvar(tree,name)
                out=node.outputs['Fac' if size==1 else 'Color' if dtype=='color3' else 'Vector']
            # Attribute Alpha is 1 even when a Blender attribute is missing.
            # Import an independent presence marker, so authored zero stays
            # distinct from an absent USD primvar with a nonzero default.
            if isinstance(node,Primvar): present=node.outputs['present']
            else: marker=tree.nodes.new('ShaderNodeAttribute');marker.attribute_name='hde:present:'+name;present=marker.outputs['Fac']
            default=v('default',0. if size==1 else [0.]*size)
            out=component_math(tree,'ADD',size,default,component_math(tree,'MULTIPLY',size,component_math(tree,'SUBTRACT',size,out,default),present))
            return {'out':out}
        if kind in MATH:
            values=[v(n) for n in schema['inputs']]
            if kind=='ln': values.append(math.e)
            return {'out':op(MATH[kind],*values)}
        if kind=='clamp':
            return {'out':op('MINIMUM',op('MAXIMUM',v('in'),v('low')),v('high'))}
        if kind=='invert':
            return {'out':op('SUBTRACT',v('amount'),v('in'))}
        if kind=='mix':
            if dtype=='surfaceshader':
                node=tree.nodes.new('ShaderNodeMixShader')
                feed(tree,node.inputs[0],v('mix'))
                for socket,name in zip(node.inputs[1:],('bg','fg')):
                    value=v(name)
                    if value is not None: feed(tree,socket,value)
                return {'out':node.outputs[0]}
            return {'out':op('ADD',v('bg'),op('MULTIPLY',op('SUBTRACT',v('fg'),v('bg')),v('mix')))}
        if kind in ('remap','range'):
            result=op('DIVIDE',op('SUBTRACT',v('in'),v('inlow')),op('SUBTRACT',v('inhigh'),v('inlow')))
            if kind=='range':
                result=op('MULTIPLY',op('SIGN',result),op('POWER',op('ABSOLUTE',result),op('DIVIDE',1.,v('gamma'))))
            result=op('ADD',v('outlow'),op('MULTIPLY',result,op('SUBTRACT',v('outhigh'),v('outlow'))))
            if kind=='range' and v('doclamp'):
                if isinstance(v('doclamp'),bpy.types.NodeSocket): raise TranslationError('Connected range doclamp is not supported at '+key)
                result=op('MINIMUM',op('MAXIMUM',result,v('outlow')),v('outhigh'))
            return {'out':result}
        if kind.startswith('separate'):
            ports=list(schema['outputs']); values=components(tree,v('in'),len(ports))
            return dict(zip(ports,values))
        if kind.startswith('combine'):
            return {'out':combine(tree,[v(n) for n in schema['inputs']])}
        if kind=='extract':
            values=components(tree,v('in'),SIZES[schema['inputs']['in']['type']]); index=v('index')
            if isinstance(index,bpy.types.NodeSocket):
                result=0.
                for i,part in enumerate(values):result=scalar(tree,'ADD',result,scalar(tree,'MULTIPLY',part,scalar(tree,'COMPARE',index,i,0.)))
            else:result=values[int(index)] if 0<=int(index)<len(values) else 0.
            return {'out':result}
        if kind=='convert':
            source_type=schema['inputs']['in']['type']; source_size=SIZES.get(source_type,1)
            value=v('in')
            if dtype=='surfaceshader':
                node=tree.nodes.new('ShaderNodeEmission')
                feed(tree,node.inputs['Color'],value)
                if source_size==4:
                    transparent=tree.nodes.new('ShaderNodeBsdfTransparent'); mix=tree.nodes.new('ShaderNodeMixShader')
                    feed(tree,mix.inputs[0],components(tree,value,4)[3])
                    feed(tree,mix.inputs[1],transparent.outputs[0]);feed(tree,mix.inputs[2],node.outputs[0])
                    return {'out':mix.outputs[0]}
                return {'out':node.outputs[0]}
            if size==1 and source_size>1:
                value=scalar(tree,'MULTIPLY',scalar(tree,'ADD',*components(tree,value,2)),.5) if source_size==2 else vector(tree,'DOT_PRODUCT',value,(1/3,1/3,1/3))
            elif size>1:
                parts=components(tree,value,source_size) if source_size>1 else [value]*min(size,3)
                # MaterialX vector widening pads missing XYZ with zero and W
                # with one, including float -> color4/vector4.
                while len(parts)<size: parts.append(1. if len(parts)==3 else 0.)
                value=combine(tree,parts[:size])
            if dtype=='integer': value=scalar(tree,'TRUNC',value)
            elif dtype=='boolean': value=scalar(tree,'GREATER_THAN',scalar(tree,'ABSOLUTE',value),0.)
            return {'out':value}
        if kind in ('dotproduct','crossproduct','normalize','magnitude'):
            input_size=SIZES.get(schema['inputs']['in' if 'in' in schema['inputs'] else 'in1']['type'],3)
            if input_size==4:
                parts=components(tree,v('in') if kind!='dotproduct' else v('in1'),4)
                other=components(tree,v('in2'),4) if kind=='dotproduct' else parts
                total=0.
                for a,b in zip(parts,other):total=scalar(tree,'ADD',total,scalar(tree,'MULTIPLY',a,b))
                if kind=='dotproduct':return {'out':total}
                length=scalar(tree,'SQRT',total)
                return {'out':length if kind=='magnitude' else combine(tree,[scalar(tree,'DIVIDE',p,length) for p in parts])}
            operation={'dotproduct':'DOT_PRODUCT','crossproduct':'CROSS_PRODUCT','normalize':'NORMALIZE','magnitude':'LENGTH'}[kind]
            return {'out':vector(tree,operation,*(v(n) for n in schema['inputs']))}
        if kind=='luminance':
            result=vector(tree,'DOT_PRODUCT',v('in'),v('lumacoeffs'))
            return {'out':combine(tree,[result]*3+[components(tree,v('in'),4)[3]]) if size==4 else combine(tree,[result]*3) if size>1 else result}
        if kind=='rotate2d': return {'out':self.rotate(v('in'),v('amount'))}
        if kind=='rotate3d': return {'out':self.rotate3d(v('in'),v('amount'),v('axis'))}
        if kind=='place2d':
            result=vector(tree,'SUBTRACT',v('texcoord'),v('pivot'))
            order=v('operationorder')
            if order not in (0,1): raise TranslationError('Unsupported place2d operation order at '+key)
            if order==1: result=vector(tree,'SUBTRACT',result,v('offset'))
            if order==0: result=component_math(tree,'DIVIDE',2,result,v('scale'))
            result=self.rotate(result,v('rotate'))
            result=(vector(tree,'SUBTRACT',result,v('offset')) if order==0 else component_math(tree,'DIVIDE',2,result,v('scale')))
            return {'out':vector(tree,'ADD',result,v('pivot'))}
        if kind=='normalmap':
            # MaterialX scales tangent X/Y before normalizing. Blender's strength
            # blends normals differently, so encode the scaled vector explicitly.
            encoded=vector(tree,'SUBTRACT',vector(tree,'MULTIPLY',v('in'),(2,2,2)),(1,1,1))
            zero=scalar(tree,'COMPARE',vector(tree,'DOT_PRODUCT',v('in'),v('in')),0.,0.)
            encoded=vector(tree,'ADD',encoded,vector(tree,'MULTIPLY',vector(tree,'SUBTRACT',(0,0,1),encoded),combine(tree,[zero]*3)))
            strength=components(tree,v('scale'),2)
            encoded=vector(tree,'MULTIPLY',encoded,combine(tree,[*strength,1.]))
            encoded=vector(tree,'ADD',vector(tree,'MULTIPLY',encoded,(.5,.5,.5)),(.5,.5,.5))
            node=tree.nodes.new('ShaderNodeNormalMap'); node.uv_map=self.uv(key) or 'st'
            feed(tree,node.inputs['Color'],encoded)
            for name in ('normal','tangent','bitangent'):
                link=self.links.get((key,name))
                standard_normal=(name=='normal' and link and self.nodes[link[0]]['type']=='ND_normal_vector3' and self.val(link[0],'space')=='world')
                if self.changed(key,name) and not standard_normal: self.warn(key,'Custom '+name+' basis on Normal Map is not translated.')
            return {'out':matrix_vector(tree,self.basis.inverted(),node.outputs['Normal'])}
        if kind=='bump':
            node=tree.nodes.new('ShaderNodeBump')
            feed(tree,node.inputs['Height'],v('height')); feed(tree,node.inputs['Distance'],v('scale'))
            if self.changed(key,'normal'): feed(tree,node.inputs['Normal'],matrix_vector(tree,self.basis,v('normal')))
            for name in ('tangent','bitangent'):
                if self.changed(key,name): self.warn(key,'Custom '+name+' basis on Bump is not translated.')
            return {'out':matrix_vector(tree,self.basis.inverted(),node.outputs['Normal'])}
        raise TranslationError('Unsupported MaterialX operation '+kind+' at '+key)

    def rotate(self, value, degrees):
        x,y=components(self.tree,value,2)
        angle=scalar(self.tree,'MULTIPLY',degrees,math.pi/180.)
        c=scalar(self.tree,'COSINE',angle); s=scalar(self.tree,'SINE',angle)
        return combine(self.tree,[scalar(self.tree,'SUBTRACT',scalar(self.tree,'MULTIPLY',x,c),scalar(self.tree,'MULTIPLY',y,s)),
                                  scalar(self.tree,'ADD',scalar(self.tree,'MULTIPLY',x,s),scalar(self.tree,'MULTIPLY',y,c))])

    def rotate3d(self,value,degrees,axis):
        node=self.tree.nodes.new('ShaderNodeVectorRotate');node.rotation_type='AXIS_ANGLE'
        feed(self.tree,node.inputs['Vector'],value);feed(self.tree,node.inputs['Axis'],axis)
        feed(self.tree,node.inputs['Angle'],scalar(self.tree,'MULTIPLY',degrees,math.pi/180.))
        return node.outputs[0]

    def position(self,space='object'):
        if space in ('object','model'):
            node=self.tree.nodes.new('ShaderNodeTexCoord');return node.outputs['Object']
        if space=='world':
            node=self.tree.nodes.new('ShaderNodeNewGeometry')
            return matrix_vector(self.tree,self.basis.inverted(),node.outputs['Position'])
        raise TranslationError('Unsupported MaterialX coordinate space '+str(space))

    def coordinate(self,key,name,dimension):
        value=self.val(key,name)
        if value is not None:return combine(self.tree,components(self.tree,value,2)) if dimension==2 else value
        if dimension==3:return self.position()
        node=self.tree.nodes.new('ShaderNodeUVMap');node.uv_map='st';return node.outputs['UV']

    def transform(self,value,source,target,kind):
        source='object' if source=='model' else source;target='object' if target=='model' else target
        if not source or not target or source==target:return value
        if source not in ('object','world') or target not in ('object','world'):
            raise TranslationError('Unsupported MaterialX transform space '+source+' -> '+target)
        if source=='world':value=matrix_vector(self.tree,self.basis,value)
        node=self.tree.nodes.new('ShaderNodeVectorTransform');node.vector_type=kind
        node.convert_from=source.upper();node.convert_to=target.upper()
        feed(self.tree,node.inputs[0],value);value=node.outputs[0]
        return matrix_vector(self.tree,self.basis.inverted(),value) if target=='world' else value

    def surface(self, key):
        tree=self.tree; v=lambda name:self.val(key,name)
        node=tree.nodes.new('ShaderNodeBsdfPrincipled'); node.label=key.rsplit('/',1)[-1]
        for source,target in SURFACE_INPUTS.items():
            value=v(source)
            if value is not None and source in ('normal','coat_normal','tangent'):value=matrix_vector(tree,self.basis,value)
            if value is not None and target in node.inputs: feed(tree,node.inputs[target],value)
        color=vector(tree,'MULTIPLY',v('base_color'),combine(tree,[v('base')]*3))
        # Principled shares its tint between base, subsurface and transmission.
        for weight,tint in (('subsurface','subsurface_color'),('transmission','transmission_color')):
            if self.changed(key,tint) and ((key,weight) in self.links or v(weight)!=0.):
                self.warn(key,tint+' shares Principled Base Color; the BSDF mapping is approximate.')
                color=vector(tree,'ADD',color,vector(tree,'MULTIPLY',vector(tree,'SUBTRACT',v(tint),color),combine(tree,[v(weight)]*3)))
        feed(tree,node.inputs['Base Color'],color)
        feed(tree,node.inputs['Specular IOR Level'],scalar(tree,'MULTIPLY',v('specular'),.5))
        feed(tree,node.inputs['Alpha'],vector(tree,'DOT_PRODUCT',v('opacity'),(1/3,1/3,1/3)))
        if self.changed(key,'opacity'): self.warn(key,'Opacity is reduced to scalar alpha; colored transparency is approximate.')
        consumed=set(SURFACE_INPUTS)|{'base','base_color','specular','opacity','subsurface_color','transmission_color'}
        for name in SCHEMA[self.nodes[key]['type']]['inputs']:
            if name not in consumed and self.changed(key,name): self.warn(key,'Standard Surface '+name+' has no translated Principled equivalent.')
        return node.outputs['BSDF']

    def texture(self, key, kind, dtype):
        tree=self.tree; p=self.nodes[key].get('parameters',{}); v=lambda name,default=None:self.val(key,name,default)
        filename=v('file')
        if not isinstance(filename,str): raise TranslationError('Connected image filename is not supported at '+key)
        if not filename: return v('default')
        color_space=p.get('colorSpace:file',p.get('colorspace','auto' if dtype in ('color3','color4') else 'raw'))
        node=tree.nodes.new('ShaderNodeTexImage'); node.label=key.rsplit('/',1)[-1]
        node.image=image_file(filename,color_space)
        node.interpolation={'closest':'Closest','linear':'Linear','cubic':'Cubic'}.get(v('filtertype'),'Linear')
        coord=v('texcoord')
        if coord is None:
            uv=tree.nodes.new('ShaderNodeUVMap'); uv.uv_map='st'; coord=uv.outputs['UV']
        if kind=='tiledimage':
            tiling=component_math(tree,'DIVIDE',2,vector(tree,'MULTIPLY',v('uvtiling'),v('realworldtilesize')),v('realworldimagesize'))
            coord=vector(tree,'ADD',vector(tree,'MULTIPLY',coord,tiling),v('uvoffset'))
            node.extension='REPEAT'
        else:
            # Address axes independently: Blender has a single extension mode.
            # Explicit wrapping also implements MaterialX's mirrored repeat.
            modes=[v('uaddressmode'),v('vaddressmode')]
            parts=components(tree,coord,2); wrapped=[]; outside=[]
            for part,mode in zip(parts,modes):
                if mode=='periodic': part=scalar(tree,'FRACT',part)
                elif mode=='mirror': part=scalar(tree,'PINGPONG',part,1.)
                elif mode in ('clamp','constant'):
                    if mode=='constant': outside.append(scalar(tree,'MAXIMUM',scalar(tree,'LESS_THAN',part,0.),scalar(tree,'GREATER_THAN',part,1.)))
                    part=scalar(tree,'MINIMUM',scalar(tree,'MAXIMUM',part,0.),1.)
                else: raise TranslationError('Unsupported texture address mode '+str(mode)+' at '+key)
                wrapped.append(part)
            # UDIM coordinates must retain their tile indices.
            coord=coord if node.image.source=='TILED' else combine(tree,wrapped)
            node.extension='REPEAT' if modes==['periodic','periodic'] else 'EXTEND'
        if not isinstance(coord,bpy.types.NodeSocket):coord=combine(tree,components(tree,coord,2))
        feed(tree,node.inputs['Vector'],coord)
        result=node.outputs['Color']
        if dtype=='float': result=components(tree,result)[0]
        elif dtype=='vector2': result=combine(tree,components(tree,result,2))
        elif SIZES[dtype]==4: result=combine(tree,[*components(tree,result),node.outputs['Alpha']])
        if kind=='image' and outside and node.image.source!='TILED':
            mask=outside[0] if len(outside)==1 else scalar(tree,'MAXIMUM',*outside)
            result=component_math(tree,'ADD',SIZES[dtype],result,
                component_math(tree,'MULTIPLY',SIZES[dtype],component_math(tree,'SUBTRACT',SIZES[dtype],v('default'),result),mask))
        for name in ('layer','framerange','frameoffset'):
            if self.changed(key,name): self.warn(key,'Image '+name+' is not translated; use a resolved image file per frame.')
        return result


def network(tree, definition, basis=None):
    tree.nodes.clear()
    builder=Builder(tree,definition,basis)
    terminal=definition.get('terminal')
    if not terminal:
        candidates=[n['id'] for n in definition['nodes'] if n['type'].startswith(('ND_standard_surface_','ND_surfacematerial'))]
        if len(candidates)!=1: raise TranslationError('MaterialX surface terminal is ambiguous')
        terminal=candidates[0]
    result=builder.output(terminal)
    if not isinstance(result,bpy.types.NodeSocket) or result.type!='SHADER':
        raise TranslationError('MaterialX surface terminal is not a shader: '+terminal)
    output=tree.nodes.new('ShaderNodeOutputMaterial'); feed(tree,output.inputs['Surface'],result)
    if any(n['type'] in ('kma_hair','kma_fur_2') for n in definition['nodes']):
        # One Curves object can cover an entire animal or fruit. Its object
        # bounds are not a fiber's optical thickness; use each strand diameter.
        hair=tree.nodes.new('ShaderNodeHairInfo')
        tree.links.new(hair.outputs['Thickness'],output.inputs['Thickness'])
    return builder.warnings


def displacement(tree, definition, basis=None):
    """Add a MaterialX displacement graph to an existing material's output."""
    builder=Builder(tree,definition,basis)
    terminal=definition.get('terminal')
    result=builder.output(terminal)
    if not isinstance(result,bpy.types.NodeSocket) or result.type!='VECTOR':
        raise TranslationError('MaterialX displacement terminal is not a displacement shader: '+str(terminal))
    output=next((n for n in tree.nodes if n.bl_idname=='ShaderNodeOutputMaterial'),None) or tree.nodes.new('ShaderNodeOutputMaterial')
    tree.links.new(result,output.inputs['Displacement'])
    return builder.warnings
