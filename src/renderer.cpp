#include "renderer.h"
#include "prims.h"
#include <pxr/base/gf/half.h>
#include <pxr/base/work/loops.h>
#include <pxr/imaging/hd/types.h>
#include <cmath>
#include <cstdio>
#include <fstream>
#if defined(__x86_64__) || defined(_M_X64)
#include <immintrin.h>
#endif
#if defined(_MSC_VER)
#include <intrin.h>
#endif

PXR_NAMESPACE_OPEN_SCOPE
namespace hdEevee {
namespace {
class WorkerError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

constexpr double kGiveUpSeconds = 180.0;
constexpr size_t kBatchBytes = 64u << 20;

double Milliseconds(std::chrono::steady_clock::duration d) {
    return std::chrono::duration<double, std::milli>(d).count();
}

// ------------------------------------------------------------ conversions
#if defined(__x86_64__) || defined(_M_X64)
#if defined(__GNUC__) || defined(__clang__)
__attribute__((target("avx,f16c")))
#endif
void ToHalfF16C(const float *src, uint16_t *dst, size_t count) {
    size_t i = 0;
    for (; i + 8 <= count; i += 8) {
        const __m256 value = _mm256_loadu_ps(src + i);
        _mm_storeu_si128(reinterpret_cast<__m128i*>(dst + i), _mm256_cvtps_ph(value, _MM_FROUND_TO_NEAREST_INT));
    }
    for (; i < count; ++i) dst[i] = GfHalf(src[i]).bits();
}

bool HasF16C() {
#if defined(__GNUC__) || defined(__clang__)
    __builtin_cpu_init();
    return __builtin_cpu_supports("avx") && __builtin_cpu_supports("f16c");
#else
    int info[4];
    __cpuid(info, 1);
    return (info[2] & (1 << 28)) && (info[2] & (1 << 29));
#endif
}
#endif

void ToHalf(const float *src, uint16_t *dst, size_t count) {
#if defined(__x86_64__) || defined(_M_X64)
    static const bool f16c = HasF16C();
    if (f16c) { ToHalfF16C(src, dst, count); return; }
#endif
    for (size_t i = 0; i < count; ++i) dst[i] = GfHalf(src[i]).bits();
}

template <class Fn>
void ForRows(unsigned rows, Fn &&fn) {
    WorkParallelForN(rows, [&](size_t begin, size_t end) {
        for (size_t y = begin; y < end; ++y) fn(unsigned(y));
    }, 8);
}

/// Write one row of float values into a plane of `format`.
void StoreRow(const float *values, unsigned width, unsigned sourceChannels, HdFormat format, uint8_t *row) {
    const size_t channels = HdGetComponentCount(format);
    const HdFormat component = HdGetComponentFormat(format);
    if (sourceChannels == channels) {
        if (component == HdFormatFloat16) { ToHalf(values, reinterpret_cast<uint16_t*>(row), size_t(width) * channels); return; }
        if (component == HdFormatFloat32) { std::memcpy(row, values, size_t(width) * channels * sizeof(float)); return; }
    }
    for (unsigned x = 0; x < width; ++x)
        for (size_t c = 0; c < channels; ++c) {
            const float v = c < sourceChannels ? values[size_t(x) * sourceChannels + c] : (c == 3 ? 1.f : 0.f);
            if (component == HdFormatFloat16) reinterpret_cast<uint16_t*>(row)[x * channels + c] = GfHalf(v).bits();
            else if (component == HdFormatFloat32) reinterpret_cast<float*>(row)[x * channels + c] = v;
            else if (component == HdFormatInt32) reinterpret_cast<int32_t*>(row)[x * channels + c] = int32_t(v);
        }
}
} // namespace

// ------------------------------------------------------------ ViewRequest
bool ViewRequest::operator==(const ViewRequest &o) const {
    return width == o.width && height == o.height && view == o.view && projection == o.projection &&
        camera == o.camera && cameraSamples == o.cameraSamples && colorFormat == o.colorFormat &&
        depth == o.depth && ids == o.ids && aovs == o.aovs && frame == o.frame &&
        targetSamples == o.targetSamples && raytracing == o.raytracing && upAxis == o.upAxis &&
        config == o.config && navigationScale == o.navigationScale && textureLimit == o.textureLimit &&
        limitSurface == o.limitSurface;
}

// ---------------------------------------------------------------- Renderer
Renderer::Renderer(BridgeState *state) : _state(state), _final(state->FinalRender()) {
    if (const char *budget = std::getenv("HDEEVEE_REFINE_BUDGET_MS")) _budgetMs = std::max(10.0, std::atof(budget));
    SetStatus("idle", "");
    if (!_final) _thread = std::thread([this] { Run(); });
}

Renderer::~Renderer() {
    {
        std::lock_guard<std::mutex> lock(_mutex);
        _stopping = true;
    }
    _wake.notify_all();
    // Abort a blocking read so a long draw cannot hold Houdini while the
    // renderer is switched or restarted.
    _connection.Interrupt();
    if (_thread.joinable()) _thread.join();
    _connection.Close();
}

void Renderer::Post(const ViewRequest &request) {
    {
        std::lock_guard<std::mutex> lock(_mutex);
        if (!_hasRequest || _latest != request) {
            _latest = request;
            _hasRequest = true;
            ++_latestSerial;
        }
    }
    // Scene edits change the state version without changing the view.
    _wake.notify_one();
}

bool Renderer::ChangedLocked(const ViewRequest &request) const {
    return !_renderedValid || _rendered != request || _renderedVersion != _state->Version();
}

bool Renderer::NeedsWorkLocked() const {
    if (!_hasRequest || _paused) return false;
    if (Clock::now() < _retryAt) return false;
    const bool failed = _failedValid && _failed == _latest && _failedVersion == _state->Version();
    if (failed) return false;
    return ChangedLocked(_latest) || !_complete;
}

void Renderer::Snapshot(std::shared_ptr<const Frame> *frame, bool *converged) const {
    std::lock_guard<std::mutex> lock(_mutex);
    *frame = _frame;
    if (!_hasRequest) { *converged = false; return; }
    const bool failed = _failedValid && _failed == _latest && _failedVersion == _state->Version();
    *converged = failed || _paused || (!_busy && !ChangedLocked(_latest) && _complete);
}

void Renderer::SetPaused(bool paused) {
    {
        std::lock_guard<std::mutex> lock(_mutex);
        _paused = paused;
    }
    SetStatus(paused ? "paused" : "idle", "");
    _wake.notify_all();
}

bool Renderer::Paused() const {
    std::lock_guard<std::mutex> lock(_mutex);
    return _paused;
}

int Renderer::FirstStep(int target) const {
    if (target <= 4) return target;
    if (_perSampleMs <= 0.0) return std::min(target, 4);
    const int samples = std::clamp(int(_budgetMs / _perSampleMs), 1, target);
    return 2 * samples >= target ? target : samples;
}

Renderer::Job Renderer::Plan(const ViewRequest &request, uint64_t serial) {
    Job job;
    job.request = request;
    job.serial = serial;
    const int target = std::max(1, request.targetSamples);
    bool changed;
    {
        std::lock_guard<std::mutex> lock(_mutex);
        changed = ChangedLocked(request);
    }
    if (changed) {
        // Interactive change: one quick sample, smaller while navigating.
        _stepSamples = 0;
        int scale = std::max(1, request.navigationScale);
        while (scale > 1 && (request.width / unsigned(scale) < 64 || request.height / unsigned(scale) < 64)) --scale;
        job.purpose = scale > 1 ? "navigate" : "refine";
        job.scale = scale;
        job.samples = scale > 1 ? 1 : std::min(target, 1);
    } else {
        // Refinement. EEVEE restarts accumulation whenever the sample count
        // changes, so use at most one intermediate step before the target.
        job.purpose = "refine";
        job.scale = 1;
        int next = target;
        const int first = FirstStep(target);
        if (_stepSamples == 0 && first < target) next = first;
        else if (_stepSamples > 0 && first > _stepSamples && first < target && first >= 2 * _stepSamples) next = first;
        job.samples = next;
    }
    // A solid preview is shown once per worker session while EEVEE compiles
    // its first shaders; every later job must be drawn by EEVEE.
    job.allowPreview = !_eeveeDrawn && !_previewShown;
    return job;
}

void Renderer::Run() {
    while (true) {
        ViewRequest request;
        uint64_t serial = 0, version = 0;
        {
            std::unique_lock<std::mutex> lock(_mutex);
            _wake.wait_for(lock, std::chrono::milliseconds(250), [&] { return _stopping || NeedsWorkLocked(); });
            if (_stopping) return;
            if (!NeedsWorkLocked()) continue;
            request = _latest;
            serial = _latestSerial;
            version = _state->Version();
            _busy = true;
        }
        const Job job = Plan(request, serial);
        std::shared_ptr<const Frame> frame;
        std::string error;
        bool workerError = false;
        try {
            frame = Execute(job);
        } catch (const WorkerError &exc) {
            error = exc.what();
            workerError = true;
        } catch (const std::exception &exc) {
            error = exc.what();
        }
        bool stopping;
        {
            std::lock_guard<std::mutex> lock(_mutex);
            _busy = false;
            stopping = _stopping;
            if (frame) {
                auto published = std::const_pointer_cast<Frame>(frame);
                published->serial = ++_frameSerial;
                _frame = frame;
                _renderedValid = true;
                _rendered = job.request;
                _renderedVersion = version;
                const int target = std::max(1, job.request.targetSamples);
                if (frame->preview) _previewShown = true;
                else {
                    _eeveeDrawn = true;
                    if (job.purpose == "refine" && job.scale == 1) _stepSamples = job.samples;
                }
                _complete = !frame->preview && job.purpose == "refine" && job.scale == 1 && job.samples >= target;
                _failedValid = false;
                _connecting = false;
            } else if (workerError || stopping) {
                _failedValid = true;
                _failed = job.request;
                _failedVersion = version;
            } else {
                // Connection problem: the worker may still be starting. Retry
                // for a while, then stop polling until the view changes.
                const auto now = Clock::now();
                if (!_connecting) { _connecting = true; _connectSince = now; }
                if (std::chrono::duration<double>(now - _connectSince).count() > kGiveUpSeconds) {
                    _failedValid = true;
                    _failed = job.request;
                    _failedVersion = version;
                    _connecting = false;
                } else {
                    _retryAt = now + std::chrono::milliseconds(500);
                }
            }
        }
        if (stopping) return;
        if (frame) {
            SetStatus(frame->preview ? "compiling" : (_complete ? "converged" : "rendering"),
                      frame->preview ? "Compiling EEVEE shaders" : "");
        } else if (workerError) {
            fprintf(stderr, "[EEVEE] %s\n", error.c_str());
            SetStatus("error", error);
        } else {
            _connection.Close();
            _mapping.close();
            _replay = true;
            bool failed;
            {
                std::lock_guard<std::mutex> lock(_mutex);
                failed = _failedValid;
            }
            if (failed) {
                fprintf(stderr, "[EEVEE] %s\n", error.c_str());
                SetStatus("error", "EEVEE worker unavailable: " + error);
            } else {
                SetStatus("starting", "Waiting for the Blender EEVEE worker");
            }
        }
    }
}

std::shared_ptr<const Frame> Renderer::RenderFinal(const ViewRequest &request) {
    Job job;
    job.request = request;
    job.purpose = "final";
    job.samples = std::max(1, request.targetSamples);
    try {
        auto frame = Execute(job);
        std::const_pointer_cast<Frame>(frame)->serial = ++_frameSerial;
        SetStatus("converged", "");
        return frame;
    } catch (const std::exception &exc) {
        _connection.Close();
        _mapping.close();
        _replay = true;
        SetStatus("error", exc.what());
        throw;
    }
}

void Renderer::Connect() {
    if (_connection.IsOpen()) return;
    _connection.Open(30);
    _connection.Send({{"op", "hello"}, {"owner_pid", hde::processId()}, {"client", "hdEevee 0.7.1"}});
    const Json reply = _connection.Receive(nullptr);
    if (!reply.value("ok", false))
        throw WorkerError(reply.value("error", std::string("EEVEE worker rejected the connection")));
    // A new connection is a new worker session: replay the complete scene.
    _replay = true;
    _eeveeDrawn = false;
    _previewShown = false;
    std::lock_guard<std::mutex> lock(_statsMutex);
    _stats["gpu"] = VtValue(reply.value("gpu", std::string()));
    _stats["session"] = VtValue(reply.value("session", 0));
    _stats["worker_version"] = VtValue(reply.value("version", std::string()));
}

void Renderer::SendChanges(const Job &job) {
    const bool reset = _replay;
    std::vector<Change> changes = reset ? _state->TakeReplay() : _state->TakePending();
    if (changes.empty() && !reset) return;
    _connection.SetTimeout(900);
    size_t start = 0;
    bool first = true;
    Json errors = Json::object();
    while (first || start < changes.size()) {
        size_t end = start, bytes = 0;
        while (end < changes.size()) {
            size_t size = 256;
            for (const auto &[key, blob] : changes[end].blobs) size += blob->size;
            if (end > start && bytes + size > kBatchBytes) break;
            bytes += size;
            ++end;
        }
        Json header = {{"op", "update"}, {"reset", reset && first}, {"owner_pid", hde::processId()},
                       {"config", job.request.config}, {"up_axis", job.request.upAxis}};
        Json list = Json::array();
        std::vector<const Change*> batch;
        for (size_t i = start; i < end; ++i) { list.push_back(changes[i].json); batch.push_back(&changes[i]); }
        header["changes"] = std::move(list);
        _connection.Send(std::move(header), batch);
        const Json reply = _connection.Receive(nullptr);
        if (!reply.value("ok", false)) {
            // The worker's scene may be partial now; resynchronize next time.
            _replay = true;
            throw WorkerError(reply.value("error", std::string("EEVEE scene update failed")));
        }
        if (reply.contains("errors")) errors.update(reply["errors"]);
        start = end;
        first = false;
    }
    _replay = false;
    if (!errors.empty()) {
        std::lock_guard<std::mutex> lock(_statsMutex);
        _stats["scene_errors"] = VtValue(int(errors.size()));
        for (auto it = errors.begin(); it != errors.end(); ++it)
            fprintf(stderr, "[EEVEE] %s: %s\n", it.key().c_str(), it.value().dump().c_str());
    }
}

std::shared_ptr<const Frame> Renderer::Execute(const Job &job) {
    const auto start = Clock::now();
    const ViewRequest &r = job.request;
    if (!r.width || !r.height) throw WorkerError("EEVEE viewport has no size");
    Connect();
    const auto uploadStart = Clock::now();
    SendChanges(job);
    const double uploadMs = Milliseconds(Clock::now() - uploadStart);
    const unsigned width = std::max(1u, r.width / unsigned(job.scale));
    const unsigned height = std::max(1u, r.height / unsigned(job.scale));
    Json aovs = Json::array();
    for (const auto &[name, format] : r.aovs) aovs.push_back(name);
    // Picking IDs and depth come from an extra pass over the whole scene.
    // Navigation frames skip it; the settled frame provides them.
    const bool settledOnly = job.purpose == "navigate";
    Json request = {{"op", "render"}, {"owner_pid", hde::processId()},
        {"width", width}, {"height", height}, {"output_width", r.width}, {"output_height", r.height},
        {"view", MatrixJson(r.view)}, {"projection", MatrixJson(r.projection)},
        {"samples", job.samples}, {"purpose", job.purpose}, {"allow_preview", job.allowPreview},
        {"depth", r.depth && !settledOnly}, {"ids", r.ids && !settledOnly}, {"aovs", aovs}, {"frame", r.frame}, {"camera", r.camera},
        {"up_axis", r.upAxis}, {"raytracing", r.raytracing}, {"config", r.config},
        {"final_render", job.purpose == "final"}, {"revision", job.serial}, {"transport", "shm"},
        {"texture_limit", job.purpose == "final" ? 0 : r.textureLimit},
        {"subdivision_limit_surface", job.purpose == "final" || r.limitSurface}};
    if (job.purpose == "final") request["camera_samples"] = r.cameraSamples;
    _connection.SetTimeout(job.purpose == "final" ? 7200 : 900);
    _connection.Send(std::move(request));
    std::vector<uint8_t> payload;
    const Json reply = _connection.Receive(&payload);
    if (!reply.value("ok", false)) throw WorkerError(reply.value("error", std::string("EEVEE rendering failed")));
    auto frame = BuildFrame(job, reply, payload);
    frame->samples = reply.value("samples", job.samples);
    frame->targetSamples = reply.value("target_samples", r.targetSamples);
    frame->preview = reply.value("preview", false);
    frame->purpose = job.purpose;
    const double elapsed = Milliseconds(Clock::now() - start);
    if (!frame->preview && job.purpose == "refine" && job.scale == 1 && job.samples > 0) {
        // Adopt a lower cost at once: the first draw of a session includes
        // shader compilation and would otherwise keep refinement steps tiny.
        const double perSample = reply.value("draw_ms", 0.0) / job.samples;
        _perSampleMs = (_perSampleMs <= 0.0 || perSample < _perSampleMs) ? perSample : .7 * _perSampleMs + .3 * perSample;
    }
    RecordReply(reply, elapsed);
    {
        std::lock_guard<std::mutex> lock(_statsMutex);
        _stats["upload_ms"] = VtValue(uploadMs);
    }
    if (std::getenv("HDEEVEE_TRACE")) {
        Json trace = reply;
        trace["bridge_ms"] = elapsed;
        trace["upload_ms"] = uploadMs;
        trace["job"] = {{"purpose", job.purpose}, {"samples", job.samples}, {"scale", job.scale}};
        trace["view"] = MatrixJson(r.view);
        trace["projection"] = MatrixJson(r.projection);
        std::ofstream out(hde::environmentPath("HDEEVEE_TRACE"), std::ios::app);
        out << trace.dump() << '\n';
    }
    return frame;
}

std::shared_ptr<std::vector<uint8_t>> Renderer::Allocate(size_t bytes) {
    // Reuse image storage no longer held by Houdini's buffers or old frames.
    for (auto &entry : _pool) {
        if (entry.use_count() == 1) {
            entry->resize(bytes);
            return entry;
        }
    }
    auto created = std::make_shared<std::vector<uint8_t>>(bytes);
    if (_pool.size() < 24) _pool.push_back(created);
    return created;
}

Plane Renderer::Constant(HdFormat format, unsigned width, unsigned height, float value) {
    // Planes such as "no depth" or "no pick ID" never change for a size, so
    // they are built once here instead of being filled on Houdini's thread.
    const auto key = std::make_tuple(int(format), width, height, value);
    auto it = _constants.find(key);
    if (it != _constants.end()) return it->second;
    if (_constants.size() > 16) _constants.clear();
    Plane plane{format, width, height, std::make_shared<std::vector<uint8_t>>(size_t(width) * height * HdDataSizeOfFormat(format))};
    if (format == HdFormatInt32) {
        auto *out = reinterpret_cast<int32_t*>(plane.pixels->data());
        std::fill(out, out + size_t(width) * height, int32_t(value));
    } else {
        auto *out = reinterpret_cast<float*>(plane.pixels->data());
        std::fill(out, out + size_t(width) * height * HdGetComponentCount(format), value);
    }
    return _constants[key] = plane;
}

std::shared_ptr<Frame> Renderer::BuildFrame(const Job &job, const Json &reply, const std::vector<uint8_t> &payload) {
    const uint8_t *base = payload.data();
    size_t available = payload.size();
    if (reply.contains("shm")) {
        const Json &shm = reply["shm"];
        available = shm.at("size").get<size_t>();
        base = _mapping.map(shm.at("name").get<std::string>(), available);
    }
    std::map<std::string, Json> buffers;
    for (const auto &descriptor : reply.value("buffers", Json::array())) buffers[descriptor.at("name").get<std::string>()] = descriptor;
    auto source = [&](const std::string &name, const char *dtype, unsigned channels, unsigned *w, unsigned *h) -> const uint8_t* {
        auto it = buffers.find(name);
        if (it == buffers.end()) return nullptr;
        const Json &d = it->second;
        *w = d.at("width").get<unsigned>();
        *h = d.at("height").get<unsigned>();
        const size_t offset = d.at("offset").get<size_t>(), bytes = d.at("bytes").get<size_t>();
        if (d.at("dtype").get<std::string>() != dtype || (channels && d.value("channels", 1u) != channels) ||
            offset > available || bytes > available - offset || bytes != size_t(*w) * *h * std::max(channels, 1u) * 4)
            throw WorkerError("EEVEE returned an invalid " + name + " buffer");
        return base + offset;
    };
    auto frame = std::make_shared<Frame>();
    const ViewRequest &r = job.request;
    const unsigned W = r.width, H = r.height;
    unsigned rw = 0, rh = 0;
    // Color: bilinear resize for navigation frames, then 16/32-bit float.
    if (const auto *pixels = reinterpret_cast<const float*>(source("color", "f4", 4, &rw, &rh))) {
        Plane plane{r.colorFormat, W, H, Allocate(size_t(W) * H * HdDataSizeOfFormat(r.colorFormat))};
        const size_t rowBytes = size_t(W) * HdDataSizeOfFormat(r.colorFormat);
        if (rw == W && rh == H) {
            ForRows(H, [&](unsigned y) {
                StoreRow(pixels + size_t(y) * W * 4, W, 4, r.colorFormat, plane.pixels->data() + y * rowBytes);
            });
        } else {
            std::vector<unsigned> x0(W), x1(W);
            std::vector<float> fx(W);
            for (unsigned x = 0; x < W; ++x) {
                const float sx = std::clamp((x + .5f) * rw / W - .5f, 0.f, float(rw - 1));
                x0[x] = unsigned(sx);
                x1[x] = std::min(x0[x] + 1, rw - 1);
                fx[x] = sx - float(x0[x]);
            }
            ForRows(H, [&](unsigned y) {
                const float sy = std::clamp((y + .5f) * rh / H - .5f, 0.f, float(rh - 1));
                const unsigned y0 = unsigned(sy), y1 = std::min(y0 + 1, rh - 1);
                const float fy = sy - float(y0);
                const float *a = pixels + size_t(y0) * rw * 4, *b = pixels + size_t(y1) * rw * 4;
                std::vector<float> row(size_t(W) * 4);
                for (unsigned x = 0; x < W; ++x)
                    for (int c = 0; c < 4; ++c) {
                        const float top = a[x0[x] * 4 + c] + (a[x1[x] * 4 + c] - a[x0[x] * 4 + c]) * fx[x];
                        const float bottom = b[x0[x] * 4 + c] + (b[x1[x] * 4 + c] - b[x0[x] * 4 + c]) * fx[x];
                        row[size_t(x) * 4 + c] = top + (bottom - top) * fy;
                    }
                StoreRow(row.data(), W, 4, r.colorFormat, plane.pixels->data() + y * rowBytes);
            });
        }
        frame->planes["color"] = std::move(plane);
    }
    // Single-value planes use nearest samples: IDs and depth must not blend.
    auto nearest = [&](const uint8_t *src, unsigned sw, unsigned sh, size_t pixelBytes, Plane &plane) {
        plane.pixels = Allocate(size_t(W) * H * pixelBytes);
        uint8_t *dst = plane.pixels->data();
        if (sw == W && sh == H) { std::memcpy(dst, src, size_t(W) * H * pixelBytes); return; }
        ForRows(H, [&](unsigned y) {
            const unsigned syy = std::min(sh - 1, unsigned((y + .5) * sh / H));
            for (unsigned x = 0; x < W; ++x) {
                const unsigned sxx = std::min(sw - 1, unsigned((x + .5) * sw / W));
                std::memcpy(dst + (size_t(y) * W + x) * pixelBytes, src + (size_t(syy) * sw + sxx) * pixelBytes, pixelBytes);
            }
        });
    };
    if (r.depth) {
        if (const uint8_t *depth = source("depth", "f4", 1, &rw, &rh)) {
            Plane plane{HdFormatFloat32, W, H, nullptr};
            nearest(depth, rw, rh, 4, plane);
            frame->planes["depth"] = std::move(plane);
        }
    }
    if (r.ids) {
        unsigned tw = 0, th = 0;
        const auto *pick = reinterpret_cast<const uint32_t*>(source("pick", "u4", 1, &rw, &rh));
        auto it = buffers.find("pick_table");
        if (pick && it != buffers.end()) {
            const Json &d = it->second;
            const size_t count = d.at("shape").at(1).get<size_t>();
            const size_t offset = d.at("offset").get<size_t>();
            if (d.at("dtype").get<std::string>() != "i4" || offset > available || count * 8 > available - offset)
                throw WorkerError("EEVEE returned an invalid pick table");
            const auto *table = reinterpret_cast<const int32_t*>(base + offset);
            (void)tw; (void)th;
            Plane prim{HdFormatInt32, W, H, Allocate(size_t(W) * H * 4)};
            Plane instance{HdFormatInt32, W, H, Allocate(size_t(W) * H * 4)};
            auto *primOut = reinterpret_cast<int32_t*>(prim.pixels->data());
            auto *instanceOut = reinterpret_cast<int32_t*>(instance.pixels->data());
            ForRows(H, [&](unsigned y) {
                const unsigned syy = rh == H ? y : std::min(rh - 1, unsigned((y + .5) * rh / H));
                for (unsigned x = 0; x < W; ++x) {
                    const unsigned sxx = rw == W ? x : std::min(rw - 1, unsigned((x + .5) * rw / W));
                    const uint32_t value = pick[size_t(syy) * rw + sxx];
                    const uint32_t index = value & 0x00ffffffu;
                    const bool valid = (value >> 24) == 255u && index > 0 && index < count;
                    primOut[size_t(y) * W + x] = valid ? table[index] : -1;
                    instanceOut[size_t(y) * W + x] = valid ? table[count + index] : -1;
                }
            });
            frame->planes["primId"] = std::move(prim);
            frame->planes["instanceId"] = std::move(instance);
        }
    }
    if (r.depth && !frame->planes.count("depth")) frame->planes["depth"] = Constant(HdFormatFloat32, W, H, 1.f);
    if (r.ids && !frame->planes.count("primId")) {
        frame->planes["primId"] = Constant(HdFormatInt32, W, H, -1.f);
        frame->planes["instanceId"] = frame->planes["primId"];
    }
    for (const auto &[name, format] : r.aovs) {
        auto it = buffers.find(name);
        if (it == buffers.end()) continue;
        const unsigned channels = it->second.value("channels", 1u);
        const uint8_t *data = source(name, "f4", channels, &rw, &rh);
        Plane plane{format, W, H, nullptr};
        const size_t outChannels = HdGetComponentCount(format);
        if (outChannels == channels && HdGetComponentFormat(format) == HdFormatFloat32) {
            nearest(data, rw, rh, size_t(channels) * 4, plane);
        } else {
            Plane resized{HdFormatFloat32, W, H, nullptr};
            nearest(data, rw, rh, size_t(channels) * 4, resized);
            plane.pixels = Allocate(size_t(W) * H * HdDataSizeOfFormat(format));
            const size_t rowBytes = size_t(W) * HdDataSizeOfFormat(format);
            const auto *values = reinterpret_cast<const float*>(resized.pixels->data());
            ForRows(H, [&](unsigned y) {
                StoreRow(values + size_t(y) * W * channels, W, channels, format, plane.pixels->data() + y * rowBytes);
            });
        }
        frame->planes[name] = std::move(plane);
    }
    return frame;
}

void Renderer::RecordReply(const Json &reply, double bridgeMs) {
    std::lock_guard<std::mutex> lock(_statsMutex);
    _stats["bridge_ms"] = VtValue(bridgeMs);
    for (const char *key : {"update_ms", "draw_ms", "readback_ms", "total_ms"})
        if (reply.contains(key) && reply[key].is_number()) _stats[key] = VtValue(reply[key].get<double>());
    _stats["samples"] = VtValue(reply.value("samples", 0));
    _stats["target_samples"] = VtValue(reply.value("target_samples", 0));
    _stats["frame"] = VtValue(reply.value("frame", 0));
    _stats["objects"] = VtValue(reply.value("objects", 0));
    _stats["materials"] = VtValue(reply.value("materials", 0));
    _stats["purpose"] = VtValue(reply.value("purpose", std::string()));
    const auto warnings = reply.value("material_warnings", Json::object()).size() +
                          reply.value("geometry_warnings", Json::object()).size();
    _stats["warnings"] = VtValue(int(warnings));
}

void Renderer::SetStatus(const std::string &state, const std::string &message) {
    std::lock_guard<std::mutex> lock(_statsMutex);
    const int samples = _stats.count("samples") ? _stats["samples"].Get<int>() : 0;
    const int target = _stats.count("target_samples") ? _stats["target_samples"].Get<int>() : 0;
    std::string text = message;
    if (text.empty()) {
        if (state == "rendering") text = "Rendering " + std::to_string(samples) + "/" + std::to_string(target) + " samples";
        else if (state == "converged") text = "Done (" + std::to_string(samples) + " samples)";
        else text = state;
    }
    _stats["state"] = VtValue(state);
    _stats["status"] = VtValue(text);
    _stats["huskErrorStatus"] = VtValue(state == "error" ? 1 : 0);
    if (state == _statusState && message == _statusMessage) return;
    _statusState = state;
    _statusMessage = message;
    // A small status file lets Houdini's UI show errors in its status bar.
    const auto session = hde::environmentPath("HDEEVEE_SESSION_DIR");
    if (session.empty() || (state != "error" && state != "starting" && state != "converged" && state != "compiling")) return;
    try {
        const auto path = session / "status.json";
        const auto temporary = session / ("status-" + std::to_string(hde::processId()) + ".tmp");
        {
            std::ofstream out(temporary);
            out << Json({{"state", state}, {"message", message}, {"pid", hde::processId()},
                         {"time", std::chrono::duration<double>(std::chrono::system_clock::now().time_since_epoch()).count()}}).dump();
        }
        std::filesystem::rename(temporary, path);
    } catch (const std::exception&) {
    }
}

VtDictionary Renderer::Stats() const {
    std::lock_guard<std::mutex> lock(_statsMutex);
    return _stats;
}
} // namespace hdEevee
PXR_NAMESPACE_CLOSE_SCOPE
