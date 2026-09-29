#include "scene.h"
#include <cstdlib>

PXR_NAMESPACE_OPEN_SCOPE
namespace hdEevee {
namespace {
// Merge a partial primvar update into a complete snapshot state.
void MergePrimvars(Json &target, const Json &update, const char *key) {
    if (!update.contains(key)) return;
    if (!target.contains(key) || !target[key].is_object()) target[key] = Json::object();
    for (auto it = update[key].begin(); it != update[key].end(); ++it) target[key][it.key()] = it.value();
}
} // namespace

BridgeState::BridgeState() : _finalRender(std::getenv("HDEEVEE_FINAL_RENDER") != nullptr) {}

void BridgeState::Queue(Change &&change) {
    std::lock_guard<std::mutex> lock(_mutex);
    const std::string kind = change.Kind(), id = change.Id();
    if (kind.rfind("delete", 0) == 0) {
        _snapshot.erase(id);
    } else {
        Change &cached = _snapshot[id];
        const bool partial = change.json.value("primvars_partial", false);
        // A material sync replaces its representation; geometry edits are
        // deltas that may omit unchanged arrays.
        if (cached.json.empty() || kind == "material") {
            cached.json = Json::object();
            cached.blobs.clear();
        }
        for (auto it = change.json.begin(); it != change.json.end(); ++it) {
            const std::string &key = it.key();
            if (key == "primvars_partial" || key == "primvars_removed") continue;
            if (partial && (key == "uvs" || key == "attributes")) MergePrimvars(cached.json, change.json, key.c_str());
            else cached.json[key] = it.value();
        }
        if (partial && change.json.contains("primvars_removed")) {
            for (const auto &name : change.json["primvars_removed"]) {
                const std::string text = name.get<std::string>();
                for (const char *key : {"uvs", "attributes"})
                    if (cached.json.contains(key) && cached.json[key].is_object()) cached.json[key].erase(text);
            }
        }
        for (const auto &[key, blob] : change.blobs) cached.blobs[key] = blob;
        // Superseded arrays are released here, so the snapshot stays compact.
        cached.Prune();
    }
    _pending.push_back(std::move(change));
    ++_version;
}

std::vector<Change> BridgeState::TakePending() {
    std::lock_guard<std::mutex> lock(_mutex);
    std::vector<Change> result;
    result.swap(_pending);
    return result;
}

std::vector<Change> BridgeState::TakeReplay() {
    std::lock_guard<std::mutex> lock(_mutex);
    _pending.clear();
    std::vector<Change> result;
    result.reserve(_snapshot.size());
    // Materials first so bindings resolve without an extra pass.
    for (const auto &[id, change] : _snapshot) if (change.Kind() == "material") result.push_back(change);
    for (const auto &[id, change] : _snapshot) if (change.Kind() != "material") result.push_back(change);
    return result;
}

bool BridgeState::HasPending() const {
    std::lock_guard<std::mutex> lock(_mutex);
    return !_pending.empty();
}

void BridgeState::SetPrimConfig(const std::string &path, const Json *config, bool active) {
    {
        std::lock_guard<std::mutex> lock(_mutex);
        if (config) {
            _configs[path] = *config;
            if (active) _activeSettings = path;
            else if (_activeSettings == path) _activeSettings.clear();
        } else {
            _configs.erase(path);
            if (_activeSettings == path) _activeSettings.clear();
        }
    }
    UpdateMotion();
    ++_version;
}

void BridgeState::SetDelegateConfig(const Json *config) {
    {
        std::lock_guard<std::mutex> lock(_mutex);
        _delegateConfig = config ? *config : Json();
    }
    UpdateMotion();
    ++_version;
}

Json BridgeState::ActiveConfig() const {
    std::lock_guard<std::mutex> lock(_mutex);
    if (_delegateConfig.is_object() && !_delegateConfig.empty()) return _delegateConfig;
    auto it = _configs.find(_activeSettings);
    if (it != _configs.end()) return it->second;
    if (_configs.size() == 1) return _configs.begin()->second;
    return Json::object();
}

void BridgeState::UpdateMotion() {
    if (!_finalRender) { _motionExtent = 0.f; return; }
    const Json config = ActiveConfig();
    const Json render = config.value("render", Json::object());
    // The render settings node stores toggles as 0/1 numbers.
    const Json blur = render.value("use_motion_blur", Json(false));
    const bool enabled = blur.is_boolean() ? blur.get<bool>() : blur.is_number() && blur.get<double>() != 0.0;
    const Json shutter = render.value("motion_blur_shutter", Json(.5));
    _motionExtent = enabled ? std::max(1.f, shutter.is_number() ? shutter.get<float>() : .5f) : 0.f;
}
} // namespace hdEevee
PXR_NAMESPACE_CLOSE_SCOPE
