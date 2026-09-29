// Headless Hydra host that drives a render delegate like a Houdini viewport.
//
// It loads a USD stage through UsdImagingDelegate, renders through the
// delegate's render pass at a steady 60 Hz "UI" cadence, orbits the camera,
// edits a material and checks picking IDs. The report measures how long each
// render-pass call blocks the calling (UI) thread and how many new frames were
// presented. It works with any Hydra delegate, so old and new plugins can be
// compared under identical conditions.
#include <pxr/pxr.h>
#include <pxr/base/gf/half.h>
#include <pxr/base/gf/rect2i.h>
#include <pxr/base/plug/registry.h>
#include <pxr/base/tf/token.h>
#include <pxr/usd/usd/editContext.h>
#include <pxr/usd/usd/stage.h>
#include <pxr/base/gf/camera.h>
#include <pxr/base/gf/rotation.h>
#include <pxr/usd/usdGeom/camera.h>
#include <pxr/usd/usdGeom/xformable.h>
#include <pxr/usd/usdGeom/xformCache.h>
#include <pxr/usd/usdShade/shader.h>
#include <pxr/usdImaging/usdImaging/delegate.h>
#include <pxr/imaging/cameraUtil/conformWindow.h>
#include <pxr/imaging/cameraUtil/framing.h>
#include <pxr/base/gf/frustum.h>
#include <pxr/imaging/hd/camera.h>
#include <pxr/imaging/hd/engine.h>
#include <pxr/imaging/hd/renderBuffer.h>
#include <pxr/imaging/hd/renderDelegate.h>
#include <pxr/imaging/hd/renderIndex.h>
#include <pxr/imaging/hd/renderPass.h>
#include <pxr/imaging/hd/renderPassState.h>
#include <pxr/imaging/hd/rendererPlugin.h>
#include <pxr/imaging/hd/rendererPluginRegistry.h>
#include <pxr/imaging/hd/rprimCollection.h>
#include <pxr/imaging/hd/task.h>
#include <pxr/imaging/hd/tokens.h>
#include <nlohmann/json.hpp>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <map>
#include <thread>
#ifdef _WIN32
#include <windows.h>
#include <timeapi.h>
#endif

PXR_NAMESPACE_USING_DIRECTIVE
using Json = nlohmann::json;
using Clock = std::chrono::steady_clock;

namespace {
double Ms(Clock::duration d) { return std::chrono::duration<double, std::milli>(d).count(); }

class PassTask final : public HdTask {
public:
    PassTask(HdRenderPassSharedPtr pass, HdRenderPassStateSharedPtr state)
        : HdTask(SdfPath("/harness/task")), _pass(pass), _state(state) {}
    void Sync(HdSceneDelegate*, HdTaskContext*, HdDirtyBits *bits) override { _pass->Sync(); *bits = 0; }
    void Prepare(HdTaskContext*, HdRenderIndex *index) override { _state->Prepare(index->GetResourceRegistry()); }
    void Execute(HdTaskContext*) override { _pass->Execute(_state, GetRenderTags()); }
    const TfTokenVector &GetRenderTags() const override {
        static const TfTokenVector tags = {HdRenderTagTokens->geometry};
        return tags;
    }
private:
    HdRenderPassSharedPtr _pass;
    HdRenderPassStateSharedPtr _state;
};

/// Render pass state with explicit matrices. Houdini's camera adapter only
/// runs inside Houdini, so the harness supplies the view itself.
class HarnessPassState final : public HdRenderPassState {
public:
    GfMatrix4d view{1.0}, projection{1.0};
    GfMatrix4d GetWorldToViewMatrix() const override { return view; }
    GfMatrix4d GetProjectionMatrix() const override { return projection; }
};

struct Stats {
    std::vector<double> values;
    void Add(double v) { values.push_back(v); }
    Json Summary() const {
        if (values.empty()) return nullptr;
        std::vector<double> v = values;
        std::sort(v.begin(), v.end());
        auto at = [&](double q) { return v[std::min(v.size() - 1, size_t(q * (v.size() - 1) + .5))]; };
        return {{"count", v.size()}, {"median", at(.5)}, {"p95", at(.95)}, {"max", v.back()}};
    }
};

struct Viewport {
    HdRendererPlugin *plugin = nullptr;
    HdRenderDelegate *delegate = nullptr;
    HdRenderIndex *index = nullptr;
    UsdImagingDelegate *scene = nullptr;
    std::map<std::string, HdRenderBuffer*> buffers;
    HdRenderPassSharedPtr pass;
    std::shared_ptr<HarnessPassState> state;
    HdTaskSharedPtrVector tasks;
    HdEngine engine;
    unsigned width = 0, height = 0;

    void Create(HdRendererPlugin *p, const UsdStageRefPtr &stage, unsigned w, unsigned h) {
        plugin = p;
        delegate = plugin->CreateRenderDelegate();
        index = HdRenderIndex::New(delegate, {});
        scene = new UsdImagingDelegate(index, SdfPath::AbsoluteRootPath());
        scene->Populate(stage->GetPseudoRoot());
        scene->SetTime(UsdTimeCode(1));
        pass = delegate->CreateRenderPass(index, HdRprimCollection(HdTokens->geometry, HdReprSelector(HdReprTokens->smoothHull)));
        state = std::make_shared<HarnessPassState>();
        tasks = {std::make_shared<PassTask>(pass, state)};
        Resize(w, h);
    }

    void Resize(unsigned w, unsigned h) {
        width = w; height = h;
        HdRenderPassAovBindingVector bindings;
        for (const TfToken &name : {HdAovTokens->color, HdAovTokens->depth, HdAovTokens->primId, HdAovTokens->instanceId}) {
            HdRenderBuffer *&buffer = buffers[name.GetString()];
            if (!buffer) buffer = static_cast<HdRenderBuffer*>(delegate->CreateBprim(HdPrimTypeTokens->renderBuffer,
                                                                                      SdfPath("/harness/" + name.GetString())));
            const HdAovDescriptor descriptor = delegate->GetDefaultAovDescriptor(name);
            if (descriptor.format == HdFormatInvalid) continue;
            buffer->Allocate(GfVec3i(int(w), int(h), 1), descriptor.format, false);
            HdRenderPassAovBinding binding;
            binding.aovName = name;
            binding.renderBuffer = buffer;
            binding.clearValue = descriptor.clearValue;
            bindings.push_back(binding);
        }
        state->SetAovBindings(bindings);
        state->SetFraming(CameraUtilFraming(GfRect2i(GfVec2i(0, 0), int(w), int(h))));
    }

    bool Execute(double *ms) {
        const auto start = Clock::now();
        engine.Execute(index, &tasks);
        if (ms) *ms = Ms(Clock::now() - start);
        return pass->IsConverged();
    }

    /// Cheap signature of the displayed color image.
    uint64_t Signature() {
        HdRenderBuffer *color = buffers["color"];
        const size_t pixelBytes = HdDataSizeOfFormat(color->GetFormat());
        const auto *data = static_cast<const uint8_t*>(color->Map());
        uint64_t hash = 1469598103934665603ull;
        if (data) {
            const size_t pixels = size_t(width) * height;
            for (size_t i = 0; i < pixels; i += 97)
                for (size_t b = 0; b < pixelBytes; ++b) hash = (hash ^ data[i * pixelBytes + b]) * 1099511628211ull;
        }
        color->Unmap();
        return hash;
    }

    std::vector<float> Pixel(unsigned x, unsigned y) {
        HdRenderBuffer *color = buffers["color"];
        const auto *data = static_cast<const uint8_t*>(color->Map());
        std::vector<float> out(4, 0.f);
        const size_t i = size_t(y) * width + x;
        if (data) {
            if (color->GetFormat() == HdFormatFloat16Vec4) {
                for (int c = 0; c < 4; ++c) { GfHalf h; h.setBits(reinterpret_cast<const uint16_t*>(data)[i * 4 + c]); out[c] = float(h); }
            } else if (color->GetFormat() == HdFormatFloat32Vec4) {
                for (int c = 0; c < 4; ++c) out[c] = reinterpret_cast<const float*>(data)[i * 4 + c];
            }
        }
        color->Unmap();
        return out;
    }

    Json Picks() {
        Json result = Json::object();
        HdRenderBuffer *prim = buffers["primId"], *instance = buffers["instanceId"];
        const auto *ids = static_cast<const int32_t*>(prim->Map());
        const auto *instances = static_cast<const int32_t*>(instance->Map());
        std::map<std::string, int> counts;
        std::map<std::string, std::set<int>> instanceIds;
        if (ids) {
            for (size_t i = 0, n = size_t(width) * height; i < n; ++i) {
                const std::string path = ids[i] < 0 ? "<none>" : index->GetRprimPathFromPrimId(ids[i]).GetString();
                ++counts[path];
                if (instances && instances[i] >= 0 && instanceIds[path].size() < 100000) instanceIds[path].insert(instances[i]);
            }
        }
        prim->Unmap();
        instance->Unmap();
        for (const auto &[path, count] : counts)
            result[path] = {{"pixels", count}, {"distinct_instances", instanceIds[path].size()}};
        return result;
    }

    Json Depth() {
        HdRenderBuffer *depth = buffers["depth"];
        const auto *values = static_cast<const float*>(depth->Map());
        Json result;
        if (values) {
            float lo = 2.f, hi = -1.f;
            for (size_t i = 0, n = size_t(width) * height; i < n; ++i) { lo = std::min(lo, values[i]); hi = std::max(hi, values[i]); }
            result = {{"min", lo}, {"max", hi}, {"center", values[size_t(height / 2) * width + width / 2]}};
        }
        depth->Unmap();
        return result;
    }

    void Destroy() {
        tasks.clear();
        pass.reset();
        state.reset();
        delete scene;
        for (auto &[name, buffer] : buffers) delegate->DestroyBprim(buffer);
        buffers.clear();
        delete index;
        plugin->DeleteRenderDelegate(delegate);
    }
};

/// The shot camera at the test time, orbited around a pivot by the harness.
struct HarnessCamera {
    GfCamera camera;
    GfMatrix4d base{1.0};
    GfVec3d pivot{0.0};
    double degrees = 0.0;

    void Create(const UsdStageRefPtr &stage, const SdfPath &source, UsdTimeCode time, const GfVec3d &center) {
        camera = UsdGeomCamera(stage->GetPrimAtPath(source)).GetCamera(time);
        base = camera.GetTransform();
        pivot = center;
    }

    /// Rotate the camera around the pivot's vertical axis.
    void Orbit(double angle) { degrees = angle; }

    void Apply(Viewport &v) const {
        GfMatrix4d rotate(1.0);
        rotate.SetRotate(GfRotation(GfVec3d(0, 1, 0), degrees));
        const GfMatrix4d around = GfMatrix4d(1.0).SetTranslate(-pivot) * rotate * GfMatrix4d(1.0).SetTranslate(pivot);
        GfCamera moved = camera;
        moved.SetTransform(base * around);
        GfFrustum frustum = moved.GetFrustum();
        CameraUtilConformWindow(&frustum, CameraUtilFit, double(v.width) / double(v.height));
        v.state->view = frustum.ComputeViewMatrix();
        v.state->projection = frustum.ComputeProjectionMatrix();
    }
};

/// Keep calling the pass at a 60 Hz UI cadence until it converges.
Json Settle(Viewport &v, const HarnessCamera &camera, double timeoutSeconds) {
    const auto start = Clock::now();
    Stats block;
    int calls = 0, frames = 0;
    uint64_t signature = v.Signature();
    double firstFrame = -1;
    bool converged = false;
    while (Ms(Clock::now() - start) < timeoutSeconds * 1000.0) {
        const auto tick = Clock::now();
        double ms;
        camera.Apply(v);
        converged = v.Execute(&ms);
        block.Add(ms);
        ++calls;
        const uint64_t now = v.Signature();
        if (now != signature) {
            ++frames;
            signature = now;
            if (firstFrame < 0) firstFrame = Ms(Clock::now() - start);
        }
        if (converged) break;
        std::this_thread::sleep_until(tick + std::chrono::microseconds(16667));
    }
    return {{"converged", converged}, {"seconds", Ms(Clock::now() - start) / 1000.0}, {"calls", calls},
            {"new_frames", frames}, {"first_new_frame_ms", firstFrame}, {"ui_block_ms", block.Summary()}};
}
} // namespace

int main(int argc, char **argv) {
#ifdef _WIN32
    // The default 15.6 ms timer turns each 16.7 ms tick into ~31 ms, halving the
    // simulated viewport rate. Request 1 ms resolution, as interactive apps do.
    timeBeginPeriod(1);
#endif
    if (argc < 4) {
        fprintf(stderr, "usage: hydra_harness PLUGIN_RESOURCES SCENE.usd REPORT.json [key=value ...]\n"
                        "  width height orbit(ui ticks) degrees(per tick) second(0/1) camera time pivot(x,y,z)\n"
                        "  config_prim samples edit(shader:input:r,g,b) settle(seconds)\n");
        return 2;
    }
    std::map<std::string, std::string> options = {{"width", "1280"}, {"height", "720"}, {"orbit", "120"},
        {"degrees", "1.5"}, {"second", "0"}, {"camera", ""}, {"time", "1"}, {"pivot", "0,0,0"},
        {"config_prim", ""}, {"samples", ""}, {"edit", ""}, {"settle", "600"}, {"output", ""}, {"idle", "0"}};
    for (int i = 4; i < argc; ++i) {
        const std::string item = argv[i];
        const size_t eq = item.find('=');
        if (eq == std::string::npos || !options.count(item.substr(0, eq))) { fprintf(stderr, "Unknown option %s\n", argv[i]); return 2; }
        options[item.substr(0, eq)] = item.substr(eq + 1);
    }
    const unsigned width = unsigned(std::stoi(options["width"])), height = unsigned(std::stoi(options["height"]));
    const int orbitFrames = std::stoi(options["orbit"]);
    const double degrees = std::stod(options["degrees"]);
    const double settle = std::stod(options["settle"]);
    const UsdTimeCode time(std::stod(options["time"]));
    GfVec3d pivot(0.0);
    std::sscanf(options["pivot"].c_str(), "%lf,%lf,%lf", &pivot[0], &pivot[1], &pivot[2]);
    PlugRegistry::GetInstance().RegisterPlugins(argv[1]);
    UsdStageRefPtr stage = UsdStage::Open(argv[2]);
    if (!stage) { fprintf(stderr, "Cannot open %s\n", argv[2]); return 1; }
    HdRendererPlugin *plugin = HdRendererPluginRegistry::GetInstance().GetRendererPlugin(TfToken("HdEeveeRendererPlugin"));
    if (!plugin) { fprintf(stderr, "EEVEE plugin not found\n"); return 1; }
    SdfPath source = options["camera"].empty() ? SdfPath() : SdfPath(options["camera"]);
    if (source.IsEmpty())
        for (const UsdPrim &prim : stage->Traverse()) if (prim.IsA<UsdGeomCamera>()) { source = prim.GetPath(); break; }
    HarnessCamera camera;
    camera.Create(stage, source, time, pivot);
    if (!camera.camera.GetFocalLength()) { fprintf(stderr, "No usable camera %s\n", source.GetText()); return 1; }
    // Render settings: Houdini's viewport receives the Stage settings prim;
    // the harness passes the same eevee:config as a delegate setting.
    std::string config;
    if (!options["config_prim"].empty())
        if (UsdPrim prim = stage->GetPrimAtPath(SdfPath(options["config_prim"])))
            prim.GetAttribute(TfToken("eevee:config")).Get(&config);
    if ((!options["samples"].empty() || !options["output"].empty()) && !config.empty()) {
        Json parsed = Json::parse(config);
        if (!options["samples"].empty()) parsed["eevee"]["taa_samples"] = std::stoi(options["samples"]);
        // Never write disk renders to the path stored in the scene.
        if (!options["output"].empty()) parsed["output"] = options["output"];
        config = parsed.dump();
    }
    Json report = {{"width", width}, {"height", height}, {"scene", argv[2]}, {"camera", source.GetString()},
                   {"time", time.GetValue()}, {"options", options}};
    auto configure = [&](Viewport &v) {
        if (!config.empty()) v.delegate->SetRenderSetting(TfToken("eevee:config"), VtValue(config));
        v.scene->SetTime(time);
    };
    Viewport view;
    const auto created = Clock::now();
    view.Create(plugin, stage, width, height);
    configure(view);
    report["initial"] = Settle(view, camera, settle);
    report["initial"]["wall_seconds"] = Ms(Clock::now() - created) / 1000.0;
    // Orbit at a 60 Hz UI cadence: one camera change per UI tick.
    {
        Stats block;
        int frames = 0;
        uint64_t signature = view.Signature();
        const auto start = Clock::now();
        for (int i = 0; i < orbitFrames; ++i) {
            const auto tick = Clock::now();
            camera.Orbit((i + 1) * degrees);
            camera.Apply(view);
            double ms;
            view.Execute(&ms);
            block.Add(ms);
            const uint64_t now = view.Signature();
            if (now != signature) { ++frames; signature = now; }
            std::this_thread::sleep_until(tick + std::chrono::microseconds(16667));
        }
        const double seconds = Ms(Clock::now() - start) / 1000.0;
        report["orbit"] = {{"ui_ticks", orbitFrames}, {"seconds", seconds}, {"new_frames", frames},
                           {"presented_fps", frames / seconds}, {"ui_block_ms", block.Summary()},
                           {"ui_fps", orbitFrames / seconds}};
    }
    report["after_orbit"] = Settle(view, camera, settle);
    report["depth"] = view.Depth();
    report["picks"] = view.Picks();
    if (!options["edit"].empty()) {
        // shader:input:r,g,b
        const std::string edit = options["edit"];
        const size_t a = edit.find(':'), b = edit.find(':', a + 1);
        GfVec3f value(0.f);
        std::sscanf(edit.substr(b + 1).c_str(), "%f,%f,%f", &value[0], &value[1], &value[2]);
        const auto before = view.Pixel(width / 2, height / 2);
        UsdShadeShader shader(stage->GetPrimAtPath(SdfPath(edit.substr(0, a))));
        bool authored = false;
        if (shader) {
            UsdShadeInput input = shader.GetInput(TfToken(edit.substr(a + 1, b - a - 1)));
            if (!input) input = shader.CreateInput(TfToken(edit.substr(a + 1, b - a - 1)), SdfValueTypeNames->Color3f);
            authored = input.Set(value);
        }
        view.scene->ApplyPendingUpdates();
        report["material_edit"] = Settle(view, camera, settle);
        report["material_edit"]["authored"] = authored;
        report["material_edit"]["pixel_before"] = before;
        report["material_edit"]["pixel_after"] = view.Pixel(width / 2, height / 2);
    }
    // Resize, as when a viewport pane is dragged.
    view.Resize(width * 3 / 4, height * 3 / 4);
    report["resize"] = Settle(view, camera, settle);
    view.Resize(width, height);
    Settle(view, camera, settle);
    if (options["second"] != "0") {
        Viewport other;
        other.Create(plugin, stage, width / 2, height / 2);
        configure(other);
        camera.Orbit(30.0);
        const auto start = Clock::now();
        bool first = false, secondDone = false;
        Stats block;
        while ((!first || !secondDone) && Ms(Clock::now() - start) < settle * 1000.0) {
            double ms;
            camera.Apply(view);
            first = view.Execute(&ms); block.Add(ms);
            camera.Apply(other);
            secondDone = other.Execute(&ms); block.Add(ms);
            std::this_thread::sleep_for(std::chrono::milliseconds(16));
        }
        report["two_viewports"] = {{"both_converged", first && secondDone}, {"seconds", Ms(Clock::now() - start) / 1000.0},
            {"ui_block_ms", block.Summary()}, {"second_picks_count", other.Picks().size()}};
        auto stats = other.delegate->GetRenderStats();
        auto primary = view.delegate->GetRenderStats();
        if (stats.count("session") && primary.count("session"))
            report["two_viewports"]["sessions"] = {primary["session"].Get<int>(), stats["session"].Get<int>()};
        other.Destroy();
    }
    // Idle viewport (no render calls), e.g. to observe worker memory release.
    if (const double idle = std::stod(options["idle"]); idle > 0)
        std::this_thread::sleep_for(std::chrono::milliseconds(int64_t(idle * 1000)));
    VtDictionary stats = view.delegate->GetRenderStats();
    Json statsJson = Json::object();
    for (const auto &[key, value] : stats) {
        if (value.IsHolding<double>()) statsJson[key] = value.UncheckedGet<double>();
        else if (value.IsHolding<int>()) statsJson[key] = value.UncheckedGet<int>();
        else if (value.IsHolding<std::string>()) statsJson[key] = value.UncheckedGet<std::string>();
    }
    report["final_stats"] = statsJson;
    const auto destroyStart = Clock::now();
    view.Destroy();
    report["destroy_ms"] = Ms(Clock::now() - destroyStart);
    std::ofstream(argv[3]) << report.dump(2) << '\n';
    printf("%s\n", report.dump(2).c_str());
    return 0;
}
