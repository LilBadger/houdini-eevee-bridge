#pragma once
#include "renderer.h"
#include <pxr/imaging/hd/renderBuffer.h>

PXR_NAMESPACE_OPEN_SCOPE
namespace hdEevee {

/// CPU render buffer. Presenting a frame plane of the same size and format
/// shares its storage instead of copying it.
class EeveeBuffer final : public HdRenderBuffer {
public:
    explicit EeveeBuffer(const SdfPath &id) : HdRenderBuffer(id) {}
    bool Allocate(const GfVec3i &dimensions, HdFormat format, bool multiSampled) override;
    unsigned int GetWidth() const override { return _width; }
    unsigned int GetHeight() const override { return _height; }
    unsigned int GetDepth() const override { return 1; }
    HdFormat GetFormat() const override { return _format; }
    bool IsMultiSampled() const override { return false; }
    void *Map() override { ++_mapped; return _pixels ? _pixels->data() : nullptr; }
    void Unmap() override { --_mapped; }
    bool IsMapped() const override { return _mapped > 0; }
    void Resolve() override {}
    bool IsConverged() const override { return _converged; }

    void SetConverged(bool converged) { _converged = converged; }
    /// Show a plane, resizing (nearest) or converting it only when needed.
    void Present(const Plane &plane);
    /// Fill every component with a value (for example -1 for missing IDs).
    void Fill(float value);
    /// Incremented by Allocate; lets the pass re-present after a resize.
    uint64_t Generation() const { return _generation; }

protected:
    void _Deallocate() override;

private:
    unsigned int _width = 0, _height = 0;
    HdFormat _format = HdFormatInvalid;
    std::shared_ptr<std::vector<uint8_t>> _pixels;
    std::atomic<int> _mapped{0};
    bool _converged = false;
    uint64_t _generation = 0;
    bool _filled = false;      // storage holds a uniform _fillValue
    float _fillValue = 0.f;
};
} // namespace hdEevee
PXR_NAMESPACE_CLOSE_SCOPE
