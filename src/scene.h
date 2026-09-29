#pragma once
// Scene changes queued by Hydra prim syncs, plus the latest complete state of
// every prim for replaying into a new or restarted worker session.
#include "protocol.h"
#include <pxr/imaging/hd/renderDelegate.h>
#include <mutex>

PXR_NAMESPACE_OPEN_SCOPE
namespace hdEevee {

class BridgeState final : public HdRenderParam {
public:
    BridgeState();

    /// Record a prim edit. Called concurrently from Hydra's sync threads.
    void Queue(Change &&change);
    /// Move the pending edits out (render thread).
    std::vector<Change> TakePending();
    /// Every prim's latest complete state; clears pending edits (reconnect).
    std::vector<Change> TakeReplay();
    bool HasPending() const;
    uint64_t Version() const { return _version.load(); }
    void Touch() { ++_version; }

    /// Render settings: Stage prims (eevee:config) and delegate settings.
    void SetPrimConfig(const std::string &path, const Json *config, bool active);
    void SetDelegateConfig(const Json *config);
    Json ActiveConfig() const;
    /// Shutter span in frames, or zero when motion samples are not needed.
    /// Viewports use EEVEE's own motion history; only final renders with
    /// motion blur enabled sample Hydra's shutter.
    float MotionExtent() const { return _motionExtent.load(); }
    bool FinalRender() const { return _finalRender; }

private:
    void UpdateMotion();

    mutable std::mutex _mutex;
    std::vector<Change> _pending;
    std::map<std::string, Change> _snapshot;
    std::atomic<uint64_t> _version{0};
    std::map<std::string, Json> _configs;
    std::string _activeSettings;
    Json _delegateConfig;
    bool _finalRender = false;
    std::atomic<float> _motionExtent{0.f};
};
} // namespace hdEevee
PXR_NAMESPACE_CLOSE_SCOPE
