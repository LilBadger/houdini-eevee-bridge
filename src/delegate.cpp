// Hydra render delegate: registration, render pass and settings.
#include "buffer.h"
#include "prims.h"
#include "renderer.h"
#include <pxr/base/tf/registryManager.h>
#include <pxr/base/tf/type.h>
#include <pxr/imaging/hd/aov.h>
#include <pxr/imaging/hd/renderIndex.h>
#include <pxr/imaging/hd/renderPass.h>
#include <pxr/imaging/hd/renderPassState.h>
#include <pxr/imaging/hd/rendererPlugin.h>
#include <pxr/imaging/hd/rendererPluginRegistry.h>
#include <pxr/imaging/hd/resourceRegistry.h>
#include <pxr/imaging/hd/tokens.h>
#include <cstdio>
#include <fstream>

PXR_NAMESPACE_OPEN_SCOPE
namespace hdEevee {
namespace {
std::string PassId(std::string name) {
    if (name.rfind("eevee:", 0) == 0) name = name.substr(6);
    return name;
}

HdFormat PassFormat(const std::string &name) {
    const auto id = PassId(name);
    if (id == "z" || id == "mist") return HdFormatFloat32;
    if (id == "vector" || id == "transparent") return HdFormatFloat32Vec4;
    static const std::set<std::string> vectors = {"normal", "position", "diffuse_light", "diffuse_color", "specular_light",
        "specular_color", "volume_light", "emission", "environment", "shadow", "ao"};
    return vectors.count(id) ? HdFormatFloat32Vec3 : HdFormatInvalid;
}

bool IsColor(const TfToken &name) { return name == HdAovTokens->color || name == TfToken("C"); }

HdFormat ColorFormat() {
    const char *choice = std::getenv("HDEEVEE_COLOR_FORMAT");
    return choice && std::string(choice) == "float" ? HdFormatFloat32Vec4 : HdFormatFloat16Vec4;
}
} // namespace

class EeveePass final : public HdRenderPass {
public:
    EeveePass(HdRenderIndex *index, const HdRprimCollection &collection, BridgeState *state,
              Renderer *renderer, HdRenderDelegate *owner)
        : HdRenderPass(index, collection), _state(state), _renderer(renderer), _owner(owner) {}
    bool IsConverged() const override { return _converged; }

protected:
    void _Execute(const HdRenderPassStateSharedPtr &pass, const TfTokenVector&) override;

private:
    EeveeBuffer *Resolve(const HdRenderPassAovBinding &binding) const {
        return binding.renderBuffer ? dynamic_cast<EeveeBuffer*>(binding.renderBuffer)
            : dynamic_cast<EeveeBuffer*>(GetRenderIndex()->GetBprim(HdPrimTypeTokens->renderBuffer, binding.renderBufferId));
    }
    void Present(const HdRenderPassAovBindingVector &bindings, const Frame &frame, bool converged);

    BridgeState *_state;
    Renderer *_renderer;
    HdRenderDelegate *_owner;
    bool _converged = false;
    int _emptyPasses = 0;
    uint64_t _presented = 0;
    std::map<EeveeBuffer*, uint64_t> _generations;
};

void EeveePass::Present(const HdRenderPassAovBindingVector &bindings, const Frame &frame, bool converged) {
    for (const auto &binding : bindings) {
        EeveeBuffer *buffer = Resolve(binding);
        if (!buffer) continue;
        const TfToken &name = binding.aovName;
        const char *plane = IsColor(name) ? "color" : name == HdAovTokens->depth ? "depth" :
            name == HdAovTokens->primId ? "primId" : name == HdAovTokens->instanceId ? "instanceId" : nullptr;
        auto it = frame.planes.find(plane ? std::string(plane) : name.GetString());
        if (it != frame.planes.end()) buffer->Present(it->second);
        else if (name == HdAovTokens->elementId || name == HdAovTokens->primId || name == HdAovTokens->instanceId)
            buffer->Fill(-1.f);
        else if (name == HdAovTokens->depth)
            buffer->Fill(1.f);   // navigation frames carry no depth: nothing occludes guides
        buffer->SetConverged(converged);
        _generations[buffer] = buffer->Generation();
    }
}

void EeveePass::_Execute(const HdRenderPassStateSharedPtr &pass, const TfTokenVector&) {
    const auto &bindings = pass->GetAovBindings();
    EeveeBuffer *color = nullptr;
    for (const auto &b : bindings) if (IsColor(b.aovName)) color = Resolve(b);
    if (!color) for (const auto &b : bindings) {
        EeveeBuffer *buffer = Resolve(b);
        if (buffer && (buffer->GetFormat() == HdFormatFloat32Vec4 || buffer->GetFormat() == HdFormatFloat16Vec4)) { color = buffer; break; }
    }
    if (!color || !color->GetWidth() || !color->GetHeight()) {
        if (++_emptyPasses <= 3) {
            fprintf(stderr, "[EEVEE] Waiting for color buffer (%zu bindings)\n", bindings.size());
            for (const auto &b : bindings) fprintf(stderr, "[EEVEE] AOV: %s buffer=%p\n", b.aovName.GetText(), static_cast<void*>(Resolve(b)));
        }
        _converged = true;
        return;
    }
    ViewRequest request;
    request.width = color->GetWidth();
    request.height = color->GetHeight();
    request.colorFormat = color->GetFormat();
    request.view = pass->GetWorldToViewMatrix();
    request.projection = pass->GetProjectionMatrix();
    const Json config = _state->ActiveConfig();
    request.config = config;
    const Json eevee = config.value("eevee", Json::object());
    request.targetSamples = eevee.contains("taa_samples") && !_state->FinalRender()
        ? eevee["taa_samples"].get<int>() : _owner->GetRenderSetting<int>(TfToken("eeveeSamples"), 16);
    request.raytracing = _owner->GetRenderSetting<bool>(TfToken("eeveeRaytracing"), true);
    request.upAxis = _owner->GetRenderSetting<int>(TfToken("eeveeUpAxis"), 0) == 1 ? "Z" : "Y";
    // Menu index: 0 full, 1 half, 2 third, 3 quarter resolution while navigating.
    request.navigationScale = std::clamp(_owner->GetRenderSetting<int>(TfToken("eeveeNavigationScale"), 1), 0, 3) + 1;
    request.frame = _owner->GetRenderSetting<double>(TfToken("houdini:frame"), 1.0);
    // Menu index: 0 default (HDEEVEE_TEXTURE_LIMIT, else 2048), then 8192 ... 1024, 5 full resolution.
    // EEVEE Render Settings' texture options, when present in eevee:config, take precedence.
    static const int kTextureLimits[] = {-1, 8192, 4096, 2048, 1024, 0};
    request.textureLimit = kTextureLimits[std::clamp(_owner->GetRenderSetting<int>(TfToken("eeveeTextureLimit"), 0), 0, 5)];
    if (request.textureLimit < 0) {
        const char *studio = std::getenv("HDEEVEE_TEXTURE_LIMIT");
        request.textureLimit = studio && *studio ? std::max(0, std::atoi(studio)) : 2048;
    }
    request.limitSurface = _owner->GetRenderSetting<int>(TfToken("eeveeSubdivisionAccuracy"), 0) == 1;
    if (const HdCamera *camera = pass->GetCamera()) {
        // Lens values are in world units; depth of field is on when F-Stop > 0.
        request.camera = {{"fstop", camera->GetFStop()}, {"focus_distance", camera->GetFocusDistance()},
            {"focal_length", camera->GetFocalLength()}, {"horizontal_aperture", camera->GetHorizontalAperture()},
            {"dof_aspect", camera->GetDofAspect()},
            {"clip_start", camera->GetClippingRange().GetMin()}, {"clip_end", camera->GetClippingRange().GetMax()}};
        if (const auto *sampled = dynamic_cast<const EeveeCamera*>(camera)) request.cameraSamples = sampled->Samples();
    }
    for (const auto &b : bindings) {
        if (b.aovName == HdAovTokens->depth) request.depth = true;
        else if (b.aovName == HdAovTokens->primId || b.aovName == HdAovTokens->instanceId) request.ids = true;
        else if (PassFormat(b.aovName.GetString()) != HdFormatInvalid &&
                 (_state->FinalRender() || PassId(b.aovName.GetString()) != "vector")) {
            EeveeBuffer *buffer = Resolve(b);
            request.aovs.emplace_back(b.aovName.GetString(),
                                      buffer ? buffer->GetFormat() : PassFormat(b.aovName.GetString()));
        }
    }
    if (_state->FinalRender()) {
        // Disk rendering (husk) waits for the complete final frame.
        try {
            auto frame = _renderer->RenderFinal(request);
            Present(bindings, *frame, true);
        } catch (const std::exception &exc) {
            // The output wrapper requires a successful EEVEE image manifest;
            // an error buffer is never published as the requested output.
            fprintf(stderr, "[EEVEE] %s\n", exc.what());
            for (const auto &b : bindings) if (EeveeBuffer *buffer = Resolve(b)) { buffer->Fill(0.f); buffer->SetConverged(true); }
        }
        _converged = true;
        return;
    }
    _renderer->Post(request);
    std::shared_ptr<const Frame> frame;
    bool converged = false;
    _renderer->Snapshot(&frame, &converged);
    if (frame) {
        bool reallocated = false;
        for (const auto &b : bindings)
            if (EeveeBuffer *buffer = Resolve(b)) {
                auto it = _generations.find(buffer);
                reallocated = reallocated || it == _generations.end() || it->second != buffer->Generation();
            }
        if (frame->serial != _presented || reallocated) {
            Present(bindings, *frame, converged);
            _presented = frame->serial;
        } else {
            for (const auto &b : bindings) if (EeveeBuffer *buffer = Resolve(b)) buffer->SetConverged(converged);
        }
    }
    _converged = converged && frame && _presented == frame->serial;
}

class EeveeDelegate final : public HdRenderDelegate {
public:
    explicit EeveeDelegate(const HdRenderSettingsMap &settings)
        : HdRenderDelegate(settings), _registry(std::make_shared<HdResourceRegistry>()),
          _renderer(std::make_unique<Renderer>(&_state)) {
        fprintf(stderr, "[EEVEE] Native Hydra delegate created (0.7.2)\n");
        auto it = settings.find(TfToken("eevee:config"));
        if (it != settings.end() && it->second.IsHolding<std::string>() && !it->second.UncheckedGet<std::string>().empty()) {
            const Json config = Json::parse(it->second.UncheckedGet<std::string>(), nullptr, false);
            if (config.is_object()) _state.SetDelegateConfig(&config);
        }
    }
    ~EeveeDelegate() override { _renderer.reset(); }

    const TfTokenVector &GetSupportedRprimTypes() const override {
        static TfTokenVector t = {HdPrimTypeTokens->mesh, HdPrimTypeTokens->volume, HdPrimTypeTokens->basisCurves,
                                  HdPrimTypeTokens->points};
        return t;
    }
    const TfTokenVector &GetSupportedSprimTypes() const override {
        static TfTokenVector t = {HdPrimTypeTokens->camera, HdPrimTypeTokens->material, HdPrimTypeTokens->rectLight,
            HdPrimTypeTokens->diskLight, HdPrimTypeTokens->distantLight, HdPrimTypeTokens->sphereLight,
            HdPrimTypeTokens->cylinderLight, HdPrimTypeTokens->domeLight};
        return t;
    }
    const TfTokenVector &GetSupportedBprimTypes() const override {
        static TfTokenVector t = {HdPrimTypeTokens->renderBuffer, HdPrimTypeTokens->renderSettings,
            TfToken("openvdbAsset"), TfToken("houdiniFieldAsset")};
        return t;
    }
    HdRenderParam *GetRenderParam() const override { return const_cast<BridgeState*>(&_state); }
    HdResourceRegistrySharedPtr GetResourceRegistry() const override { return _registry; }
    HdRenderPassSharedPtr CreateRenderPass(HdRenderIndex *index, const HdRprimCollection &collection) override {
        return std::make_shared<EeveePass>(index, collection, &_state, _renderer.get(), this);
    }
    HdInstancer *CreateInstancer(HdSceneDelegate *d, const SdfPath &id) override { return new EeveeInstancer(d, id, &_state); }
    void DestroyInstancer(HdInstancer *p) override { delete p; }
    HdRprim *CreateRprim(const TfToken &t, const SdfPath &id) override {
        if (t == HdPrimTypeTokens->mesh) return new EeveeMesh(id, &_state);
        if (t == HdPrimTypeTokens->basisCurves) return new EeveeCurves(id, &_state);
        if (t == HdPrimTypeTokens->volume) return new EeveeVolume(id, &_state);
        if (t == HdPrimTypeTokens->points) return new EeveePoints(id, &_state);
        return nullptr;
    }
    void DestroyRprim(HdRprim *p) override { delete p; }
    HdSprim *CreateSprim(const TfToken &t, const SdfPath &id) override {
        if (t == HdPrimTypeTokens->camera) return new EeveeCamera(id, &_state);
        if (t == HdPrimTypeTokens->material) return new EeveeMaterial(id, &_state);
        return new EeveeLight(id, t, &_state);
    }
    // Fallback prims have empty paths; their destructors queue nothing.
    HdSprim *CreateFallbackSprim(const TfToken &t) override { return CreateSprim(t, SdfPath::EmptyPath()); }
    void DestroySprim(HdSprim *p) override { delete p; }
    HdBprim *CreateBprim(const TfToken &t, const SdfPath &id) override {
        if (t == HdPrimTypeTokens->renderBuffer) return new EeveeBuffer(id);
        if (t == HdPrimTypeTokens->renderSettings) return new EeveeRenderSettings(id, &_state);
        if (t == TfToken("openvdbAsset") || t == TfToken("houdiniFieldAsset")) return new EeveeField(id, &_state);
        return nullptr;
    }
    HdBprim *CreateFallbackBprim(const TfToken &t) override { return CreateBprim(t, SdfPath::EmptyPath()); }
    void DestroyBprim(HdBprim *p) override { delete p; }
    void CommitResources(HdChangeTracker*) override {}

    HdAovDescriptor GetDefaultAovDescriptor(const TfToken &name) const override {
        if (IsColor(name)) return HdAovDescriptor(ColorFormat(), false, VtValue(GfVec4f(0, 0, 0, 1)));
        const auto format = PassFormat(name.GetString());
        if (format == HdFormatFloat32) return HdAovDescriptor(format, false, VtValue(0.f));
        if (format == HdFormatFloat32Vec3) return HdAovDescriptor(format, false, VtValue(GfVec3f(0)));
        if (format == HdFormatFloat32Vec4) return HdAovDescriptor(format, false, VtValue(GfVec4f(0)));
        // Depth is window-space [0, 1] from the pick/depth pass.
        if (name == HdAovTokens->depth) return HdAovDescriptor(HdFormatFloat32, false, VtValue(1.f));
        if (name == HdAovTokens->primId || name == HdAovTokens->instanceId || name == HdAovTokens->elementId)
            return HdAovDescriptor(HdFormatInt32, false, VtValue(-1));
        return HdAovDescriptor();
    }
    TfToken GetMaterialBindingPurpose() const override { return HdTokens->full; }
    TfTokenVector GetMaterialRenderContexts() const override { return {TfToken("eevee"), TfToken("mtlx"), TfToken("kma"), TfToken()}; }
    TfTokenVector GetShadingSystems() const override { return {TfToken("mtlx"), TfToken("glslfx")}; }
    TfTokenVector GetShaderSourceTypes() const override { return GetShadingSystems(); }
    TfTokenVector GetRenderSettingsNamespaces() const override { return {TfToken("eevee"), TfToken("driver"), TfToken("houdini")}; }
    HdRenderSettingDescriptorList GetRenderSettingDescriptors() const override {
        return {{"EEVEE Samples", TfToken("eeveeSamples"), VtValue(16)},
                {"EEVEE Ray Tracing", TfToken("eeveeRaytracing"), VtValue(true)},
                {"Scene Up Axis (0=Y, 1=Z)", TfToken("eeveeUpAxis"), VtValue(0)},
                {"Navigation Resolution (0 full, 1 half, 2 third, 3 quarter)", TfToken("eeveeNavigationScale"), VtValue(1)},
                {"Texture Size Limit (0 default 2048, 1 8192, 2 4096, 3 2048, 4 1024, 5 full)", TfToken("eeveeTextureLimit"), VtValue(0)},
                {"Subdivision Surfaces (0 fast cage, 1 exact limit surface)", TfToken("eeveeSubdivisionAccuracy"), VtValue(0)},
                {"EEVEE Stage Configuration", TfToken("eevee:config"), VtValue(std::string())}};
    }
    void SetRenderSetting(const TfToken &key, const VtValue &value) override {
        if (GetRenderSetting(key) == value) return;
        if (std::getenv("HDEEVEE_TRACE")) {
            std::ofstream trace(hde::environmentPath("HDEEVEE_TRACE").concat(".settings"), std::ios::app);
            trace << Json({{"event", "delegate_setting"}, {"key", key.GetString()}, {"value", ValueJson(value)}}).dump() << '\n';
        }
        HdRenderDelegate::SetRenderSetting(key, value);
        if (key == TfToken("eevee:config")) {
            const std::string text = value.IsHolding<std::string>() ? value.UncheckedGet<std::string>() : std::string();
            const Json config = text.empty() ? Json() : Json::parse(text, nullptr, false);
            _state.SetDelegateConfig(config.is_object() ? &config : nullptr);
        } else {
            _state.Touch();
        }
    }
    VtDictionary GetRenderStats() const override {
        VtDictionary stats = _renderer->Stats();
        stats["renderer"] = VtValue(std::string("Blender EEVEE"));
        return stats;
    }
    bool IsPauseSupported() const override { return true; }
    bool IsPaused() const override { return _renderer->Paused(); }
    bool Pause() override { _renderer->SetPaused(true); return true; }
    bool Resume() override { _renderer->SetPaused(false); return true; }

private:
    BridgeState _state;
    HdResourceRegistrySharedPtr _registry;
    std::unique_ptr<Renderer> _renderer;
};
} // namespace hdEevee

class HdEeveeRendererPlugin final : public HdRendererPlugin {
public:
    HdRenderDelegate *CreateRenderDelegate() override { return new hdEevee::EeveeDelegate({}); }
    HdRenderDelegate *CreateRenderDelegate(const HdRenderSettingsMap &s) override { return new hdEevee::EeveeDelegate(s); }
    void DeleteRenderDelegate(HdRenderDelegate *d) override { delete d; }
    bool IsSupported(bool) const override { return true; }
    bool IsSupported(const HdRendererCreateArgs&, std::string*) const override { return true; }
};

TF_REGISTRY_FUNCTION(TfType) {
    HdRendererPluginRegistry::Define<HdEeveeRendererPlugin>();
}
PXR_NAMESPACE_CLOSE_SCOPE
