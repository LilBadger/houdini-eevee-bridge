#pragma once
// Background rendering for one render delegate.
//
// Houdini's viewport calls the render pass on its UI thread. The pass only
// posts the latest view to this Renderer and presents the newest finished
// frame, so Houdini stays responsive while Blender draws. The render thread
// sends scene edits, schedules navigation and refinement frames, reads the
// worker's shared-memory pixels and converts them for Houdini.
#include "protocol.h"
#include "scene.h"
#include <pxr/base/gf/matrix4d.h>
#include <pxr/base/vt/dictionary.h>
#include <pxr/imaging/hd/types.h>
#include <chrono>
#include <condition_variable>
#include <thread>
#include <tuple>

PXR_NAMESPACE_OPEN_SCOPE
namespace hdEevee {

struct ViewRequest {
    unsigned width = 0, height = 0;          // Houdini's color buffer
    GfMatrix4d view{1.0}, projection{1.0};
    Json camera = Json::object();
    Json cameraSamples = Json::array();
    HdFormat colorFormat = HdFormatFloat16Vec4;
    bool depth = false, ids = false;
    std::vector<std::pair<std::string, HdFormat>> aovs;
    double frame = 1.0;
    int targetSamples = 16;
    bool raytracing = true;
    std::string upAxis = "Y";
    Json config = Json::object();
    int navigationScale = 2;
    int textureLimit = 0;                    // pixels; 0 = full resolution
    bool limitSurface = false;               // exact subdivision surfaces in the viewport

    bool operator==(const ViewRequest &other) const;
    bool operator!=(const ViewRequest &other) const { return !(*this == other); }
};

struct Plane {
    HdFormat format = HdFormatInvalid;
    unsigned width = 0, height = 0;
    std::shared_ptr<std::vector<uint8_t>> pixels;
};

struct Frame {
    uint64_t serial = 0;
    std::map<std::string, Plane> planes;     // color, depth, primId, instanceId, EEVEE pass names
    int samples = 0, targetSamples = 0;
    bool preview = false;
    std::string purpose;
};

class Renderer {
public:
    explicit Renderer(BridgeState *state);
    ~Renderer();
    Renderer(const Renderer&) = delete;
    Renderer &operator=(const Renderer&) = delete;

    /// Viewport: record the wanted view; returns immediately.
    void Post(const ViewRequest &request);
    /// The newest frame and whether it is final for the posted view.
    void Snapshot(std::shared_ptr<const Frame> *frame, bool *converged) const;
    /// Disk rendering: render the request on the calling thread.
    std::shared_ptr<const Frame> RenderFinal(const ViewRequest &request);

    void SetPaused(bool paused);
    bool Paused() const;
    VtDictionary Stats() const;

private:
    struct Job {
        ViewRequest request;
        uint64_t serial = 0;
        std::string purpose;     // navigate, refine, final
        int samples = 1;
        int scale = 1;
        bool allowPreview = false;
    };
    using Clock = std::chrono::steady_clock;

    void Run();
    bool NeedsWorkLocked() const;
    bool ChangedLocked(const ViewRequest &request) const;
    Job Plan(const ViewRequest &request, uint64_t serial);
    int FirstStep(int target) const;
    std::shared_ptr<const Frame> Execute(const Job &job);
    void Connect();
    void SendChanges(const Job &job);
    std::shared_ptr<Frame> BuildFrame(const Job &job, const Json &reply, const std::vector<uint8_t> &payload);
    std::shared_ptr<std::vector<uint8_t>> Allocate(size_t bytes);
    Plane Constant(HdFormat format, unsigned width, unsigned height, float value);
    void SetStatus(const std::string &state, const std::string &message);
    void RecordReply(const Json &reply, double bridgeMs);

    BridgeState *_state;
    const bool _final;
    double _budgetMs = 150.0;

    mutable std::mutex _mutex;
    std::condition_variable _wake;
    bool _stopping = false, _paused = false, _busy = false;
    bool _hasRequest = false;
    ViewRequest _latest;
    uint64_t _latestSerial = 0;

    // State of the last successful frame (guarded by _mutex).
    bool _renderedValid = false;
    ViewRequest _rendered;
    uint64_t _renderedVersion = 0;
    bool _complete = false;
    std::shared_ptr<const Frame> _frame;
    uint64_t _frameSerial = 0;
    // A state that failed is not retried until the view or scene changes.
    bool _failedValid = false;
    ViewRequest _failed;
    uint64_t _failedVersion = 0;
    Clock::time_point _retryAt{};

    // Render-thread state.
    Connection _connection;
    hde::SharedMapping _mapping;
    bool _replay = true;
    bool _eeveeDrawn = false;
    bool _previewShown = false;
    int _stepSamples = 0;
    double _perSampleMs = 0.0;
    Clock::time_point _connectSince{};
    bool _connecting = false;
    std::vector<std::shared_ptr<std::vector<uint8_t>>> _pool;
    std::map<std::tuple<int, unsigned, unsigned, float>, Plane> _constants;
    std::thread _thread;

    mutable std::mutex _statsMutex;
    VtDictionary _stats;
    std::string _statusState, _statusMessage;
};
} // namespace hdEevee
PXR_NAMESPACE_CLOSE_SCOPE
