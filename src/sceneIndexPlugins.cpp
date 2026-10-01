// Scene index plugins applied to this renderer's Hydra render index.
#include <pxr/imaging/hd/retainedDataSource.h>
#include <pxr/imaging/hd/sceneIndexPlugin.h>
#include <pxr/imaging/hd/sceneIndexPluginRegistry.h>
#include <pxr/imaging/hd/tokens.h>
#include <pxr/imaging/hdsi/implicitSurfaceSceneIndex.h>
#include <pxr/imaging/hdsi/lightLinkingSceneIndex.h>
#include <pxr/imaging/hdsi/nurbsApproximatingSceneIndex.h>

PXR_NAMESPACE_OPEN_SCOPE

namespace {
// Must match "displayName" of HdEeveeRendererPlugin in plugInfo.json.
const char *const kRendererDisplayName = "EEVEE Bridge (Prototype)";
const TfToken kImplicitSurfacesPlugin("HdEevee_ImplicitSurfaceSceneIndexPlugin");
const TfToken kLightLinkingPlugin("HdEevee_LightLinkingSceneIndexPlugin");
const TfToken kNurbsPlugin("HdEevee_NurbsApproximatingSceneIndexPlugin");
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

/// USD NURBS patches and curves have no EEVEE equivalent; approximate them with
/// meshes and basis curves.
class HdEevee_NurbsApproximatingSceneIndexPlugin final : public HdSceneIndexPlugin {
protected:
    HdSceneIndexBaseRefPtr _AppendSceneIndex(const HdSceneIndexBaseRefPtr &input,
                                             const HdContainerDataSourceHandle &) override {
        return HdsiNurbsApproximatingSceneIndex::New(input);
    }
};

/// USD light and shadow linking: turns the lights' link collections into
/// category tokens on lights and geometry, which the delegate reads through
/// HdSceneDelegate::GetCategories and the lights' lightLink/shadowLink params.
class HdEevee_LightLinkingSceneIndexPlugin final : public HdSceneIndexPlugin {
protected:
    HdSceneIndexBaseRefPtr _AppendSceneIndex(const HdSceneIndexBaseRefPtr &input,
                                             const HdContainerDataSourceHandle &args) override {
        return HdsiLightLinkingSceneIndex::New(input, args);
    }
};

TF_REGISTRY_FUNCTION(TfType) {
    HdSceneIndexPluginRegistry::Define<HdEevee_ImplicitSurfaceSceneIndexPlugin>();
    HdSceneIndexPluginRegistry::Define<HdEevee_LightLinkingSceneIndexPlugin>();
    HdSceneIndexPluginRegistry::Define<HdEevee_NurbsApproximatingSceneIndexPlugin>();
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
    HdSceneIndexPluginRegistry::GetInstance().RegisterSceneIndexForRenderer(
        kRendererDisplayName, kNurbsPlugin, nullptr, 0,
        HdSceneIndexPluginRegistry::InsertionOrderAtStart);

    // Phase 1: after implicit shapes have become meshes, so they are geometry too.
    using Tokens = HdRetainedTypedSampledDataSource<VtArray<TfToken>>;
    const HdContainerDataSourceHandle linking = HdRetainedContainerDataSource::New(
        HdsiLightLinkingSceneIndexTokens->lightPrimTypes, Tokens::New(VtArray<TfToken>{
            HdPrimTypeTokens->sphereLight, HdPrimTypeTokens->diskLight, HdPrimTypeTokens->rectLight,
            HdPrimTypeTokens->distantLight, HdPrimTypeTokens->cylinderLight, HdPrimTypeTokens->domeLight}),
        HdsiLightLinkingSceneIndexTokens->geometryPrimTypes, Tokens::New(VtArray<TfToken>{
            HdPrimTypeTokens->mesh, HdPrimTypeTokens->basisCurves, HdPrimTypeTokens->points, HdPrimTypeTokens->volume,
            HdPrimTypeTokens->sphere, HdPrimTypeTokens->cube, HdPrimTypeTokens->cone, HdPrimTypeTokens->cylinder,
            HdPrimTypeTokens->capsule, HdPrimTypeTokens->plane}));
    HdSceneIndexPluginRegistry::GetInstance().RegisterSceneIndexForRenderer(
        kRendererDisplayName, kLightLinkingPlugin, linking, 1,
        HdSceneIndexPluginRegistry::InsertionOrderAtStart);
}

PXR_NAMESPACE_CLOSE_SCOPE
