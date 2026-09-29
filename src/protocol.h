#pragma once
// Worker protocol 2: framed JSON headers with binary array payloads.
//
// Each frame is a uint32 little-endian header length, a UTF-8 JSON header and
// header["payload"] bytes. Large arrays never become JSON text: a header
// references them as {"$b": "<key>"} and header["blobs"][key] gives
// [offset, bytes, dtype, shape] inside the payload.
#include "bridgePlatform.h"
#include <pxr/pxr.h>
#include <pxr/base/vt/value.h>
#include <nlohmann/json.hpp>
#include <atomic>
#include <filesystem>
#include <map>
#include <memory>
#include <string>
#include <vector>

PXR_NAMESPACE_OPEN_SCOPE
namespace hdEevee {
using Json = nlohmann::json;

/// An immutable typed array. It either borrows a Vt array's storage (kept
/// alive by `owner`) or owns converted bytes.
struct Blob {
    std::string dtype;              // "f4", "f8", "i4", "u4", "u1"
    std::vector<int64_t> shape;
    const uint8_t *data = nullptr;
    size_t size = 0;                // bytes
    uint64_t hash = 0;
    VtValue owner;
    std::vector<uint8_t> storage;
};
using BlobPtr = std::shared_ptr<const Blob>;

/// Wrap bytes that stay valid while `owner` is held (no copy).
BlobPtr BorrowBlob(const VtValue &owner, const void *data, size_t bytes, std::string dtype, std::vector<int64_t> shape);
/// Take ownership of converted bytes.
BlobPtr OwnBlob(std::vector<uint8_t> &&bytes, std::string dtype, std::vector<int64_t> shape);
template <class T>
BlobPtr CopyBlob(const std::vector<T> &values, std::string dtype, std::vector<int64_t> shape) {
    std::vector<uint8_t> bytes(values.size() * sizeof(T));
    if (!bytes.empty()) std::memcpy(bytes.data(), values.data(), bytes.size());
    return OwnBlob(std::move(bytes), std::move(dtype), std::move(shape));
}

/// One prim edit: JSON whose array fields reference blobs.
struct Change {
    Json json = Json::object();
    std::map<std::string, BlobPtr> blobs;

    Json Ref(const BlobPtr &blob);
    std::string Kind() const { return json.value("kind", ""); }
    std::string Id() const { return json.value("id", ""); }
    /// Drop blobs no longer referenced by `json`.
    void Prune();
};

/// Collect every blob key referenced from a JSON tree.
void CollectRefs(const Json &value, std::vector<std::string> &keys);

/// A connection to the local worker. Owned by one thread; Interrupt() may be
/// called from another thread to abort a blocking read.
class Connection {
public:
    ~Connection() { Close(); }
    bool IsOpen() const { return _fd.load() != hde::invalidSocket; }
    /// Connect using HDEEVEE_ENDPOINT (TCP descriptor) or HDEEVEE_SOCKET.
    void Open(int timeoutSeconds);
    void SetTimeout(int seconds);
    /// Send a header (token and protocol added) and the payload made of the
    /// referenced blobs of `changes`.
    void Send(Json header, const std::vector<const Change*> &changes = {});
    /// Receive a reply header; inline payload bytes are stored in `payload`.
    Json Receive(std::vector<uint8_t> *payload);
    void Interrupt();
    void Close();
    const std::string &Token() const { return _token; }

private:
    std::atomic<hde::Socket> _fd{hde::invalidSocket};
    std::string _token;
};

/// Serialize a JSON header to its framed bytes.
std::string FrameHeader(const Json &header);
} // namespace hdEevee
PXR_NAMESPACE_CLOSE_SCOPE
