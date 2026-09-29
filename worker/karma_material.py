"""Karma-specific graph nodes that can be represented by EEVEE shader nodes."""
import hashlib,json
import bpy
from shader_utils import feed,scalar,vector,combine,components,component_math

def ramp(builder,key,size):
    tree=builder.tree;p=builder.nodes[key].get('parameters',{});value=builder.val(key,'t',0.)
    prefix='f' if size==1 else 'v'
    keys=p.get(prefix+'keys',[0.,1.]);values=p.get(prefix+'values',[0.,1.] if size==1 else [[0.]*3,[1.]*3]);bases=p.get(prefix+'basis',['linear']*len(keys))
    if len(keys)!=len(values) or len(keys)!=len(bases) or not keys:raise ValueError('Invalid Karma ramp at '+key)
    if all(b in ('linear','constant') for b in bases):
        # Native math preserves step discontinuities and arbitrary key spacing.
        op=lambda operation,*args:component_math(tree,operation,size,*args)
        result=values[0]
        for i in range(len(keys)-1):
            if keys[i+1]<keys[i]:raise ValueError('Karma ramp keys must be ordered at '+key)
            if bases[i]=='constant' or keys[i+1]==keys[i]:
                t=scalar(tree,'SUBTRACT',1.,scalar(tree,'LESS_THAN',value,keys[i+1]))
            else:t=scalar(tree,'MINIMUM',scalar(tree,'MAXIMUM',scalar(tree,'DIVIDE',scalar(tree,'SUBTRACT',value,keys[i]),keys[i+1]-keys[i]),0.),1.)
            result=op('ADD',result,op('MULTIPLY',op('SUBTRACT',values[i+1],values[i]),t))
        return result
    samples=p.get('hde:ramp_samples')
    if not samples:raise ValueError('Karma spline ramp requires samples from the native Houdini adapter: '+key)
    digest=hashlib.sha256(json.dumps(samples,separators=(',',':')).encode()).hexdigest()
    image=next((im for im in bpy.data.images if im.get('hde_ramp')==digest),None)
    if image is None:
        image=bpy.data.images.new('Karma ramp '+digest[:12],width=len(samples),height=1,float_buffer=True)
        image.colorspace_settings.name='Non-Color';image.pixels.foreach_set([c for rgba in samples for c in rgba]);image.update();image['hde_ramp']=digest
    texture=tree.nodes.new('ShaderNodeTexImage');texture.image=image;texture.interpolation='Linear';texture.extension='EXTEND'
    x=scalar(tree,'ADD',scalar(tree,'MULTIPLY',value,(len(samples)-1)/len(samples)),.5/len(samples))
    feed(tree,texture.inputs['Vector'],combine(tree,[x,.5,0.]))
    builder.warn(key,'Spline ramp uses a 4096-sample lookup evaluated by Houdini; linear and constant segments use exact shader math.')
    return components(tree,texture.outputs['Color'])[0] if size==1 else texture.outputs['Color']

def hair(builder,key):
    tree=builder.tree;v=lambda name,default:builder.val(key,name,default)
    node=tree.nodes.new('ShaderNodeBsdfPrincipled');node.label='Karma hair / EEVEE fiber approximation'
    # Karma's fiber is a rough dielectric, not opaque diffuse paint. In
    # particular, zero-melanin white fur must transmit its underlying surface.
    # d'Eon absorption coefficients are the MaterialX/Houdini pigment model;
    # the conversion to a single Principled transmission lobe is approximate.
    melanin=scalar(tree,'MULTIPLY',-1.,scalar(tree,'LOGARITHM',scalar(tree,'MAXIMUM',scalar(tree,'SUBTRACT',1.,v('melanin',.4)),.0001),2.718281828459045))
    redness=scalar(tree,'MINIMUM',scalar(tree,'MAXIMUM',v('melaninRedness',.1),0.),1.)
    pigment=vector(tree,'ADD',(.419,.697,1.37),vector(tree,'MULTIPLY',(-.232,-.297,-.32),combine(tree,[redness]*3)))
    depth=scalar(tree,'MULTIPLY',melanin,v('thicknessScale',1.))
    absorption=components(tree,vector(tree,'MULTIPLY',pigment,combine(tree,[depth]*3)))
    transmission=combine(tree,[scalar(tree,'EXPONENT',scalar(tree,'MULTIPLY',-1.,a)) for a in absorption])
    color=vector(tree,'MULTIPLY',v('baseColor',[1.,1.,1.]),transmission)
    color=vector(tree,'MULTIPLY',color,v('tint',[1.,1.,1.]))
    color=vector(tree,'MULTIPLY',color,combine(tree,[v('base',1.)]*3))
    feed(tree,node.inputs['Base Color'],color)
    feed(tree,node.inputs['Roughness'],v('roughness',.3));feed(tree,node.inputs['IOR'],v('ior',1.55))
    feed(tree,node.inputs['Transmission Weight'],1.)
    feed(tree,node.inputs['Specular IOR Level'],scalar(tree,'MULTIPLY',.5,v('cuticle_reflectance',1.)))
    feed(tree,node.inputs['Coat Weight'],v('coat',0.))
    diffuse=tree.nodes.new('ShaderNodeBsdfDiffuse')
    feed(tree,diffuse.inputs['Color'],v('diffuseColor',[1.,1.,1.]))
    mix=tree.nodes.new('ShaderNodeMixShader');feed(tree,mix.inputs[0],v('diffuse',0.))
    tree.links.new(node.outputs[0],mix.inputs[1]);tree.links.new(diffuse.outputs[0],mix.inputs[2])
    builder.warn(key,'Karma hair/fur uses a transmissive EEVEE fiber approximation; separate longitudinal/azimuthal, medulla, cuticle-shift and strand-randomization lobes are not equivalent.')
    return mix.outputs[0]
