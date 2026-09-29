#include <HUSD/HUSD_GeoUtils.h>
#include <HUSD/XUSD_LockedGeoRegistry.h>
#include <pxr/usd/sdf/layer.h>
#include <GU/GU_Detail.h>
#include <nlohmann/json.hpp>
#include <string>
PXR_NAMESPACE_USING_DIRECTIVE
extern "C" const char *inspect_volume_registry(const char *path) {
    static std::string text;
    nlohmann::json result=nlohmann::json::array();
    std::string asset;
    SdfLayer::FileFormatArguments args;
    SdfLayer::SplitIdentifier(path,&asset,&args);
    std::vector<std::string> candidates{asset};
    if(asset.ends_with(".volumes")) {asset.resize(asset.size()-8);candidates.push_back(asset);}
    if(asset.ends_with(".sop")) {asset.resize(asset.size()-4);candidates.push_back(asset);}
    auto copy=candidates;
    for(auto &p:copy)if(p.rfind("op:",0)==0)candidates.push_back(p.substr(3));
    for(auto &p:candidates) {
        auto handle=XUSD_LockedGeoRegistry::getGeometry(UT_StringHolder(p),args);
        GU_DetailHandleAutoReadLock lock(handle);
        result.push_back({{"path",p},{"registry",lock.getGdp()?int(lock.getGdp()->getNumPrimitives()):-1}});
    }
    text=result.dump();return text.c_str();
}
