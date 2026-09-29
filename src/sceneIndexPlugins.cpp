// Scene index plugins applied to this renderer's Hydra render index.
#include <pxr/imaging/hd/retainedDataSource.h>
#include <pxr/imaging/hd/sceneIndexPlugin.h>
#include <pxr/imaging/hd/sceneIndexPluginRegistry.h>
#include <pxr/imaging/hd/tokens.h>
#include <pxr/imaging/hdsi/implicitSurfaceSceneIndex.h>

PXR_NAMESPACE_OPEN_SCOPE

namespace {
// Must match "displayName" of HdEeveeRendererPlugin in plugInfo.json.
const char *const kRendererDisplayName = "EEVEE Bridge (Prototype)";
const TfToken kImplicitSurfacesPlugin("HdEevee_ImplicitSurfaceSceneIndexPlugin");
} // namespace

/// USD implicit gprims (Sphere, Cube, Cone, Cylinder, Capsule, Plane) have no
/// Blender equivalent; tessellate them to meshes as Storm and HdPrman do.
class HdEevee_ImplicitSurfaceSceneIndexPlugin final : public HdSceneIndexPlugin {
protected:
    HdSceneIndexBaseRefPtr _AppendSceneIndex(const HdSceneIndexBaseRefPtr &input,
                                             const HdContainerDataSourceHandle &args) override {
        return HdsiImplicitSurfaceSceneIndex::New(input, args);
    }
};

TF_REGISTRY_FUNCTION(TfType) {
    HdSceneIndexPluginRegistry::Define<HdEevee_ImplicitSurfaceSceneIndexPlugin>();
}

TF_REGISTRY_FUNCTION(HdSceneIndexPlugin) {
    const HdDataSourceBaseHandle toMesh =
        HdRetainedTypedSampledDataSource<TfToken>::New(HdsiImplicitSurfaceSceneIndexTokens->toMesh);
    const HdContainerDataSourceHandle args = HdRetainedContainerDataSource::New(
        HdPrimTypeTokens->sphere, toMesh, HdPrimTypeTokens->cube, toMesh,
        HdPrimTypeTokens->cone, toMesh, HdPrimTypeTokens->cylinder, toMesh,
        HdPrimTypeTokens->capsule, toMesh, HdPrimTypeTokens->plane, toMesh);
    HdSceneIndexPluginRegistry::GetInstance().RegisterSceneIndexForRenderer(
        kRendererDisplayName, kImplicitSurfacesPlugin, args, 0,
        HdSceneIndexPluginRegistry::InsertionOrderAtStart);
}

PXR_NAMESPACE_CLOSE_SCOPE
