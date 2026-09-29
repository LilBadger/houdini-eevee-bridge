#pragma once
// Local IPC is TCP on both platforms. Unix sockets remain a Linux compatibility path.
// Rendered pixels arrive through a shared-memory segment created by the worker.
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <process.h>
#else
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <arpa/inet.h>
#include <netinet/tcp.h>
#include <unistd.h>
#endif
#include <algorithm>
#include <cerrno>
#include <climits>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <stdexcept>
#include <string>

namespace hde {
#ifdef _WIN32
using Socket = SOCKET;
constexpr Socket invalidSocket = INVALID_SOCKET;
inline void initializeSockets() {
    static const bool initialized = [] { WSADATA data; return WSAStartup(MAKEWORD(2,2), &data) == 0; }();
    if (!initialized) throw std::runtime_error("Cannot initialize Windows sockets");
}
inline void closeSocket(Socket fd) { if (fd != invalidSocket) closesocket(fd); }
inline void interruptSocket(Socket fd) { if (fd != invalidSocket) shutdown(fd, SD_BOTH); }
inline int lastError() { return WSAGetLastError(); }
inline bool interrupted() { return lastError() == WSAEINTR; }
inline uint64_t processId() { return _getpid(); }
#else
using Socket = int;
constexpr Socket invalidSocket = -1;
inline void initializeSockets() {}
inline void closeSocket(Socket fd) { if (fd != invalidSocket) ::close(fd); }
inline void interruptSocket(Socket fd) { if (fd != invalidSocket) ::shutdown(fd, SHUT_RDWR); }
inline int lastError() { return errno; }
inline bool interrupted() { return errno == EINTR; }
inline uint64_t processId() { return ::getpid(); }
#endif
inline std::string socketError() { return "socket error " + std::to_string(lastError()); }
inline void noDelay(Socket fd) {
    int value=1;
    setsockopt(fd,IPPROTO_TCP,TCP_NODELAY,reinterpret_cast<const char*>(&value),sizeof(value));
}
inline void timeouts(Socket fd, int seconds) {
#ifdef _WIN32
    DWORD timeout = DWORD(seconds * 1000);
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&timeout), sizeof(timeout));
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, reinterpret_cast<const char*>(&timeout), sizeof(timeout));
#else
    timeval timeout{seconds,0};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
#endif
}
inline void transfer(Socket fd, void *data, size_t bytes, bool sending) {
    auto *p = static_cast<char*>(data);
    while (bytes) {
        int count = int(std::min(bytes, size_t(INT_MAX)));
#ifdef _WIN32
        int n = sending ? ::send(fd,p,count,0) : ::recv(fd,p,count,0);
#else
        auto n = sending ? ::send(fd,p,count,MSG_NOSIGNAL) : ::recv(fd,p,count,0);
#endif
        if (n < 0 && interrupted()) continue;
        if (n <= 0) throw std::runtime_error("EEVEE worker connection closed: " + socketError());
        p += n;
        bytes -= size_t(n);
    }
}
inline std::filesystem::path environmentPath(const char *name) {
#ifdef _WIN32
    const std::string narrow(name);
    const std::wstring wide(narrow.begin(),narrow.end());
    const wchar_t *value=_wgetenv(wide.c_str());
    return value ? std::filesystem::path(value) : std::filesystem::path();
#else
    const char *value=std::getenv(name);
    return value ? std::filesystem::path(value) : std::filesystem::path();
#endif
}
inline std::string pathString(const std::filesystem::path &path) {
    auto value=path.generic_u8string();
    return std::string(reinterpret_cast<const char*>(value.data()),value.size());
}
inline std::filesystem::path cacheDirectory(const std::string &name) {
    auto session=environmentPath("HDEEVEE_SESSION_DIR");
    auto base = !session.empty() ? session :
        std::filesystem::temp_directory_path() / ("houdini-eevee-" + std::to_string(processId()));
    auto path = base / "cache" / name;
    std::filesystem::create_directories(path);
    return path;
}

/// Read-only view of a worker shared-memory segment. The worker replaces the
/// segment (with a new name) when a larger image needs more space.
class SharedMapping {
public:
    SharedMapping() = default;
    SharedMapping(const SharedMapping&) = delete;
    SharedMapping &operator=(const SharedMapping&) = delete;
    ~SharedMapping() { close(); }

    const uint8_t *map(const std::string &name, size_t size) {
        if (name == _name && size <= _size && _data) return _data;
        close();
        if (name.empty() || name.size() > 200 || name.find('\\') != std::string::npos)
            throw std::runtime_error("Invalid EEVEE shared-memory name");
#ifdef _WIN32
        const std::wstring wide(name.begin(), name.end());
        _handle = OpenFileMappingW(FILE_MAP_READ, FALSE, wide.c_str());
        if (!_handle) throw std::runtime_error("Cannot open EEVEE shared memory " + name);
        void *data = MapViewOfFile(_handle, FILE_MAP_READ, 0, 0, size);
        if (!data) { CloseHandle(_handle); _handle = nullptr; throw std::runtime_error("Cannot map EEVEE shared memory"); }
        MEMORY_BASIC_INFORMATION info{};
        if (!VirtualQuery(data, &info, sizeof(info)) || info.RegionSize < size) {
            UnmapViewOfFile(data); CloseHandle(_handle); _handle = nullptr;
            throw std::runtime_error("EEVEE shared memory is smaller than its image");
        }
#else
        if (name.front() != '/' || name.find('/', 1) != std::string::npos)
            throw std::runtime_error("Invalid EEVEE shared-memory name");
        const int fd = shm_open(name.c_str(), O_RDONLY, 0);
        if (fd < 0) throw std::runtime_error("Cannot open EEVEE shared memory " + name + ": " + std::strerror(errno));
        struct stat info{};
        if (fstat(fd, &info) != 0 || size_t(info.st_size) < size) {
            ::close(fd);
            throw std::runtime_error("EEVEE shared memory is smaller than its image");
        }
        void *data = mmap(nullptr, size, PROT_READ, MAP_SHARED, fd, 0);
        ::close(fd);
        if (data == MAP_FAILED) throw std::runtime_error("Cannot map EEVEE shared memory");
#endif
        _data = static_cast<const uint8_t*>(data);
        _size = size;
        _name = name;
        return _data;
    }

    void close() {
        if (_data) {
#ifdef _WIN32
            UnmapViewOfFile(_data);
#else
            munmap(const_cast<uint8_t*>(_data), _size);
#endif
        }
#ifdef _WIN32
        if (_handle) CloseHandle(_handle);
        _handle = nullptr;
#endif
        _data = nullptr;
        _size = 0;
        _name.clear();
    }

private:
    const uint8_t *_data = nullptr;
    size_t _size = 0;
    std::string _name;
#ifdef _WIN32
    HANDLE _handle = nullptr;
#endif
};
} // namespace hde
