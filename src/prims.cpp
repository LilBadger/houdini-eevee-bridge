#include "prims.h"
#include <pxr/base/arch/hash.h>
#include <pxr/base/gf/quatd.h>
#include <pxr/base/gf/quatf.h>
#include <pxr/base/gf/vec2d.h>
#include <pxr/base/gf/vec2f.h>
#include <pxr/base/gf/vec3d.h>
#include <pxr/base/gf/vec3f.h>
#include <pxr/base/gf/vec4f.h>
#include <pxr/usd/sdf/assetPath.h>
#include <pxr/usd/sdf/layer.h>
#include <pxr/imaging/hd/changeTracker.h>
#include <pxr/imaging/hd/geomSubsetSchema.h>
#include <pxr/imaging/hd/materialBindingSchema.h>
#include <pxr/imaging/hd/materialBindingsSchema.h>
#include <pxr/imaging/hd/materialSchema.h>
#include <pxr/imaging/hd/renderIndex.h>
#include <pxr/imaging/hd/repr.h>
#include <pxr/imaging/hd/sceneDelegate.h>
#include <pxr/imaging/hd/timeSampleArray.h>
#include <pxr/imaging/hd/tokens.h>
#include <UT/UT_IStream.h>
#include <UT/UT_Ramp.h>
#include <FS/FS_Info.h>
#include <FS/FS_Reader.h>
#include <GU/GU_Detail.h>
#include <GU/GU_PrimVDB.h>
#include <GEO/GEO_PrimVolume.h>
#include <GA/GA_Handle.h>
#include <HUSD/HUSD_GeoUtils.h>
#include <HUSD/XUSD_LockedGeoRegistry.h>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <typeinfo>

PXR_NAMESPACE_OPEN_SCOPE
namespace hdEevee {
namespace {
const char *const kInterpolations[] = {"constant", "uniform", "varying", "vertex", "faceVarying"};

std::string AssetFilename(const SdfAssetPath &asset) {
    const std::string path = asset.GetResolvedPath().empty() ? asset.GetAssetPath() : asset.GetResolvedPath();
    if (path.rfind("opdef:", 0) != 0 && path.rfind("oplib:", 0) != 0) return path;
    // Embedded HDA textures are readable by Houdini's stream API, not by
    // Blender's filesystem API. Materialize their bytes once per revision.
    static std::mutex mutex;
    static std::map<std::string, std::string> cache;
    FS_Info info(path.c_str());
    const std::string key = path + '\n' + std::to_string(int64_t(info.getModTime())) + '\n' +
                            std::to_string(int64_t(info.getFileDataSize()));
    std::lock_guard<std::mutex> lock(mutex);
    auto cached = cache.find(key);
    if (cached != cache.end()) return cached->second;
    FS_Reader reader(path.c_str());
    auto *input = reader.getStream();
    if (!input || !reader.isGood()) {
        fprintf(stderr, "[EEVEE] Cannot open embedded texture: %s\n", path.c_str());
        return path;
    }
    std::string bytes;
    char buffer[65536];
    while (auto count = input->bread(buffer, sizeof(buffer))) {
        bytes.append(buffer, count);
        if (bytes.size() > 512u * 1024u * 1024u) throw std::runtime_error("Embedded EEVEE texture exceeds 512 MiB");
    }
    auto directory = hde::cacheDirectory("embedded_textures");
    std::string extension = std::filesystem::path(path).extension().string();
    if (extension.size() > 16 || extension.find_first_not_of(".abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") != std::string::npos)
        extension = ".bin";
    auto filename = directory / (std::to_string(ArchHash64(bytes.data(), bytes.size())) + extension);
    if (!std::filesystem::exists(filename)) {
        const auto temporary = filename.string() + ".tmp";
        std::ofstream output(temporary, std::ios::binary);
        output.write(bytes.data(), std::streamsize(bytes.size()));
        output.close();
        if (!output) throw std::runtime_error("Cannot cache embedded EEVEE texture");
        std::filesystem::rename(temporary, filename);
    }
    return cache[key] = hde::pathString(filename);
}

void RampSamples(const std::string &identifier, Json &parameters) {
    if (identifier != "kma_rampconst_float" && identifier != "kma_rampconst_color") return;
    const bool color = identifier == "kma_rampconst_color";
    const std::string prefix = color ? "v" : "f";
    auto keys = parameters.value(prefix + "keys", Json::array({0., 1.}));
    auto values = parameters.value(prefix + "values", color ? Json::array({{0., 0., 0.}, {1., 1., 1.}}) : Json::array({0., 1.}));
    auto bases = parameters.value(prefix + "basis", Json::array({"linear", "linear"}));
    if (keys.size() != values.size() || keys.size() != bases.size() || keys.empty()) return;
    UT_Ramp ramp;
    ramp.clearAndDestroy();
    for (size_t i = 0; i < keys.size(); ++i) {
        float v[4];
        for (int c = 0; c < 4; ++c) v[c] = color ? (c < 3 ? values[i][c].get<float>() : 1.f) : values[i].get<float>();
        auto basis = bases[i].get<std::string>();
        if (basis == "catmullrom") basis = "catmull-rom";
        if (basis == "bspline") basis = "b-spline";
        ramp.addNode(keys[i].get<float>(), v, UTsplineBasisFromName(basis.c_str()), false);
    }
    ramp.ensureRampIsBuilt();
    Json samples = Json::array();
    for (int i = 0; i < 4096; ++i) {
        float c[4];
        ramp.getColor(double(i) / 4095., c);
        samples.push_back({c[0], c[1], c[2], c[3]});
    }
    parameters["hde:ramp_samples"] = std::move(samples);
}

Json TransformSamples(HdSceneDelegate *d, const SdfPath &id, float extent) {
    Json result = Json::array();
    if (extent <= 0.f) return result;
    HdTimeSampleArray<GfMatrix4d, 4> samples;
    d->SampleTransform(id, -extent, extent, &samples);
    for (size_t i = 0; i < samples.count; ++i)
        result.push_back({{"time", samples.times[i]}, {"value", MatrixJson(samples.values[i])}});
    return result;
}

/// float3 arrays (points, normals, vectors) as a float32 [n, 3] blob.
BlobPtr Vec3Blob(const VtValue &value) {
    if (value.IsHolding<GfVec3f>()) {
        const GfVec3f v = value.UncheckedGet<GfVec3f>();
        return CopyBlob(std::vector<float>{v[0], v[1], v[2]}, "f4", {1, 3});
    }
    if (value.IsHolding<VtVec3fArray>()) {
        const auto &a = value.UncheckedGet<VtVec3fArray>();
        return BorrowBlob(value, a.cdata(), a.size() * sizeof(GfVec3f), "f4", {int64_t(a.size()), 3});
    }
    if (value.IsHolding<VtVec3dArray>()) {
        const auto &a = value.UncheckedGet<VtVec3dArray>();
        std::vector<float> out(a.size() * 3);
        for (size_t i = 0; i < a.size(); ++i) for (int c = 0; c < 3; ++c) out[i * 3 + c] = float(a[i][c]);
        return CopyBlob(out, "f4", {int64_t(a.size()), 3});
    }
    return nullptr;
}

BlobPtr Vec2Blob(const VtValue &value) {
    if (value.IsHolding<GfVec2f>()) {
        const GfVec2f v = value.UncheckedGet<GfVec2f>();
        return CopyBlob(std::vector<float>{v[0], v[1]}, "f4", {1, 2});
    }
    if (value.IsHolding<VtVec2fArray>()) {
        const auto &a = value.UncheckedGet<VtVec2fArray>();
        return BorrowBlob(value, a.cdata(), a.size() * sizeof(GfVec2f), "f4", {int64_t(a.size()), 2});
    }
    if (value.IsHolding<VtVec2dArray>()) {
        const auto &a = value.UncheckedGet<VtVec2dArray>();
        std::vector<float> out(a.size() * 2);
        for (size_t i = 0; i < a.size(); ++i) for (int c = 0; c < 2; ++c) out[i * 2 + c] = float(a[i][c]);
        return CopyBlob(out, "f4", {int64_t(a.size()), 2});
    }
    return nullptr;
}

BlobPtr FloatBlob(const VtValue &value) {
    if (value.IsHolding<VtFloatArray>()) {
        const auto &a = value.UncheckedGet<VtFloatArray>();
        return BorrowBlob(value, a.cdata(), a.size() * sizeof(float), "f4", {int64_t(a.size())});
    }
    if (value.IsHolding<VtDoubleArray>()) {
        const auto &a = value.UncheckedGet<VtDoubleArray>();
        std::vector<float> out(a.begin(), a.end());
        return CopyBlob(out, "f4", {int64_t(a.size())});
    }
    if (value.IsHolding<float>()) return CopyBlob(std::vector<float>{value.UncheckedGet<float>()}, "f4", {1});
    if (value.IsHolding<double>()) return CopyBlob(std::vector<float>{float(value.UncheckedGet<double>())}, "f4", {1});
    return nullptr;
}

BlobPtr IntBlob(const VtIntArray &array) {
    return BorrowBlob(VtValue(array), array.cdata(), array.size() * sizeof(int), "i4", {int64_t(array.size())});
}

/// Row-major USD matrices as float32 [n, 4, 4].
BlobPtr MatricesBlob(const VtMatrix4dArray &matrices, const GfMatrix4d *prefix = nullptr) {
    std::vector<float> out(matrices.size() * 16);
    for (size_t i = 0; i < matrices.size(); ++i) {
        const GfMatrix4d m = prefix ? (*prefix) * matrices[i] : matrices[i];
        const double *values = m.GetArray();
        for (int k = 0; k < 16; ++k) out[i * 16 + k] = float(values[k]);
    }
    return CopyBlob(out, "f4", {int64_t(matrices.size()), 4, 4});
}

std::string PrimvarInterpolation(HdSceneDelegate *d, const SdfPath &id, const TfToken &name) {
    for (int i = HdInterpolationConstant; i <= HdInterpolationFaceVarying; ++i)
        for (const auto &desc : d->GetPrimvarDescriptors(id, HdInterpolation(i)))
            if (desc.name == name) return kInterpolations[i];
    return "";
}

void QueueDelete(BridgeState *state, const SdfPath &id, const char *kind) {
    // Fallback prims have no path and were never sent to the worker.
    if (id.IsEmpty()) return;
    Change change;
    change.json = {{"kind", kind}, {"id", id.GetString()}};
    state->Queue(std::move(change));
}

/// Send float, float2 and float3 primvars as "attributes" and "uvs". Unchanged
/// arrays are skipped unless forced. Also reports displayColor as "color" and
/// whether it varies over the prim ("color_varying"). Returns true if sent.
bool SyncPrimvarsTo(HdSceneDelegate *d, const SdfPath &id, SentArrays &sent, std::set<std::string> &previous,
                    Change &change, bool force, const std::set<std::string> &skip) {
    Json uvs = Json::object(), attributes = Json::object();
    std::set<std::string> names;
    bool any = false;
    for (int i = HdInterpolationConstant; i <= HdInterpolationFaceVarying; ++i) {
        for (const auto &desc : d->GetPrimvarDescriptors(id, HdInterpolation(i))) {
            const std::string name = desc.name.GetString();
            if (skip.count(name)) continue;
            const VtValue value = d->Get(id, desc.name);
            BlobPtr blob;
            Json *target = &attributes;
            std::string type = "FLOAT";
            if ((blob = Vec2Blob(value))) target = &uvs;
            else if ((blob = FloatBlob(value))) type = "FLOAT";
            else if ((blob = Vec3Blob(value))) type = "FLOAT_VECTOR";
            else continue;
            names.insert(name);
            // Interpolation is part of the identity: the same values on a
            // different domain produce different Blender data.
            const uint64_t hash = blob->hash ^ (uint64_t(i + 1) * 0x9E3779B97F4A7C15ull);
            if (!sent.Changed("pv:" + name, hash) && !force) continue;
            Json entry = {{"values", change.Ref(blob)}, {"interpolation", kInterpolations[i]}};
            if (target == &attributes) entry["type"] = type;
            (*target)[name] = std::move(entry);
            any = true;
            if (desc.name == HdTokens->displayColor && value.IsHolding<VtVec3fArray>() &&
                !value.UncheckedGet<VtVec3fArray>().empty()) {
                const auto &colors = value.UncheckedGet<VtVec3fArray>();
                change.json["color"] = {colors[0][0], colors[0][1], colors[0][2]};
                change.json["color_varying"] = i != HdInterpolationConstant && colors.size() > 1;
            }
        }
    }
    Json removed = Json::array();
    for (const auto &name : previous) if (!names.count(name)) { removed.push_back(name); sent.Forget("pv:" + name); }
    if (!any && removed.empty() && !force) return false;
    change.json["uvs"] = std::move(uvs);
    change.json["attributes"] = std::move(attributes);
    if (!force) {
        change.json["primvars_partial"] = true;
        change.json["primvars_removed"] = std::move(removed);
    }
    previous = std::move(names);
    return true;
}

/// Face subsets with their bound materials: (material path, face indices).
/// Hydra 1 delegates put them on the topology; Hydra 2 scene indices expose
/// them as child "geomSubset" prims with their own material bindings.
std::vector<std::pair<SdfPath, VtIntArray>> FaceSubsets(HdSceneDelegate *d, const SdfPath &id,
                                                        const HdMeshTopology &topology) {
    std::vector<std::pair<SdfPath, VtIntArray>> result;
    for (const auto &subset : topology.GetGeomSubsets())
        if (subset.type == HdGeomSubset::TypeFaceSet && !subset.indices.empty())
            result.emplace_back(subset.materialId, subset.indices);
    if (!result.empty()) return result;
    const auto scene = d->GetRenderIndex().GetTerminalSceneIndex();
    if (!scene) return result;
    for (const SdfPath &child : scene->GetChildPrimPaths(id)) {
        const HdSceneIndexPrim prim = scene->GetPrim(child);
        if (prim.primType != HdPrimTypeTokens->geomSubset || !prim.dataSource) continue;
        const HdGeomSubsetSchema subset = HdGeomSubsetSchema::GetFromParent(prim.dataSource);
        const auto type = subset.GetType();
        const auto indices = subset.GetIndices();
        if (!type || !indices || type->GetTypedValue(0.f) != HdGeomSubsetSchemaTokens->typeFaceSet) continue;
        SdfPath material;
        const auto bindings = HdMaterialBindingsSchema::GetFromParent(prim.dataSource);
        for (const TfToken &purpose : {HdTokens->full, HdMaterialBindingsSchemaTokens->allPurpose})
            if (const auto path = bindings.GetMaterialBinding(purpose).GetPath()) {
                material = path->GetTypedValue(0.f);
                if (!material.IsEmpty()) break;
            }
        VtIntArray faces = indices->GetTypedValue(0.f);
        if (!faces.empty()) result.emplace_back(material, std::move(faces));
    }
    return result;
}

const std::set<std::string> kMeshSkip = {"points", "normals", "velocities", "accelerations", "v", "accel"};
const std::set<std::string> kPointsSkip = {"points", "widths", "normals", "velocities", "accelerations", "v", "accel"};

template <class Reprs>
void AddRepr(Reprs &reprs, const TfToken &repr) {
    for (auto &r : reprs) if (r.first == repr) return;
    reprs.emplace_back(repr, HdReprSharedPtr(new HdRepr));
}
} // namespace

Json MatrixJson(const GfMatrix4d &m) {
    Json out = Json::array();
    for (int r = 0; r < 4; ++r) out.push_back({m[r][0], m[r][1], m[r][2], m[r][3]});
    return out;
}

Json ValueJson(const VtValue &v) {
    if (v.IsHolding<float>()) return v.UncheckedGet<float>();
    if (v.IsHolding<double>()) return v.UncheckedGet<double>();
    if (v.IsHolding<int>()) return v.UncheckedGet<int>();
    if (v.IsHolding<bool>()) return v.UncheckedGet<bool>();
    if (v.IsHolding<std::string>()) return AssetFilename(SdfAssetPath(v.UncheckedGet<std::string>()));
    if (v.IsHolding<TfToken>()) return v.UncheckedGet<TfToken>().GetString();
    if (v.IsHolding<SdfAssetPath>()) return AssetFilename(v.UncheckedGet<SdfAssetPath>());
    if (v.IsHolding<GfVec2f>()) { auto p = v.UncheckedGet<GfVec2f>(); return {p[0], p[1]}; }
    if (v.IsHolding<GfVec4f>()) { auto p = v.UncheckedGet<GfVec4f>(); return {p[0], p[1], p[2], p[3]}; }
    if (v.IsHolding<GfVec3f>()) { auto p = v.UncheckedGet<GfVec3f>(); return {p[0], p[1], p[2]}; }
    if (v.IsHolding<GfVec3d>()) { auto p = v.UncheckedGet<GfVec3d>(); return {p[0], p[1], p[2]}; }
    if (v.IsHolding<GfMatrix4d>()) return MatrixJson(v.UncheckedGet<GfMatrix4d>());
    if (v.IsHolding<VtFloatArray>()) { const auto &a = v.UncheckedGet<VtFloatArray>(); return std::vector<float>(a.begin(), a.end()); }
    if (v.IsHolding<VtIntArray>()) { const auto &a = v.UncheckedGet<VtIntArray>(); return std::vector<int>(a.begin(), a.end()); }
    if (v.IsHolding<VtStringArray>()) { const auto &a = v.UncheckedGet<VtStringArray>(); return std::vector<std::string>(a.begin(), a.end()); }
    if (v.IsHolding<VtTokenArray>()) { Json a = Json::array(); for (const auto &x : v.UncheckedGet<VtTokenArray>()) a.push_back(x.GetString()); return a; }
    if (v.IsHolding<VtVec3fArray>()) {
        Json a = Json::array();
        for (const auto &p : v.UncheckedGet<VtVec3fArray>()) a.push_back({p[0], p[1], p[2]});
        return a;
    }
    return nullptr;
}

// ---------------------------------------------------------------- instancer
void EeveeInstancer::Sync(HdSceneDelegate *d, HdRenderParam*, HdDirtyBits *bits) {
    _UpdateInstancer(d, bits);
    *bits = HdChangeTracker::Clean;
}

void EeveeInstancer::SampleTimes(std::set<float> &times, int depth) {
    if (depth > 32) throw std::runtime_error("EEVEE instancer nesting exceeds 32 levels");
    const float extent = _state->MotionExtent();
    HdTimeSampleArray<GfMatrix4d, 4> xforms;
    GetDelegate()->SampleInstancerTransform(GetId(), -extent, extent, &xforms);
    for (size_t i = 0; i < xforms.count; ++i) times.insert(xforms.times[i]);
    for (const auto &key : {HdInstancerTokens->instanceTranslations, HdInstancerTokens->instanceRotations,
                            HdInstancerTokens->instanceScales, HdInstancerTokens->instanceTransforms}) {
        HdTimeSampleArray<VtValue, 4> samples;
        GetDelegate()->SamplePrimvar(GetId(), key, -extent, extent, &samples);
        for (size_t i = 0; i < samples.count; ++i) times.insert(samples.times[i]);
    }
    if (!GetParentId().IsEmpty())
        if (auto *parent = dynamic_cast<EeveeInstancer*>(GetDelegate()->GetRenderIndex().GetInstancer(GetParentId())))
            parent->SampleTimes(times, depth + 1);
}

// Hydra represents both USD native instances and point instancers through this
// interface. Compose row-vector transforms from prototype to outermost parent.
VtMatrix4dArray EeveeInstancer::Transforms(const SdfPath &prototype, int depth, float time) {
    if (depth > 32) throw std::runtime_error("EEVEE instancer nesting exceeds 32 levels");
    auto *d = GetDelegate();
    if (!d->GetVisible(GetId())) return {};
    const float extent = _state->MotionExtent();
    const auto indices = d->GetInstanceIndices(GetId(), prototype);
    auto get = [&](const TfToken &key) {
        if (std::isfinite(time) && extent > 0.f) {
            HdTimeSampleArray<VtValue, 4> samples;
            d->SamplePrimvar(GetId(), key, -extent, extent, &samples);
            if (samples.count) return samples.Resample(time);
        }
        return d->Get(GetId(), key);
    };
    const auto translations = get(HdInstancerTokens->instanceTranslations);
    const auto rotations = get(HdInstancerTokens->instanceRotations);
    const auto scales = get(HdInstancerTokens->instanceScales);
    const auto matrices = get(HdInstancerTokens->instanceTransforms);
    GfMatrix4d instancerTransform = d->GetInstancerTransform(GetId());
    if (std::isfinite(time) && extent > 0.f) {
        HdTimeSampleArray<GfMatrix4d, 4> samples;
        d->SampleInstancerTransform(GetId(), -extent, extent, &samples);
        if (samples.count) instancerTransform = samples.Resample(time);
    }
    auto vectorAt = [](const VtValue &v, int i, const GfVec3d &fallback) {
        if (v.IsHolding<VtVec3fArray>()) { auto &a = v.UncheckedGet<VtVec3fArray>(); if (i >= 0 && size_t(i) < a.size()) return GfVec3d(a[i]); }
        if (v.IsHolding<VtVec3dArray>()) { auto &a = v.UncheckedGet<VtVec3dArray>(); if (i >= 0 && size_t(i) < a.size()) return a[i]; }
        return fallback;
    };
    VtMatrix4dArray result;
    result.reserve(indices.size());
    for (int index : indices) {
        GfMatrix4d transform = instancerTransform;
        transform = GfMatrix4d(1).SetTranslate(vectorAt(translations, index, GfVec3d(0))) * transform;
        GfQuatd rotation(1);
        if (rotations.IsHolding<VtVec4fArray>()) { auto &a = rotations.UncheckedGet<VtVec4fArray>(); if (index >= 0 && size_t(index) < a.size()) rotation = GfQuatd(a[index][0], a[index][1], a[index][2], a[index][3]); }
        if (rotations.IsHolding<VtQuatfArray>()) { auto &a = rotations.UncheckedGet<VtQuatfArray>(); if (index >= 0 && size_t(index) < a.size()) rotation = GfQuatd(a[index]); }
        if (rotations.IsHolding<VtQuatdArray>()) { auto &a = rotations.UncheckedGet<VtQuatdArray>(); if (index >= 0 && size_t(index) < a.size()) rotation = a[index]; }
        transform = GfMatrix4d(1).SetRotate(rotation) * transform;
        transform = GfMatrix4d(1).SetScale(vectorAt(scales, index, GfVec3d(1))) * transform;
        if (matrices.IsHolding<VtMatrix4dArray>()) { auto &a = matrices.UncheckedGet<VtMatrix4dArray>(); if (index >= 0 && size_t(index) < a.size()) transform = a[index] * transform; }
        result.push_back(transform);
    }
    if (!GetParentId().IsEmpty()) {
        auto *parent = dynamic_cast<EeveeInstancer*>(d->GetRenderIndex().GetInstancer(GetParentId()));
        if (!parent) throw std::runtime_error("Missing EEVEE parent instancer");
        const auto outer = parent->Transforms(GetId(), depth + 1, time);
        if (result.size() * outer.size() > 50000000) throw std::runtime_error("EEVEE instance count exceeds safety limit");
        VtMatrix4dArray combined;
        combined.reserve(result.size() * outer.size());
        for (const auto &p : outer) for (const auto &child : result) combined.push_back(child * p);
        return combined;
    }
    return result;
}

// --------------------------------------------------------------------- mesh
EeveeMesh::~EeveeMesh() { QueueDelete(_state, GetId(), "delete"); }

void EeveeMesh::_InitRepr(const TfToken &repr, HdDirtyBits*) { AddRepr(_reprs, repr); }

void EeveeMesh::SyncInstances(HdSceneDelegate *d, Change &change) {
    HdInstancer::_SyncInstancerAndParents(d->GetRenderIndex(), GetInstancerId());
    auto *instancer = dynamic_cast<EeveeInstancer*>(d->GetRenderIndex().GetInstancer(GetInstancerId()));
    const GfMatrix4d prototype = d->GetTransform(GetId());
    VtMatrix4dArray transforms = instancer ? instancer->Transforms(GetId()) : VtMatrix4dArray();
    BlobPtr blob = MatricesBlob(transforms, &prototype);
    if (_sent.Changed("instances", blob->hash)) change.json["instances"] = change.Ref(blob);
    const float extent = _state->MotionExtent();
    if (!instancer || extent <= 0.f) return;
    std::set<float> times;
    instancer->SampleTimes(times);
    HdTimeSampleArray<GfMatrix4d, 4> samples;
    d->SampleTransform(GetId(), -extent, extent, &samples);
    for (size_t i = 0; i < samples.count; ++i) times.insert(samples.times[i]);
    Json values = Json::array();
    for (float time : times) {
        const GfMatrix4d transform = samples.count ? samples.Resample(time) : prototype;
        values.push_back({{"time", time}, {"value", change.Ref(MatricesBlob(instancer->Transforms(GetId(), 0, time), &transform))}});
    }
    change.json["instance_samples"] = std::move(values);
    if (!change.json.contains("instances")) change.json["instances"] = change.Ref(blob);
}

bool EeveeMesh::SyncPrimvars(HdSceneDelegate *d, Change &change, bool force) {
    return SyncPrimvarsTo(d, GetId(), _sent, _primvars, change, force, kMeshSkip);
}

void EeveeMesh::Sync(HdSceneDelegate *d, HdRenderParam*, HdDirtyBits *bits, const TfToken&) {
    const SdfPath &id = GetId();
    Change change;
    change.json = {{"kind", "mesh"}, {"id", id.GetString()}};
    const bool first = !_synced;
    const float extent = _state->MotionExtent();
    if (first || (*bits & HdChangeTracker::DirtyPrimID)) change.json["prim_id"] = GetPrimId();
    _UpdateInstancer(d, bits);
    const bool instanced = !GetInstancerId().IsEmpty();
    if (instanced) {
        if (first || !_instanced || (*bits & (HdChangeTracker::DirtyInstancer | HdChangeTracker::DirtyInstanceIndex |
                                               HdChangeTracker::DirtyTransform)))
            SyncInstances(d, change);
    } else if (_instanced) {
        change.json["instances"] = nullptr;
        _sent.Forget("instances");
    }
    _instanced = instanced;

    bool topologyChanged = false;
    if (first || (*bits & HdChangeTracker::DirtyTopology)) {
        const HdMeshTopology topology = GetMeshTopology(d);
        BlobPtr counts = IntBlob(topology.GetFaceVertexCounts());
        BlobPtr indices = IntBlob(topology.GetFaceVertexIndices());
        const std::string orientation = topology.GetOrientation().GetString();
        const std::string scheme = topology.GetScheme().GetString();
        const uint64_t hash = counts->hash ^ (indices->hash * 31) ^ std::hash<std::string>{}(orientation + scheme);
        if (_sent.Changed("topology", hash)) {
            topologyChanged = true;
            change.json["counts"] = change.Ref(counts);
            change.json["indices"] = change.Ref(indices);
            change.json["orientation"] = orientation;
            // Bilinear subdivision is still faceted; only smooth schemes shade smooth.
            change.json["smooth"] = scheme == "catmullClark" || scheme == "loop";
            change.json["subdivision_scheme"] = scheme;
        }
    }
    if (first || (*bits & (HdChangeTracker::DirtyTopology | HdChangeTracker::DirtyMaterialId))) {
        // Per-face material assignments (UsdGeomSubset "materialBind" family).
        Json subsets = Json::array();
        uint64_t hash = 0;
        for (const auto &[material, faces] : FaceSubsets(d, id, GetMeshTopology(d))) {
            BlobPtr indices = IntBlob(faces);
            hash = hash * 1099511628211ull ^ indices->hash ^ std::hash<std::string>{}(material.GetString());
            subsets.push_back({{"material", material.GetString()}, {"indices", change.Ref(indices)}});
        }
        if (_sent.Changed("subsets", hash) || topologyChanged) change.json["subsets"] = std::move(subsets);
    }
    const bool force = topologyChanged;
    if (force) {
        // A rebuilt Blender mesh has no attributes; everything is sent again.
        _sent.Forget("points");
        _sent.Forget("normals");
        for (const auto &name : _primvars) _sent.Forget("pv:" + name);
    }
    if (force || (*bits & HdChangeTracker::DirtyPoints)) {
        const VtValue points = GetPoints(d);
        BlobPtr blob = Vec3Blob(points);
        if (!blob) blob = CopyBlob(std::vector<float>{}, "f4", {0, 3});
        if (_sent.Changed("points", blob->hash) || force) change.json["points"] = change.Ref(blob);
        if (extent > 0.f) {
            HdTimeSampleArray<VtValue, 4> samples;
            d->SamplePrimvar(id, HdTokens->points, -extent, extent, &samples);
            Json values = Json::array();
            if (samples.count >= 2)
                for (size_t i = 0; i < samples.count; ++i)
                    if (BlobPtr sample = Vec3Blob(samples.values[i]))
                        values.push_back({{"time", samples.times[i]}, {"value", change.Ref(sample)}});
            change.json["point_samples"] = std::move(values);
        }
    }
    if (force || (*bits & (HdChangeTracker::DirtyTopology | HdChangeTracker::DirtySubdivTags | HdChangeTracker::DirtyDisplayStyle))) {
        const auto tags = GetSubdivTags(d);
        Json subdivision = {{"refine_level", GetDisplayStyle(d).refineLevel},
            {"boundary", tags.GetVertexInterpolationRule().GetString()},
            {"face_varying", tags.GetFaceVaryingInterpolationRule().GetString()},
            {"crease_indices", ValueJson(VtValue(tags.GetCreaseIndices()))},
            {"crease_lengths", ValueJson(VtValue(tags.GetCreaseLengths()))},
            {"crease_weights", ValueJson(VtValue(tags.GetCreaseWeights()))},
            {"corner_indices", ValueJson(VtValue(tags.GetCornerIndices()))},
            {"corner_weights", ValueJson(VtValue(tags.GetCornerWeights()))}};
        if (_sent.Changed("subdivision", std::hash<std::string>{}(subdivision.dump())) || force)
            change.json["subdivision"] = std::move(subdivision);
    }
    if (force || (*bits & (HdChangeTracker::DirtyNormals | HdChangeTracker::DirtyPrimvar | HdChangeTracker::DirtyPoints))) {
        const VtValue value = d->Get(id, HdTokens->normals);
        BlobPtr blob = Vec3Blob(value);
        // Normals only count when authored as a primvar; scene delegates may
        // return fallback values for meshes without normals.
        const std::string interpolation = blob ? PrimvarInterpolation(d, id, HdTokens->normals) : "";
        if (!blob || interpolation.empty()) blob = CopyBlob(std::vector<float>{}, "f4", {0, 3});
        const uint64_t hash = blob->hash ^ std::hash<std::string>{}(interpolation);
        if (_sent.Changed("normals", hash) || force) {
            change.json["normals"] = change.Ref(blob);
            change.json["normals_interpolation"] = interpolation;
        }
    }
    if (force || (*bits & HdChangeTracker::DirtyPrimvar)) {
        SyncPrimvars(d, change, force);
        if (extent > 0.f) {
            VtValue velocities = d->Get(id, TfToken("velocities"));
            if (velocities.IsEmpty()) velocities = d->Get(id, TfToken("v"));
            if (BlobPtr blob = Vec3Blob(velocities)) change.json["velocities"] = change.Ref(blob);
            if (BlobPtr blob = Vec3Blob(d->Get(id, TfToken("accelerations")))) change.json["accelerations"] = change.Ref(blob);
        }
    }
    if (first || (*bits & HdChangeTracker::DirtyTransform)) {
        change.json["transform"] = MatrixJson(d->GetTransform(id));
        if (extent > 0.f) change.json["transform_samples"] = TransformSamples(d, id, extent);
    }
    if (first || (*bits & HdChangeTracker::DirtyVisibility)) change.json["visible"] = d->GetVisible(id);
    if (first || (*bits & HdChangeTracker::DirtyMaterialId)) change.json["material"] = d->GetMaterialId(id).GetString();
    _synced = true;
    if (change.json.size() > 2) _state->Queue(std::move(change));
    *bits = HdChangeTracker::Clean;
}

// ------------------------------------------------------------------- curves
EeveeCurves::~EeveeCurves() { QueueDelete(_state, GetId(), "delete"); }

void EeveeCurves::_InitRepr(const TfToken &repr, HdDirtyBits*) { AddRepr(_reprs, repr); }

void EeveeCurves::Sync(HdSceneDelegate *d, HdRenderParam*, HdDirtyBits *bits, const TfToken&) {
    const SdfPath &id = GetId();
    const auto topology = GetBasisCurvesTopology(d);
    Change change;
    BlobPtr points = Vec3Blob(d->Get(id, HdTokens->points));
    if (!points) points = CopyBlob(std::vector<float>{}, "f4", {0, 3});
    change.json = {{"kind", "curves"}, {"id", id.GetString()}, {"prim_id", GetPrimId()},
        {"counts", change.Ref(IntBlob(topology.GetCurveVertexCounts()))},
        {"indices", change.Ref(IntBlob(topology.GetCurveIndices()))},
        {"points", change.Ref(points)},
        {"basis", topology.GetCurveBasis().GetString()},
        {"type", topology.GetCurveType().GetString()},
        {"wrap", topology.GetCurveWrap().GetString()},
        {"transform", MatrixJson(d->GetTransform(id))},
        {"visible", d->GetVisible(id)}, {"material", d->GetMaterialId(id).GetString()}};
    if (BlobPtr widths = FloatBlob(d->Get(id, HdTokens->widths))) change.json["widths"] = change.Ref(widths);
    change.json["widths_interpolation"] = PrimvarInterpolation(d, id, HdTokens->widths);
    // Curves are re-sent whole, so every primvar is sent with them.
    SentArrays sent;
    std::set<std::string> names;
    SyncPrimvarsTo(d, id, sent, names, change, true, kPointsSkip);
    change.json["transform_samples"] = TransformSamples(d, id, _state->MotionExtent());
    _state->Queue(std::move(change));
    *bits = HdChangeTracker::Clean;
}

// ------------------------------------------------------------------- points
EeveePoints::~EeveePoints() { QueueDelete(_state, GetId(), "delete"); }

void EeveePoints::_InitRepr(const TfToken &repr, HdDirtyBits*) { AddRepr(_reprs, repr); }

void EeveePoints::Sync(HdSceneDelegate *d, HdRenderParam*, HdDirtyBits *bits, const TfToken&) {
    // Particles usually change as a whole every frame; send the complete state.
    const SdfPath &id = GetId();
    Change change;
    BlobPtr points = Vec3Blob(d->Get(id, HdTokens->points));
    if (!points) points = CopyBlob(std::vector<float>{}, "f4", {0, 3});
    change.json = {{"kind", "points"}, {"id", id.GetString()}, {"prim_id", GetPrimId()},
        {"points", change.Ref(points)}, {"transform", MatrixJson(d->GetTransform(id))},
        {"visible", d->GetVisible(id)}, {"material", d->GetMaterialId(id).GetString()}};
    if (BlobPtr widths = FloatBlob(d->Get(id, HdTokens->widths))) change.json["widths"] = change.Ref(widths);
    change.json["widths_interpolation"] = PrimvarInterpolation(d, id, HdTokens->widths);
    SentArrays sent;
    std::set<std::string> names;
    SyncPrimvarsTo(d, id, sent, names, change, true, kPointsSkip);
    change.json["transform_samples"] = TransformSamples(d, id, _state->MotionExtent());
    _state->Queue(std::move(change));
    *bits = HdChangeTracker::Clean;
}

// ------------------------------------------------------------------- lights
EeveeLight::~EeveeLight() { QueueDelete(_state, GetId(), "delete"); }

void EeveeLight::Sync(HdSceneDelegate *d, HdRenderParam*, HdDirtyBits *bits) {
    const SdfPath &id = GetId();
    Json params = Json::object();
    for (const char *key : {"intensity", "exposure", "color", "width", "height", "radius", "length", "angle", "normalize",
                            "diffuse", "specular", "treatAsPoint", "texture:file", "texture:format", "domeOffset",
                            "enableColorTemperature", "colorTemperature", "poleAxis"}) {
        auto v = ValueJson(d->GetLightParamValue(id, TfToken(key)));
        if (!v.is_null()) params[key] = v;
    }
    Change change;
    change.json = {{"kind", "light"}, {"id", id.GetString()}, {"type", _type.GetString()},
        {"parameters", params}, {"visible", d->GetVisible(id)}, {"transform", MatrixJson(d->GetTransform(id))},
        {"transform_samples", TransformSamples(d, id, _state->MotionExtent())}};
    _state->Queue(std::move(change));
    *bits = Clean;
}

// ------------------------------------------------------------------- camera
void EeveeCamera::Sync(HdSceneDelegate *d, HdRenderParam *param, HdDirtyBits *bits) {
    {
        std::lock_guard<std::mutex> lock(_mutex);
        _samples = TransformSamples(d, GetId(), _state->MotionExtent());
    }
    HdCamera::Sync(d, param, bits);
}

Json EeveeCamera::Samples() const {
    std::lock_guard<std::mutex> lock(_mutex);
    return _samples;
}

// ------------------------------------------------------------------ volumes
namespace {
std::string VolumeFilename(const std::string &path, const std::string &name) {
    if (std::filesystem::path(path).extension() == ".vdb" && std::filesystem::is_regular_file(path)) return path;
    // HoudiniFieldAsset can reference live SOP geometry or a .bgeo cache.
    // Let Houdini read it, and convert native dense volumes to OpenVDB.
    GU_Detail geo;
    GU_ConstDetailHandle handle;
    if (path.rfind("op:", 0) == 0) {
        std::string nodepath;
        SdfLayer::FileFormatArguments arguments;
        SdfLayer::SplitIdentifier(path, &nodepath, &arguments);
        handle = XUSD_LockedGeoRegistry::getGeometry(UT_StringHolder(nodepath), arguments);
    } else handle = HUSDloadGeometryFromAsset(UT_StringRef(path));
    GU_DetailHandleAutoReadLock source(handle);
    if (!source.getGdp()) throw std::runtime_error("Cannot load Houdini volume: " + path);
    geo.merge(*source.getGdp());
    std::vector<GA_Offset> dense;
    for (GA_Iterator it(geo.getPrimitiveRange()); !it.atEnd(); ++it)
        if (dynamic_cast<const GEO_PrimVolume*>(geo.getGEOPrimitive(*it))) dense.push_back(*it);
    GA_ROHandleS names(geo.findStringTuple(GA_ATTRIB_PRIMITIVE, "name"));
    for (auto offset : dense) {
        auto *volume = dynamic_cast<const GEO_PrimVolume*>(geo.getGEOPrimitive(offset));
        std::string gridName = names.isValid() ? names.get(offset).c_str() : name;
        GU_PrimVDB::buildFromPrimVolume(geo, *volume, gridName.c_str());
        geo.destroyPrimitiveOffset(offset, true);
    }
    auto directory = hde::cacheDirectory("volume_cache");
    static std::atomic<uint64_t> serial{0};
    auto file = directory / (std::to_string(hde::processId()) + "-" + std::to_string(++serial) + ".vdb");
    if (!geo.save(hde::pathString(file).c_str(), nullptr)) throw std::runtime_error("Cannot cache Houdini volume: " + path);
    return hde::pathString(file);
}
} // namespace

EeveeField::~EeveeField() { QueueDelete(_state, GetId(), "delete_field"); }

void EeveeField::Sync(HdSceneDelegate *d, HdRenderParam*, HdDirtyBits *bits) {
    Json path = ValueJson(d->Get(GetId(), HdFieldTokens->filePath));
    Json name = ValueJson(d->Get(GetId(), HdFieldTokens->fieldName));
    if (path.is_string()) {
        const std::string fieldName = name.is_string() ? name.get<std::string>() : "density";
        Change change;
        change.json = {{"kind", "field"}, {"id", GetId().GetString()},
            {"file", VolumeFilename(path.get<std::string>(), fieldName)}, {"name", fieldName},
            {"transform", MatrixJson(d->GetTransform(GetId()))}};
        _state->Queue(std::move(change));
    }
    *bits = Clean;
}

EeveeVolume::~EeveeVolume() { QueueDelete(_state, GetId(), "delete"); }

void EeveeVolume::_InitRepr(const TfToken &repr, HdDirtyBits*) { AddRepr(_reprs, repr); }

void EeveeVolume::Sync(HdSceneDelegate *d, HdRenderParam*, HdDirtyBits *bits, const TfToken&) {
    Json fields = Json::array();
    for (const auto &field : d->GetVolumeFieldDescriptors(GetId()))
        fields.push_back({{"id", field.fieldId.GetString()}, {"name", field.fieldName.GetString()}});
    Change change;
    change.json = {{"kind", "volume"}, {"id", GetId().GetString()}, {"prim_id", GetPrimId()}, {"fields", fields},
        {"visible", d->GetVisible(GetId())}, {"material", d->GetMaterialId(GetId()).GetString()},
        {"transform", MatrixJson(d->GetTransform(GetId()))},
        {"transform_samples", TransformSamples(d, GetId(), _state->MotionExtent())}};
    _state->Queue(std::move(change));
    *bits = HdChangeTracker::Clean;
}

// ---------------------------------------------------------------- materials
EeveeMaterial::~EeveeMaterial() { QueueDelete(_state, GetId(), "delete_material"); }

void EeveeMaterial::Sync(HdSceneDelegate *d, HdRenderParam*, HdDirtyBits *bits) {
    const VtValue resource = d->GetMaterialResource(GetId());
    if (std::getenv("HDEEVEE_DEBUG_MATERIALS")) {
        if (auto scene = d->GetRenderIndex().GetTerminalSceneIndex()) {
            const auto material = HdMaterialSchema::GetFromParent(scene->GetPrim(GetId()).dataSource);
            fprintf(stderr, "[EEVEE] Material contexts for %s:", GetId().GetText());
            for (const auto &c : material.GetRenderContexts()) fprintf(stderr, " '%s'", c.GetText());
            fprintf(stderr, " delegate %s\n", typeid(*d).name());
        }
    }
    Json update = {{"kind", "material"}, {"id", GetId().GetString()}, {"parameters", Json::object()}};
    bool supported = false;
    if (resource.IsHolding<HdMaterialNetworkMap>()) {
        const auto &map = resource.UncheckedGet<HdMaterialNetworkMap>();
        for (const auto &[terminal, network] : map.map) {
            bool preview = false, compatible = true, materialx = false;
            Json nodes = Json::array(), links = Json::array();
            for (const auto &node : network.nodes) {
                const auto id = node.identifier.GetString();
                preview = preview || id == "UsdPreviewSurface";
                materialx = materialx || id.rfind("ND_", 0) == 0 || id.rfind("kma_", 0) == 0;
                compatible = compatible && (id == "UsdPreviewSurface" || id == "UsdUVTexture" ||
                                            id == "UsdTransform2d" || id.rfind("UsdPrimvarReader_", 0) == 0);
                Json parameters = Json::object();
                for (const auto &[key, value] : node.parameters) {
                    auto j = ValueJson(value);
                    if (!j.is_null()) parameters[key.GetString()] = j;
                }
                RampSamples(id, parameters);
                nodes.push_back({{"id", node.path.GetString()}, {"type", id}, {"parameters", parameters}});
                if (node.identifier == TfToken("EeveeShaderGraph")) {
                    auto it = node.parameters.find(TfToken("graph"));
                    if (it != node.parameters.end() && it->second.IsHolding<std::string>()) {
                        update["graph"] = it->second.UncheckedGet<std::string>();
                        supported = true;
                    }
                } else if (node.identifier == TfToken("UsdPreviewSurface") && network.nodes.size() == 1) {
                    for (const auto &[name, value] : node.parameters) {
                        auto j = ValueJson(value);
                        if (!j.is_null()) update["parameters"][name.GetString()] = j;
                    }
                    supported = true;
                }
            }
            if (preview && compatible && network.nodes.size() > 1) {
                for (const auto &link : network.relationships)
                    links.push_back({link.inputId.GetString(), link.inputName.GetString(), link.outputId.GetString(), link.outputName.GetString()});
                update["usd_network"] = {{"nodes", nodes}, {"links", links}};
                supported = true;
            }
            if (materialx && terminal == TfToken("surface") && !update.contains("graph")) {
                links = Json::array();
                for (const auto &link : network.relationships)
                    links.push_back({link.inputId.GetString(), link.inputName.GetString(), link.outputId.GetString(), link.outputName.GetString()});
                update["materialx_network"] = {{"nodes", nodes}, {"links", links}, {"terminal", network.nodes.back().path.GetString()}};
                supported = true;
            }
        }
    }
    if (!supported) {
        fprintf(stderr, "[EEVEE] Unsupported material graph: %s (resource %s; displaying magenta)\n",
                GetId().GetText(), resource.GetTypeName().c_str());
        if (resource.IsHolding<HdMaterialNetworkMap>())
            for (const auto &[terminal, network] : resource.UncheckedGet<HdMaterialNetworkMap>().map)
                for (const auto &node : network.nodes)
                    fprintf(stderr, "[EEVEE]   %s: %s (%s)\n", terminal.GetText(), node.path.GetText(), node.identifier.GetText());
        update["parameters"] = {{"diffuseColor", {1, 0, 1}}, {"roughness", 0.5}};
    }
    Change change;
    change.json = std::move(update);
    _state->Queue(std::move(change));
    *bits = Clean;
}

// ----------------------------------------------------------- render settings
EeveeRenderSettings::~EeveeRenderSettings() {
    if (!GetId().IsEmpty()) _state->SetPrimConfig(GetId().GetString(), nullptr, false);
}

void EeveeRenderSettings::_Sync(HdSceneDelegate*, HdRenderParam*, const HdDirtyBits*) {
    const auto &settings = GetNamespacedSettings();
    if (std::getenv("HDEEVEE_TRACE")) {
        std::ofstream trace(hde::environmentPath("HDEEVEE_TRACE").concat(".settings"), std::ios::app);
        trace << Json({{"event", "prim_settings"}, {"path", GetId().GetString()}, {"active", IsActive()},
                       {"keys", settings.size()}}).dump() << '\n';
    }
    auto it = settings.find("eevee:config");
    if (it != settings.end() && it->second.IsHolding<std::string>()) {
        const Json config = Json::parse(it->second.UncheckedGet<std::string>(), nullptr, false);
        if (!config.is_discarded()) {
            _state->SetPrimConfig(GetId().GetString(), &config, IsActive());
            return;
        }
        fprintf(stderr, "[EEVEE] Ignoring malformed eevee:config on %s\n", GetId().GetText());
    }
    _state->SetPrimConfig(GetId().GetString(), nullptr, false);
}
} // namespace hdEevee
PXR_NAMESPACE_CLOSE_SCOPE
