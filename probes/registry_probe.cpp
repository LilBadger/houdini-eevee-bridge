#include <pxr/pxr.h>
#include <pxr/base/plug/registry.h>
#include <pxr/imaging/hd/rendererPluginRegistry.h>
#include <pxr/imaging/hd/rendererPlugin.h>
#include <pxr/imaging/hd/renderDelegate.h>
#include <cstdio>
PXR_NAMESPACE_USING_DIRECTIVE
int main(int argc,char **argv) {
    if(argc!=2) return 2;
    PlugRegistry::GetInstance().RegisterPlugins(argv[1]);
    auto &registry=HdRendererPluginRegistry::GetInstance();
    HfPluginDescVector desc;
    registry.GetPluginDescs(&desc);
    for(auto &d:desc) fprintf(stderr,"Plugin: %s (%s)\n",d.id.GetText(),d.displayName.c_str());
    auto plugin=registry.GetRendererPlugin(TfToken("HdEeveeRendererPlugin"));
    if(!plugin) {fprintf(stderr,"Factory lookup returned null\n");return 1;}
    auto delegate=plugin->CreateRenderDelegate();
    fprintf(stderr,"Delegate created: %p\n",(void*)delegate);
    plugin->DeleteRenderDelegate(delegate);
    registry.ReleasePlugin(plugin);
    return 0;
}
