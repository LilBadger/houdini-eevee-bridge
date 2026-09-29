#include "buffer.h"
#include <pxr/base/gf/half.h>

PXR_NAMESPACE_OPEN_SCOPE
namespace hdEevee {
namespace {
float Component(const uint8_t *pixel, HdFormat component, size_t index) {
    switch (component) {
    case HdFormatFloat32: return reinterpret_cast<const float*>(pixel)[index];
    case HdFormatFloat16: { GfHalf h; h.setBits(reinterpret_cast<const uint16_t*>(pixel)[index]); return float(h); }
    case HdFormatInt32: return float(reinterpret_cast<const int32_t*>(pixel)[index]);
    case HdFormatUNorm8: return pixel[index] / 255.f;
    default: return 0.f;
    }
}

void Store(uint8_t *pixel, HdFormat component, size_t index, float value) {
    switch (component) {
    case HdFormatFloat32: reinterpret_cast<float*>(pixel)[index] = value; break;
    case HdFormatFloat16: reinterpret_cast<uint16_t*>(pixel)[index] = GfHalf(value).bits(); break;
    case HdFormatInt32: reinterpret_cast<int32_t*>(pixel)[index] = int32_t(std::lround(value)); break;
    case HdFormatUNorm8: pixel[index] = uint8_t(std::clamp(value, 0.f, 1.f) * 255.f + .5f); break;
    default: break;
    }
}
} // namespace

bool EeveeBuffer::Allocate(const GfVec3i &d, HdFormat format, bool) {
    if (d[0] <= 0 || d[1] <= 0 || d[0] > 16384 || d[1] > 16384 || d[2] != 1) return false;
    if (format != HdFormatFloat32Vec4 && format != HdFormatFloat16Vec4 && format != HdFormatFloat32Vec3 &&
        format != HdFormatFloat32 && format != HdFormatInt32 && format != HdFormatFloat16 &&
        format != HdFormatFloat16Vec3) return false;
    _width = unsigned(d[0]);
    _height = unsigned(d[1]);
    _format = format;
    _pixels = std::make_shared<std::vector<uint8_t>>(size_t(_width) * _height * HdDataSizeOfFormat(format), 0);
    _filled = true;
    _fillValue = 0.f;
    if (format == HdFormatInt32) Fill(-1.f);
    _converged = false;
    ++_generation;
    return true;
}

void EeveeBuffer::_Deallocate() {
    _pixels.reset();
    _width = _height = 0;
    _format = HdFormatInvalid;
}

void EeveeBuffer::Fill(float value) {
    if (!_pixels || (_filled && _fillValue == value)) return;
    const size_t channels = HdGetComponentCount(_format), pixelBytes = HdDataSizeOfFormat(_format);
    const HdFormat component = HdGetComponentFormat(_format);
    if (_pixels.use_count() != 1) _pixels = std::make_shared<std::vector<uint8_t>>(_pixels->size());
    uint8_t *data = _pixels->data();
    for (size_t i = 0, n = size_t(_width) * _height; i < n; ++i)
        for (size_t c = 0; c < channels; ++c) Store(data + i * pixelBytes, component, c, value);
    _filled = true;
    _fillValue = value;
}

void EeveeBuffer::Present(const Plane &plane) {
    if (!plane.pixels || !_width || !_height) return;
    _filled = false;
    if (plane.width == _width && plane.height == _height && plane.format == _format) {
        _pixels = plane.pixels;
        return;
    }
    // A resize or format change raced this frame: convert with nearest
    // sampling so the old image stays visible until a matching frame arrives.
    auto pixels = std::make_shared<std::vector<uint8_t>>(size_t(_width) * _height * HdDataSizeOfFormat(_format));
    const size_t inChannels = HdGetComponentCount(plane.format), outChannels = HdGetComponentCount(_format);
    const HdFormat inComponent = HdGetComponentFormat(plane.format), outComponent = HdGetComponentFormat(_format);
    const size_t inBytes = HdDataSizeOfFormat(plane.format), outBytes = HdDataSizeOfFormat(_format);
    for (unsigned y = 0; y < _height; ++y) {
        const unsigned sy = std::min(plane.height - 1, unsigned((y + .5) * plane.height / _height));
        for (unsigned x = 0; x < _width; ++x) {
            const unsigned sx = std::min(plane.width - 1, unsigned((x + .5) * plane.width / _width));
            const uint8_t *in = plane.pixels->data() + (size_t(sy) * plane.width + sx) * inBytes;
            uint8_t *out = pixels->data() + (size_t(y) * _width + x) * outBytes;
            for (size_t c = 0; c < outChannels; ++c)
                Store(out, outComponent, c, c < inChannels ? Component(in, inComponent, c) : (c == 3 ? 1.f : 0.f));
        }
    }
    _pixels = std::move(pixels);
}
} // namespace hdEevee
PXR_NAMESPACE_CLOSE_SCOPE
