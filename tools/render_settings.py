"""EEVEE Render Settings LOP: Stage settings and native USD Render ROP output."""
import json
import os
from pathlib import Path
import re
import hou
from pxr import Gf, Sdf, UsdGeom, UsdRender

ROOT = Path(__file__).resolve().parents[1]
TYPE = 'eevee::render_settings::1.0'
SCHEMA = json.loads((ROOT/'houdini/eevee_settings.json').read_text())
PASSES = json.loads((ROOT/'houdini/eevee_passes.json').read_text())
COMMAND_EXPRESSION = "__import__('render_settings').render_command()"
# Blender's views for its sRGB display. 'ACES 2.0' is numerically the same as Houdini's
# default 'ACES 2.0 - SDR 100 nits (Rec.709)' view on 'sRGB - Display'.
VIEWS = ('ACES 2.0', 'ACES 1.3', 'AgX', 'Standard', 'Khronos PBR Neutral', 'Filmic', 'Filmic Log', 'False Color', 'Raw')
VIEW_HELP = ('Display transform baked into 8- and 16-bit image files (PNG, JPEG, TIFF, ...). ACES 2.0 matches '
             "Houdini's sRGB - Display / ACES 2.0 - SDR 100 nits (Rec.709) view. EXR files, the viewport and MPlay "
             "stay scene-linear and use Houdini's own display transform.")


def render_command():
    from hde_runtime import houdini_python, command_line
    if os.name == 'nt':
        # The ROP's command parser treats backslashes as escapes, so Windows paths
        # must use forward slashes; quotes keep paths with spaces intact.
        return '"{}" -E "{}"'.format(Path(houdini_python()).as_posix(), (ROOT/'tools/eevee_husk.py').as_posix())
    return command_line([houdini_python(), '-E', ROOT/'tools/eevee_husk.py'])


# Defaults that differ from Blender's, to match Karma: USD curves are round
# tubes of their widths, while Blender's default strands are thin lines.
DEFAULTS = {('render', 'hair_type'): 'CYLINDER'}
# Blender's Strand curve shape draws hair-thin lines that ignore USD curve widths, so
# it can never match Karma; scenes that stored it render as Cylinder (render_config).
HIDDEN_CHOICES = {('render', 'hair_type', 'STRAND')}


def parm_name(group, name):
    return group + '__' + name


def parameter(group, p):
    name, default = parm_name(group, p['name']), DEFAULTS.get((group, p['name']), p['default'])
    common = {'help': p['description']}
    if p['type'] == 'BOOLEAN':
        result = hou.ToggleParmTemplate(name, p['label'], default_value=default, **common)
    elif p['type'] == 'ENUM':
        choices = [i for i in p['items'] if (group, p['name'], i[0]) not in HIDDEN_CHOICES]
        result = hou.StringParmTemplate(name, p['label'], 1, default_value=(str(default),),
                 menu_items=[i[0] for i in choices], menu_labels=[i[1] for i in choices], **common)
    elif group == 'view_settings' and p['name'] == 'view_transform':
        result = hou.StringParmTemplate(name, p['label'], 1, default_value=(VIEWS[0],), menu_items=VIEWS,
                 menu_type=hou.menuType.StringReplace, help=VIEW_HELP)
    elif p['type'] == 'STRING':
        result = hou.StringParmTemplate(name, p['label'], 1, default_value=(str(default),), **common)
    else:
        cls = hou.IntParmTemplate if p['type'] == 'INT' else hou.FloatParmTemplate
        low, high = max(p.get('soft_min', 0), -100000), min(p.get('soft_max', 1), 100000)
        if group == 'eevee' and p['name'] in ('taa_samples', 'taa_render_samples'):
            low, high = 1, 256
        result = cls(name, p['label'], 1, default_value=(default,), min=low, max=high, **common)
    if group == 'image_settings' and p['name'] == 'file_format':
        result.setScriptCallback("import render_settings; render_settings.output_format_changed(kwargs['node'])")
        result.setScriptCallbackLanguage(hou.scriptLanguage.Python)
    return result


def output_format_changed(node):
    format = node.evalParm('image_settings__file_format')
    extensions = {'OPEN_EXR':'.exr', 'PNG':'.png', 'JPEG':'.jpg', 'TIFF':'.tif',
                  'AVIF':'.avif', 'WEBP':'.webp', 'BMP':'.bmp', 'CINEON':'.cin',
                  'DPX':'.dpx', 'IRIS':'.rgb', 'JPEG2000':'.jp2', 'HDR':'.hdr',
                  'TARGA':'.tga', 'TARGA_RAW':'.tga'}
    path = Path(node.parm('outputimage').unexpandedString())
    if path.suffix.lower() in extensions.values():
        node.parm('outputimage').set(str(path.with_suffix(extensions[format])))
    depth = node.parm('image_settings__color_depth')
    if format == 'OPEN_EXR' and depth.evalAsString() not in ('16','32'):
        depth.set('16')
    elif format not in ('OPEN_EXR','PNG','TIFF'):
        depth.set('8')
    elif format in ('PNG','TIFF') and depth.evalAsString() not in ('8','16'):
        depth.set('16')


def template_group():
    group = hou.ParmTemplateGroup()
    output = hou.FolderParmTemplate('output', 'Output')
    for p in (
        hou.StringParmTemplate('primpath', 'Render Settings Path', 1, default_value=('/Render/EEVEE',)),
        hou.StringParmTemplate('camera', 'Camera', 1, default_value=('/World/Camera',),
                              tags={'script_action': "import loputils; loputils.selectPrimsInParm(kwargs, False)", 'script_action_icon': 'BUTTONS_reselect'}),
        hou.IntParmTemplate('resolution', 'Resolution', 2, default_value=(1920, 1080), min=1, max=8192, naming_scheme=hou.parmNamingScheme.Base1),
        hou.IntParmTemplate('percentage', 'Resolution Scale (%)', 1, default_value=(100,), min=1, max=100),
        hou.FloatParmTemplate('pixelaspect', 'Pixel Aspect', 1, default_value=(1.,), min=.01, max=4),
        hou.ToggleParmTemplate('disable_dof', 'Disable Camera Depth of Field', default_value=False),
        hou.StringParmTemplate('outputimage', 'Output Picture', 1, default_value=('$HIP/render/eevee.$F4.png',), string_type=hou.stringParmType.FileReference),
        hou.MenuParmTemplate('trange', 'Valid Frame Range', ('off','normal'), ('Render Current Frame','Render Frame Range'), default_value=0),
        hou.FloatParmTemplate('f', 'Start / End / Increment', 3, default_value=(1, 100, 1), naming_scheme=hou.parmNamingScheme.Base1),
    ): output.addParmTemplate(p)
    for name, label, method in [('render_disk','Render to Disk','execute'),
                                ('render_mplay','Render to MPlay','renderpreview'),
                                ('render_background','Render to Disk in Background','executebackground')]:
        button = hou.ButtonParmTemplate(name, label)
        if method == 'renderpreview':
            button.setHelp('Render the Stage camera into MPlay using the resolution, render samples and frame range above. Output Picture is not written.')
        button.setScriptCallback("import render_settings; render_settings.render(kwargs['node'], '"+method+"')")
        button.setScriptCallbackLanguage(hou.scriptLanguage.Python)
        output.addParmTemplate(button)
    group.append(output)
    environment = hou.FolderParmTemplate('environment', 'Environment')
    environment.addParmTemplate(hou.StringParmTemplate('env_source', 'Environment Source', 1,
        default_value=('stage',), menu_items=('stage','override','off'),
        menu_labels=('Stage Dome Lights','Render Settings Override','Off'),
        help='Stage Dome Lights use the connected USD lights, including HDRI textures and rotation. Override replaces their environment with the controls below. Off disables environment lighting.'))
    for p in (
        hou.FloatParmTemplate('env_intensity', 'Intensity Multiplier', 1, default_value=(1.,), min=0., max=10.,
            help='Multiplies all Stage Dome Lights, or sets the override environment strength.'),
        hou.FloatParmTemplate('env_exposure', 'Exposure (Stops)', 1, default_value=(0.,), min=-10., max=10.),
        hou.FloatParmTemplate('env_color', 'Tint', 3, default_value=(1.,1.,1.), min=0., max=1.,
            look=hou.parmLook.ColorSquare, naming_scheme=hou.parmNamingScheme.RGBA),
    ):
        p.setConditional(hou.parmCondType.DisableWhen, '{ env_source == off }')
        environment.addParmTemplate(p)
    texture=hou.StringParmTemplate('env_texture', 'HDRI Texture', 1, default_value=('',),
        string_type=hou.stringParmType.FileReference,
        help='Latitude-longitude environment image for Override mode. An empty path uses the tint as a constant environment. For Stage mode, set the texture on the Dome Light.')
    texture.setConditional(hou.parmCondType.DisableWhen, '{ env_source != override }')
    environment.addParmTemplate(texture)
    rotation=hou.FloatParmTemplate('env_rotation', 'Rotation', 3, default_value=(0.,0.,0.), min=-180., max=180.,
        naming_scheme=hou.parmNamingScheme.XYZW, help='Override environment rotation in degrees around the Stage X, Y and Z axes. Stage mode uses the Dome Light transforms.')
    rotation.setConditional(hou.parmCondType.DisableWhen, '{ env_source != override }')
    environment.addParmTemplate(rotation)
    fallback=hou.ToggleParmTemplate('env_fallback', 'Default Light When No Dome Exists', default_value=True,
        help='Use the bridge\'s dim gray environment when the Stage contains no Dome Light. Disable for a black environment. Hidden or disabled domes never trigger this fallback.')
    fallback.setConditional(hou.parmCondType.DisableWhen, '{ env_source != stage }')
    environment.addParmTemplate(fallback)
    group.append(environment)
    passes = hou.FolderParmTemplate('render_passes', 'Render Passes')
    passes.addParmTemplate(hou.ToggleParmTemplate('output_multilayer', 'Multilayer EXR at Output Picture', default_value=False,
        help='Write beauty and enabled passes into the Output Picture EXR. Otherwise enabled passes are saved beside the beauty as <name>.passes.exr.'))
    previews = [('COMBINED', 'Combined')] + [(p['preview'],p['label']) for p in PASSES if p['preview']]
    passes.addParmTemplate(hou.StringParmTemplate('preview_pass', 'Viewport Pass', 1, default_value=('COMBINED',),
        menu_items=[p[0] for p in previews], menu_labels=[p[1] for p in previews],
        help='Choose the pass displayed live in the EEVEE viewport. The pass checkboxes below select rendered output; they do not change the live view.'))
    for p in PASSES:
        passes.addParmTemplate(hou.ToggleParmTemplate('pass__'+p['id'], p['label'], default_value=False))
    passes.addParmTemplate(hou.IntParmTemplate('cryptomatte_depth', 'Cryptomatte Levels', 1, default_value=(6,), min=2, max=16))
    passes.addParmTemplate(hou.FloatParmTemplate('mist_start', 'Mist Start', 1, default_value=(5.,), min=0, max=100))
    passes.addParmTemplate(hou.FloatParmTemplate('mist_depth', 'Mist Depth', 1, default_value=(25.,), min=.001, max=1000))
    passes.addParmTemplate(hou.FolderParmTemplate('shader_aov_count', 'Shader AOVs', folder_type=hou.folderType.MultiparmBlock,
        parm_templates=(hou.StringParmTemplate('shader_aov_name#', 'Name', 1),
                        hou.StringParmTemplate('shader_aov_type#', 'Type', 1, default_value=('COLOR',), menu_items=('COLOR','VALUE'), menu_labels=('Color','Value')))))
    group.append(passes)
    quality = hou.FolderParmTemplate('eevee', 'EEVEE')
    buckets = {
        'Sampling': [], 'Ray Tracing': [], 'Shadows': [], 'Indirect Lighting': [],
        'Volumes': [], 'Depth of Field': [], 'Motion Blur': [], 'Clamping': [], 'Advanced': []}
    for p in SCHEMA['groups']['eevee']:
        name = p['name']
        bucket = ('Sampling' if name.startswith(('taa_', 'use_taa')) else
                  'Ray Tracing' if name in ('use_raytracing','ray_tracing_method') else
                  'Shadows' if 'shadow' in name and 'volumetric' not in name else
                  'Indirect Lighting' if 'gi_' in name else
                  'Volumes' if 'volum' in name else
                  'Depth of Field' if 'bokeh' in name else
                  'Motion Blur' if 'motion_blur' in name else
                  'Clamping' if name.startswith(('clamp_', 'direct_light', 'indirect_light')) else 'Advanced')
        buckets[bucket].append(parameter('eevee', p))
    buckets['Ray Tracing'].extend(parameter('ray_tracing_options', p) for p in SCHEMA['groups']['ray_tracing_options'])
    for index, (label, parms) in enumerate(buckets.items()):
        if label == 'Indirect Lighting':
            parms.insert(0, hou.LabelParmTemplate('probe_support', 'Baked probe synchronization is not implemented yet.'))
        elif label == 'Volumes':
            parms.insert(0, hou.LabelParmTemplate('volume_support', 'OpenVDB and Houdini volumes are read from the connected Stage.'))
        elif label == 'Motion Blur':
            parms.insert(0, hou.LabelParmTemplate('motion_support', 'Stage shutter samples drive camera, object and deformation motion blur.'))
        quality.addParmTemplate(hou.FolderParmTemplate('quality_'+str(index), label, parm_templates=parms))
    group.append(quality)
    geometry=hou.FolderParmTemplate('geometry','Geometry')
    for name,label,default in [('viewport_subdivision','Viewport Subdivision Limit',1),('render_subdivision','Render Subdivision Limit',2)]:
        geometry.addParmTemplate(hou.IntParmTemplate('geometry__'+name,label,1,default_value=(default,),min=0,max=6,
            help='Maximum uniform subdivision level for USD subdivision surfaces. Zero displays the coarse mesh. An authored Hydra refinement level can request fewer subdivisions.'))
    geometry.addParmTemplate(hou.IntParmTemplate('geometry__subdivision_face_budget','Subdivision Faces per Mesh',1,
        default_value=(500000,),min=0,max=4000000,
        help='Reduce uniform subdivision to keep each mesh within this face budget. Does not remove base geometry. Zero disables this limit; dense meshes may require substantial GPU memory.'))
    group.append(geometry)
    textures=hou.FolderParmTemplate('textures','Textures')
    textures.addParmTemplate(hou.StringParmTemplate('texture_limit','Texture Size Limit',1,default_value=('1024',),
        menu_items=('8192','4096','2048','1024','512'),menu_labels=('8192','4096','2048','1024','512'),
        help='Largest texture side on the GPU, in pixels. Larger textures are scaled down keeping their aspect ratio; the files are not changed. A 4K texture at 1024 needs a sixteenth of the video memory.'))
    textures.addParmTemplate(hou.ToggleParmTemplate('texture_limit_viewport','Limit in Viewport',default_value=True,
        help='Apply the limit in the live EEVEE viewport.'))
    textures.addParmTemplate(hou.ToggleParmTemplate('texture_limit_render','Limit in Final Render',default_value=False,
        help='Apply the limit to renders to disk and MPlay.'))
    group.append(textures)
    instancing=hou.FolderParmTemplate('instancing','Instancing')
    instancing.addParmTemplate(hou.StringParmTemplate('instances_viewport_mode','Viewport Instances',1,default_value=('houdini',),
        menu_items=('houdini','manual'),menu_labels=('Match Houdini Viewport','Manual'),
        help='Share of point instances the live EEVEE viewport draws. Match Houdini Viewport uses the Houdini viewport\'s point instancing percentage (Display Options); Manual uses Viewport (%).'))
    instancing.addParmTemplate(hou.IntParmTemplate('instances_viewport','Viewport (%)',1,default_value=(100,),min=1,max=100,min_is_strict=True,max_is_strict=True,
        disable_when='{ instances_viewport_mode != manual }',
        help='Share of each point instancer\'s instances drawn in the live viewport. A fixed random subset is drawn.'))
    instancing.addParmTemplate(hou.IntParmTemplate('instances_navigate','While Navigating (%)',1,default_value=(10,),min=1,max=100,min_is_strict=True,max_is_strict=True,
        help='Share of instances drawn while the camera or scene changes, for instancers with 1000 or more instances. Blender processes every instance on each redraw; the settled image draws the viewport share.'))
    instancing.addParmTemplate(hou.IntParmTemplate('instances_render','Final Render (%)',1,default_value=(100,),min=1,max=100,min_is_strict=True,max_is_strict=True,
        help='Share of each point instancer\'s instances drawn in renders to disk and MPlay.'))
    group.append(instancing)
    for key, label in [('render','Film & Motion'), ('image_settings','Image Encoding'), ('view_settings','Output Color')]:
        folder = hou.FolderParmTemplate(key, label)
        for p in SCHEMA['groups'][key]: folder.addParmTemplate(parameter(key, p))
        group.append(folder)
    return group


def configuration(node):
    config = {group: {p['name']: node.evalParm(parm_name(group, p['name'])) for p in props}
              for group, props in SCHEMA['groups'].items()}
    raw = node.parm('outputimage').unexpandedString()
    tokens = []
    def protect(match):
        tokens.append(match.group())
        return '__EEVEE_FRAME_'+str(len(tokens)-1)+'__'
    expanded = hou.expandString(re.sub(r'\$FF|\$F\d*', protect, raw))
    for i, token in enumerate(tokens): expanded = expanded.replace('__EEVEE_FRAME_'+str(i)+'__', token)
    config['output'] = expanded
    if node.parm('geometry__viewport_subdivision'):
        config['geometry']={name:node.evalParm('geometry__'+name) for name in ('viewport_subdivision','render_subdivision','subdivision_face_budget')}
    # Older HIPs can still cook before their HDA definition is upgraded.
    if node.parm('env_source'):
        texture = node.evalParm('env_texture')
        if texture and not Path(texture).is_absolute():
            texture = str(Path(hou.expandString('$HIP')) / texture)
        config['environment'] = {
            'source':node.evalParm('env_source'), 'intensity':node.evalParm('env_intensity'),
            'exposure':node.evalParm('env_exposure'), 'color':list(node.parmTuple('env_color').eval()),
            'texture':texture, 'rotation':list(node.parmTuple('env_rotation').eval()),
            'fallback':bool(node.evalParm('env_fallback'))}
    # Older HIPs can still cook before their HDA definition is upgraded.
    if node.parm('texture_limit'):
        config['texture_limit'] = {'size':int(node.evalParm('texture_limit')),
                                   'viewport':bool(node.evalParm('texture_limit_viewport')),
                                   'render':bool(node.evalParm('texture_limit_render'))}
    # Older HIPs can still cook before their HDA definition is upgraded.
    if node.parm('instances_viewport_mode'):
        config['instancing'] = {'viewport_mode':node.evalParm('instances_viewport_mode'),
                                'viewport':node.evalParm('instances_viewport'),
                                'navigate':node.evalParm('instances_navigate'),
                                'render':node.evalParm('instances_render')}
    config['disable_dof'] = bool(node.evalParm('disable_dof'))
    config['output_multilayer'] = bool(node.evalParm('output_multilayer'))
    config['preview_pass'] = node.evalParm('preview_pass')
    config['passes'] = [p['id'] for p in PASSES if node.evalParm('pass__'+p['id'])]
    config['cryptomatte_depth'] = node.evalParm('cryptomatte_depth')
    config['mist'] = {'start': node.evalParm('mist_start'), 'depth': node.evalParm('mist_depth')}
    config['shader_aovs'] = [{'name':node.evalParm('shader_aov_name'+str(i)), 'type':node.evalParm('shader_aov_type'+str(i))}
                            for i in range(1,node.evalParm('shader_aov_count')+1)]
    names = [a['name'] for a in config['shader_aovs']]
    if len(names) != len(set(names)) or any(not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', name) for name in names):
        raise hou.NodeError('Shader AOV names must be unique names containing letters, digits and underscores.')
    if any(name == 'Combined' or name.startswith('Crypto') or name in {p['layer'] for p in PASSES} for name in names):
        raise hou.NodeError('Shader AOV names must not use a built-in render pass name.')
    config['fps'] = hou.fps()
    return config


def author(python_node):
    node = python_node.parent()
    config = configuration(node)
    stage = python_node.editableStage()
    path = Sdf.Path(node.evalParm('primpath'))
    if not path.IsAbsolutePath() or not str(path).startswith('/Render/'):
        raise hou.NodeError('Render settings must be an absolute prim path below /Render.')
    settings = UsdRender.Settings.Define(stage, path)
    scale = node.evalParm('percentage') / 100
    resolution = Gf.Vec2i(*(max(1, round(v*scale)) for v in node.evalParmTuple('resolution')))
    settings.CreateResolutionAttr(resolution)
    settings.CreatePixelAspectRatioAttr(node.evalParm('pixelaspect'))
    settings.CreateCameraRel().SetTargets([Sdf.Path(node.evalParm('camera'))])
    settings.CreateIncludedPurposesAttr(['default', 'render'])
    settings.CreateMaterialBindingPurposesAttr(['full', ''])
    settings.CreateAspectRatioConformPolicyAttr('expandAperture')
    settings.CreateDisableMotionBlurAttr(not bool(config['render']['use_motion_blur']))
    camera = UsdGeom.Camera(stage.GetPrimAtPath(node.evalParm('camera')))
    if camera:
        duration = config['render']['motion_blur_shutter'] if config['render']['use_motion_blur'] else 0.
        position = config['render']['motion_blur_position']
        start = 0. if position == 'START' else -duration if position == 'END' else -duration/2
        camera.CreateShutterOpenAttr(start)
        camera.CreateShutterCloseAttr(start+duration)
    settings.CreateDisableDepthOfFieldAttr(config['disable_dof'])
    config['up_axis'] = str(UsdGeom.GetStageUpAxis(stage))
    config['fps'] = stage.GetTimeCodesPerSecond()
    settings.GetPrim().CreateAttribute('eevee:config', Sdf.ValueTypeNames.String).Set(json.dumps(config, separators=(',', ':')))
    product = UsdRender.Product.Define(stage, path.AppendChild('Beauty'))
    product.CreateProductTypeAttr('raster')
    product.CreateProductNameAttr(config['output'])
    color = UsdRender.Var.Define(stage, path.AppendChild('Color'))
    color.CreateSourceNameAttr('color'); color.CreateSourceTypeAttr('raw'); color.CreateDataTypeAttr('color4f')
    color.GetPrim().CreateAttribute('driver:parameters:aov:husk:name', Sdf.ValueTypeNames.String).Set('C')
    variables = [color.GetPath()]
    for p in PASSES:
        if p['id'] not in config['passes'] or p['id'].startswith('crypto_'): continue
        var = UsdRender.Var.Define(stage, path.AppendChild('Pass_'+p['id']))
        var.CreateSourceNameAttr('eevee:'+p['id']); var.CreateSourceTypeAttr('raw'); var.CreateDataTypeAttr(p['type'])
        var.GetPrim().CreateAttribute('driver:parameters:aov:husk:name', Sdf.ValueTypeNames.String).Set(p['id'])
        variables.append(var.GetPath())
    product.CreateOrderedVarsRel().SetTargets(variables)
    settings.CreateProductsRel().SetTargets([product.GetPath()])
    # A LOP's edit layer is not the stage root. The connected settings prim is
    # selected by Hydra; the USD Render ROP explicitly selects this same path.


def render(node, method='execute'):
    camera = node.stage().GetPrimAtPath(node.evalParm('camera'))
    if not camera or not camera.IsA(UsdGeom.Camera):
        raise hou.Error('Choose a camera on the connected Stage before rendering.')
    if method != 'renderpreview':
        target = Path(node.evalParm('outputimage'))
        if node.evalParm('output_multilayer') and target.suffix.lower() != '.exr':
            raise hou.Error('Multilayer EXR requires an .exr Output Picture path.')
        target.parent.mkdir(parents=True, exist_ok=True)
    node.node('render_to_disk').parm(method).pressButton()


def install(output_path='/stage/EEVEE_OUT'):
    old = hou.node(output_path)
    if old is None:
        raise hou.Error('Missing output node: ' + output_path)
    saved = {}
    if old.type().name() == TYPE:
        for instance in old.type().instances():
            values = {}
            for parm in instance.parms():
                kind = parm.parmTemplate().type()
                if kind not in (hou.parmTemplateType.String, hou.parmTemplateType.Int, hou.parmTemplateType.Float,
                                hou.parmTemplateType.Menu, hou.parmTemplateType.Toggle): continue
                try:
                    keyframes = parm.keyframes()
                    if kind == hou.parmTemplateType.Menu:
                        token = parm.evalAsString()
                        value = list(parm.menuItems()).index(token) if token in parm.menuItems() else parm.evalAsInt()
                        if keyframes and parm.expression() in parm.menuItems():
                            value = list(parm.menuItems()).index(parm.expression())
                            keyframes = ()
                    else:
                        value = parm.unexpandedString() if kind == hou.parmTemplateType.String else parm.eval()
                    values[parm.name()] = (keyframes, value)
                except hou.Error: pass
            saved[instance.path()] = values
    library = ROOT/'houdini/otls/eevee_render_settings.hda'
    library.parent.mkdir(parents=True, exist_ok=True)
    if TYPE not in hou.lopNodeTypeCategory().nodeTypes():
        subnet = old.parent().createNode('subnet', '_eevee_settings_definition')
        script = subnet.createNode('pythonscript', 'author_render_settings')
        script.setInput(0, subnet.indirectInputs()[0])
        script.parm('python').set('import render_settings\nrender_settings.author(hou.pwd())\n')
        out = subnet.node('output0') or subnet.createNode('output', 'output0')
        out.setInput(0, script); out.setDisplayFlag(True)
        rop = subnet.createNode('usdrender_rop', 'render_to_disk')
        rop.setParms({'renderer': 'HdEeveeRendererPlugin', 'husk_gpu': True,
                      'loppath': '..',
                      'soho_foreground': True, 'allframesatonce': True})
        rop.parm('rendercommand').setExpression(COMMAND_EXPRESSION, hou.exprLanguage.Python)
        rop.parm('rendersettings').setExpression('chs("../primpath")', hou.exprLanguage.Hscript)
        rop.parm('trange').setExpression('ch("../trange")', hou.exprLanguage.Hscript)
        for i in range(1,4): rop.parm('f'+str(i)).setExpression('ch("../f'+str(i)+'")', hou.exprLanguage.Hscript)
        subnet.layoutChildren()
        asset = subnet.createDigitalAsset(name=TYPE, hda_file_name=str(library), description='EEVEE Render Settings', min_num_inputs=1, max_num_inputs=1)
        definition = asset.type().definition()
        definition.setParmTemplateGroup(template_group())
        definition.setIcon('ROP_usdrender')
        asset.destroy()
    if old.type().name() != TYPE:
        old = old.changeNodeType(TYPE, keep_name=True, keep_parms=False, keep_network_contents=False)
    else:
        old.type().definition().setParmTemplateGroup(template_group())
    try:
        portable = old.node('render_to_disk').parm('rendercommand').expression() == COMMAND_EXPRESSION
    except hou.OperationFailed:
        portable = False
    # One husk process and EEVEE worker for the whole frame range; a process per
    # frame restarts Blender and recompiles every shader on each frame.
    if (not portable or old.node('render_to_disk').parm('trange').expression() != 'ch("../trange")'
            or not old.node('render_to_disk').evalParm('allframesatonce')):
        old.allowEditingOfContents()
        old.node('render_to_disk').parm('rendercommand').setExpression(COMMAND_EXPRESSION, hou.exprLanguage.Python)
        old.node('render_to_disk').parm('trange').setExpression('ch("../trange")', hou.exprLanguage.Hscript)
        old.node('render_to_disk').parm('allframesatonce').set(True)
        old.type().definition().updateFromNode(old)
        old.type().definition().setParmTemplateGroup(template_group())
        old.matchCurrentDefinition()
    for path, values in saved.items():
        instance = hou.node(path)
        for name, (keyframes, value) in values.items():
            parm = instance.parm(name)
            if parm is not None:
                parm.deleteAllKeyframes()
                if keyframes: parm.setKeyframes(keyframes)
                else: parm.set(value)
    old.setColor(hou.Color(.25,.85,.45))
    old.setComment('EEVEE Render Settings\nConnected Houdini Stage → Blender EEVEE')
    old.type().definition().addSection('Help', '# EEVEE Render Settings\n\nConnect a Solaris Stage. Geometry, lights, cameras, materials and volume fields remain on that Stage.\n\nRender Passes selects native EEVEE passes and Cryptomatte. Enable Multilayer EXR for one EXR at Output Picture; otherwise selected passes are saved as a .passes.exr sidecar. Shader AOV names correspond to Output AOV nodes in Stage-authored EEVEE materials.\n\nFilm & Motion controls the shutter. Final renders use Stage shutter samples for camera, object, instance and stable-topology deformation blur. Animated SOPs need upstream shutter samples or v/velocities. Viewport blur uses EEVEE frame history during timeline changes. Volume files update per frame; within-shutter grid changes are not implemented.\n\nUse EEVEE Volume Material for Stage-owned volume shading. Baked probe synchronization remains unfinished.\n')
    old.setDisplayFlag(True); old.setCurrent(True, clear_all_selected=True)
    if hou.isUIAvailable(): hou.ui.triggerUpdate()
    return {'node': old.path(), 'type': old.type().name(), 'library': str(library),
            'sample_slider_ranges': {
                name: {'min': old.parm(name).parmTemplate().minValue(),
                       'max': old.parm(name).parmTemplate().maxValue(),
                       'value': old.evalParm(name)}
                for name in ('eevee__taa_samples', 'eevee__taa_render_samples')},
            'eevee_controls': sum(len(SCHEMA['groups'][key]) for key in ('eevee','ray_tracing_options'))}
