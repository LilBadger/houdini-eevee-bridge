#include "protocol.h"
#include <pxr/base/arch/hash.h>
#include <fstream>
#include <unordered_set>

PXR_NAMESPACE_OPEN_SCOPE
namespace hdEevee {
namespace {
std::atomic<uint64_t> blobSerial{0};
constexpr size_t kAlign = 16;
constexpr uint32_t kMaxReplyHeader = 64u * 1024u * 1024u;
constexpr uint64_t kMaxInlinePayload = 4ull * 1024ull * 1024ull * 1024ull;
const uint8_t kZeros[kAlign] = {};

uint64_t HashBytes(const uint8_t *data, size_t size, const std::string &dtype) {
    uint64_t hash = ArchHash64(reinterpret_cast<const char*>(data), size);
    return ArchHash64(dtype.data(), dtype.size(), hash);
}
} // namespace

BlobPtr BorrowBlob(const VtValue &owner, const void *data, size_t bytes, std::string dtype, std::vector<int64_t> shape) {
    auto blob = std::make_shared<Blob>();
    blob->owner = owner;
    blob->data = static_cast<const uint8_t*>(data);
    blob->size = bytes;
    blob->dtype = std::move(dtype);
    blob->shape = std::move(shape);
    blob->hash = HashBytes(blob->data, blob->size, blob->dtype);
    return blob;
}

BlobPtr OwnBlob(std::vector<uint8_t> &&bytes, std::string dtype, std::vector<int64_t> shape) {
    auto blob = std::make_shared<Blob>();
    blob->storage = std::move(bytes);
    blob->data = blob->storage.data();
    blob->size = blob->storage.size();
    blob->dtype = std::move(dtype);
    blob->shape = std::move(shape);
    blob->hash = HashBytes(blob->data, blob->size, blob->dtype);
    return blob;
}

Json Change::Ref(const BlobPtr &blob) {
    const std::string key = "b" + std::to_string(++blobSerial);
    blobs[key] = blob;
    return Json{{"$b", key}};
}

void CollectRefs(const Json &value, std::vector<std::string> &keys) {
    if (value.is_object()) {
        if (value.size() == 1) {
            auto it = value.find("$b");
            if (it != value.end() && it->is_string()) { keys.push_back(it->get<std::string>()); return; }
        }
        for (const auto &item : value) if (item.is_structured()) CollectRefs(item, keys);
    } else if (value.is_array()) {
        for (const auto &item : value) {
            if (!item.is_structured()) break;   // numeric/string arrays hold no references
            CollectRefs(item, keys);
        }
    }
}

void Change::Prune() {
    std::vector<std::string> keys;
    CollectRefs(json, keys);
    std::unordered_set<std::string> used(keys.begin(), keys.end());
    for (auto it = blobs.begin(); it != blobs.end();)
        it = used.count(it->first) ? std::next(it) : blobs.erase(it);
}

std::string FrameHeader(const Json &header) {
    std::string text = header.dump();
    if (text.size() > 0xffffffffu) throw std::runtime_error("EEVEE request header is too large");
    const uint32_t length = uint32_t(text.size());
    std::string framed(4, '\0');
    std::memcpy(framed.data(), &length, 4);
    return framed + text;
}

void Connection::Open(int timeoutSeconds) {
    if (IsOpen()) return;
    hde::initializeSockets();
    hde::Socket fd = hde::invalidSocket;
    int status = -1;
    if (std::getenv("HDEEVEE_ENDPOINT")) {
        std::ifstream file(hde::environmentPath("HDEEVEE_ENDPOINT"));
        if (!file) throw std::runtime_error("EEVEE worker is not ready yet (no endpoint)");
        Json data = Json::parse(file, nullptr, false);
        if (data.is_discarded()) throw std::runtime_error("EEVEE worker endpoint is incomplete");
        if (data.value("host", "") != "127.0.0.1") throw std::runtime_error("EEVEE only connects to loopback workers");
        const int port = data.value("port", 0);
        _token = data.value("token", "");
        if (port < 1 || port > 65535 || _token.size() < 32) throw std::runtime_error("Invalid EEVEE endpoint");
        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_port = htons(static_cast<uint16_t>(port));
        inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);
        fd = ::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        if (fd == hde::invalidSocket) throw std::runtime_error("Cannot create EEVEE socket");
        hde::noDelay(fd);
        hde::timeouts(fd, timeoutSeconds);
        status = ::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));
    } else {
#ifdef _WIN32
        throw std::runtime_error("HDEEVEE_ENDPOINT is unset; start the EEVEE worker");
#else
        const char *name = std::getenv("HDEEVEE_SOCKET");
        if (!name) throw std::runtime_error("EEVEE worker is not started");
        sockaddr_un addr{};
        addr.sun_family = AF_UNIX;
        if (std::strlen(name) >= sizeof(addr.sun_path)) throw std::runtime_error("EEVEE socket path is too long");
        std::strcpy(addr.sun_path, name);
        fd = ::socket(AF_UNIX, SOCK_STREAM, 0);
        if (fd == hde::invalidSocket) throw std::runtime_error("Cannot create EEVEE socket");
        hde::timeouts(fd, timeoutSeconds);
        status = ::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));
        _token.clear();
#endif
    }
    if (status != 0) {
        const std::string reason = hde::socketError();
        hde::closeSocket(fd);
        throw std::runtime_error("Cannot connect to EEVEE worker: " + reason);
    }
    _fd = fd;
}

void Connection::SetTimeout(int seconds) {
    if (IsOpen()) hde::timeouts(_fd.load(), seconds);
}

void Connection::Send(Json header, const std::vector<const Change*> &changes) {
    header["protocol"] = 2;
    if (!_token.empty()) header["token"] = _token;
    // Lay out every referenced blob once, aligned for zero-copy NumPy views.
    Json table = Json::object();
    std::vector<const Blob*> order;
    uint64_t offset = 0;
    for (const Change *change : changes) {
        std::vector<std::string> keys;
        CollectRefs(change->json, keys);
        for (const auto &key : keys) {
            if (table.contains(key)) continue;
            auto it = change->blobs.find(key);
            if (it == change->blobs.end()) throw std::runtime_error("EEVEE change references a missing array");
            offset = (offset + kAlign - 1) / kAlign * kAlign;
            const Blob &blob = *it->second;
            table[key] = Json::array({offset, blob.size, blob.dtype, blob.shape});
            order.push_back(&blob);
            offset += blob.size;
        }
    }
    if (!table.empty()) header["blobs"] = std::move(table);
    header["payload"] = offset;
    std::string framed = FrameHeader(header);
    const hde::Socket fd = _fd.load();
    hde::transfer(fd, framed.data(), framed.size(), true);
    uint64_t written = 0;
    for (const Blob *blob : order) {
        const uint64_t aligned = (written + kAlign - 1) / kAlign * kAlign;
        if (aligned != written) hde::transfer(fd, const_cast<uint8_t*>(kZeros), size_t(aligned - written), true);
        if (blob->size) hde::transfer(fd, const_cast<uint8_t*>(blob->data), blob->size, true);
        written = aligned + blob->size;
    }
}

Json Connection::Receive(std::vector<uint8_t> *payload) {
    const hde::Socket fd = _fd.load();
    uint32_t length = 0;
    hde::transfer(fd, &length, 4, false);
    if (length > kMaxReplyHeader) throw std::runtime_error("Invalid EEVEE reply length");
    std::string text(length, '\0');
    hde::transfer(fd, text.data(), length, false);
    Json reply = Json::parse(text);
    const uint64_t size = reply.value("payload", uint64_t(0));
    if (size > kMaxInlinePayload) throw std::runtime_error("Invalid EEVEE reply payload");
    if (payload) {
        payload->resize(size_t(size));
        if (size) hde::transfer(fd, payload->data(), size_t(size), false);
    } else if (size) {
        std::vector<uint8_t> discard(static_cast<size_t>(size));
        hde::transfer(fd, discard.data(), discard.size(), false);
    }
    return reply;
}

void Connection::Interrupt() {
    hde::interruptSocket(_fd.load());
}

void Connection::Close() {
    const hde::Socket fd = _fd.exchange(hde::invalidSocket);
    hde::closeSocket(fd);
}
} // namespace hdEevee
PXR_NAMESPACE_CLOSE_SCOPE
