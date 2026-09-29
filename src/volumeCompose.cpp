// Compose volume fields using Houdini's OpenVDB ABI, outside Blender's process.
#include <openvdb/openvdb.h>
#include <openvdb/io/File.h>
#include <nlohmann/json.hpp>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <set>

using Json = nlohmann::json;
using Matrix = openvdb::math::Mat4d;

Matrix matrix(const Json &rows) {
    Matrix result = Matrix::identity();
    for (int r=0;r<4;++r) for (int c=0;c<4;++c) result(r,c)=rows.at(r).at(c).get<double>();
    return result;
}

int main(int argc, char **argv) {
    try {
        if (argc!=3) throw std::runtime_error("Usage: hde_volume fields.json output.vdb");
        std::ifstream input(std::filesystem::u8path(argv[1]));
        const auto definition=Json::parse(input);
        openvdb::initialize();
        auto inverse=matrix(definition.at("transform")).inverse();
        openvdb::GridPtrVec grids;
        std::set<std::string> names;
        for (const auto &field:definition.at("fields")) {
            auto alias=field.at("alias").get<std::string>();
            if (!names.insert(alias).second) throw std::runtime_error("Duplicate volume field: "+alias);
            openvdb::io::File source(field.at("file").get<std::string>());
            source.open();
            auto grid=source.readGrid(field.at("name").get<std::string>());
            source.close();
            if (!grid) throw std::runtime_error("Missing volume grid: "+alias);
            grid->setName(alias);
            if (!grid->transform().isLinear()) throw std::runtime_error("Non-linear VDB transforms are unsupported");
            auto relative=matrix(field.at("transform"))*inverse;
            auto affine=grid->transform().baseMap()->getAffineMap()->getMat4();
            grid->setTransform(openvdb::math::Transform::createLinearTransform(affine*relative));
            grids.push_back(grid);
        }
        if (grids.empty()) throw std::runtime_error("No volume fields to compose");
        openvdb::io::File output(argv[2]);
        output.write(grids);
        output.close();
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "EEVEE volume: " << error.what() << '\n';
        return 1;
    }
}
