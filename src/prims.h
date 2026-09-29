#pragma once
// Hydra prims that translate scene data into worker changes.
#include "scene.h"
#include <pxr/imaging/hd/basisCurves.h>
#include <pxr/imaging/hd/camera.h>
#include <pxr/imaging/hd/field.h>
#include <pxr/imaging/hd/instancer.h>
#include <pxr/imaging/hd/light.h>
#include <pxr/imaging/hd/material.h>
#include <pxr/imaging/hd/mesh.h>
#include <pxr/imaging/hd/renderSettings.h>
#include <pxr/imaging/hd/volume.h>
#include <pxr/base/gf/matrix4d.h>
#include <pxr/base/vt/array.h>
#include <set>

PXR_NAMESPACE_OPEN_SCOPE
namespace hdEevee {

Json MatrixJson(const GfMatrix4d &m);
Json ValueJson(const VtValue &v);

class EeveeInstancer final : public HdInstancer {
public:
    EeveeInstancer(HdSceneDelegate *d, const SdfPath &id, BridgeState *state) : HdInstancer(d, id), _state(state) {}
    void Sync(HdSceneDelegate *d, HdRenderParam*, HdDirtyBits *bits) override;
    void SampleTimes(std::set<float> &times, int depth = 0);
    VtMatrix4dArray Transforms(const SdfPath &prototype, int depth = 0,
                               float time = std::numeric_limits<float>::quiet_NaN());
private:
    BridgeState *_state;
};

/// Content hashes of the arrays last sent for one prim; unchanged arrays are
/// not re-sent even when Hydra marks them dirty.
class SentArrays {
public:
    bool Changed(const std::string &key, uint64_t hash) {
        auto it = _hashes.find(key);
        if (it != _hashes.end() && it->second == hash) return false;
        _hashes[key] = hash;
        return true;
    }
    void Forget(const std::string &key) { _hashes.erase(key); }
    void Clear() { _hashes.clear(); }
private:
    std::map<std::string, uint64_t> _hashes;
};

class EeveeMesh final : public HdMesh {
public:
    EeveeMesh(const SdfPath &id, BridgeState *state) : HdMesh(id), _state(state) {}
    ~EeveeMesh() override;
    HdDirtyBits GetInitialDirtyBitsMask() const override { return HdChangeTracker::AllDirty; }
    void Sync(HdSceneDelegate *d, HdRenderParam*, HdDirtyBits *bits, const TfToken&) override;
protected:
    HdDirtyBits _PropagateDirtyBits(HdDirtyBits bits) const override { return bits; }
    void _InitRepr(const TfToken &repr, HdDirtyBits*) override;
private:
    void SyncInstances(HdSceneDelegate *d, Change &change);
    bool SyncPrimvars(HdSceneDelegate *d, Change &change, bool force);
    BridgeState *_state;
    bool _synced = false, _instanced = false;
    SentArrays _sent;
    std::set<std::string> _primvars;
    std::string _normalsInterpolation;
};

class EeveeCurves final : public HdBasisCurves {
public:
    EeveeCurves(const SdfPath &id, BridgeState *state) : HdBasisCurves(id), _state(state) {}
    ~EeveeCurves() override;
    HdDirtyBits GetInitialDirtyBitsMask() const override { return HdChangeTracker::AllDirty; }
    void Sync(HdSceneDelegate *d, HdRenderParam*, HdDirtyBits *bits, const TfToken&) override;
protected:
    HdDirtyBits _PropagateDirtyBits(HdDirtyBits bits) const override { return bits; }
    void _InitRepr(const TfToken &repr, HdDirtyBits*) override;
private:
    BridgeState *_state;
};

class EeveeVolume final : public HdVolume {
public:
    EeveeVolume(const SdfPath &id, BridgeState *state) : HdVolume(id), _state(state) {}
    ~EeveeVolume() override;
    HdDirtyBits GetInitialDirtyBitsMask() const override { return HdChangeTracker::AllDirty; }
    void Sync(HdSceneDelegate *d, HdRenderParam*, HdDirtyBits *bits, const TfToken&) override;
protected:
    HdDirtyBits _PropagateDirtyBits(HdDirtyBits bits) const override { return bits; }
    void _InitRepr(const TfToken &repr, HdDirtyBits*) override;
private:
    BridgeState *_state;
};

class EeveeLight final : public HdLight {
public:
    EeveeLight(const SdfPath &id, const TfToken &type, BridgeState *state) : HdLight(id), _type(type), _state(state) {}
    ~EeveeLight() override;
    HdDirtyBits GetInitialDirtyBitsMask() const override { return AllDirty; }
    void Sync(HdSceneDelegate *d, HdRenderParam*, HdDirtyBits *bits) override;
private:
    TfToken _type;
    BridgeState *_state;
};

class EeveeCamera final : public HdCamera {
public:
    EeveeCamera(const SdfPath &id, BridgeState *state) : HdCamera(id), _state(state) {}
    void Sync(HdSceneDelegate *d, HdRenderParam *param, HdDirtyBits *bits) override;
    Json Samples() const;
private:
    BridgeState *_state;
    mutable std::mutex _mutex;
    Json _samples = Json::array();
};

class EeveeField final : public HdField {
public:
    EeveeField(const SdfPath &id, BridgeState *state) : HdField(id), _state(state) {}
    ~EeveeField() override;
    HdDirtyBits GetInitialDirtyBitsMask() const override { return AllDirty; }
    void Sync(HdSceneDelegate *d, HdRenderParam*, HdDirtyBits *bits) override;
private:
    BridgeState *_state;
};

class EeveeMaterial final : public HdMaterial {
public:
    EeveeMaterial(const SdfPath &id, BridgeState *state) : HdMaterial(id), _state(state) {}
    ~EeveeMaterial() override;
    HdDirtyBits GetInitialDirtyBitsMask() const override { return AllDirty; }
    void Sync(HdSceneDelegate *d, HdRenderParam*, HdDirtyBits *bits) override;
private:
    BridgeState *_state;
};

class EeveeRenderSettings final : public HdRenderSettings {
public:
    EeveeRenderSettings(const SdfPath &id, BridgeState *state) : HdRenderSettings(id), _state(state) {}
    ~EeveeRenderSettings() override;
protected:
    void _Sync(HdSceneDelegate*, HdRenderParam*, const HdDirtyBits*) override;
private:
    BridgeState *_state;
};
} // namespace hdEevee
PXR_NAMESPACE_CLOSE_SCOPE
