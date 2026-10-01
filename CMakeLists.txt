cmake_minimum_required(VERSION 3.22)
project(HoudiniEevee VERSION 0.7.5 LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
find_package(Houdini REQUIRED)
if(NOT Houdini_VERSION_MAJOR EQUAL 22)
    message(FATAL_ERROR "This bridge currently targets Houdini 22. Build against a Houdini 22 HDK.")
endif()
add_library(hdEevee SHARED
    src/delegate.cpp src/renderer.cpp src/prims.cpp src/scene.cpp src/protocol.cpp src/buffer.cpp
    src/sceneIndexPlugins.cpp)
target_include_directories(hdEevee PRIVATE third_party)
target_link_libraries(hdEevee PRIVATE Houdini Houdini::Dep::pxr_hd Houdini::Dep::pxr_hf Houdini::Dep::pxr_tf
    Houdini::Dep::pxr_gf Houdini::Dep::pxr_vt Houdini::Dep::pxr_sdf Houdini::Dep::pxr_plug
    Houdini::Dep::pxr_work Houdini::Dep::pxr_arch Houdini::Dep::pxr_hdsi Houdini::Dep::pxr_trace)
if(UNIX AND NOT APPLE)
    target_link_libraries(hdEevee PRIVATE rt)
endif()
set_target_properties(hdEevee PROPERTIES PREFIX ""
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/plugin/hdEevee/lib"
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/plugin/hdEevee/lib"
    WINDOWS_EXPORT_ALL_SYMBOLS ON)
if(WIN32)
    # USD headers instantiate pxr_boost::python/CPython calls inline; Linux resolves
    # them at load time from Houdini, but a Windows DLL must link them explicitly.
    target_link_libraries(hdEevee PRIVATE ws2_32 Houdini::Dep::pxr_python Houdini::Dep::python3.13)
    target_compile_definitions(hdEevee PRIVATE NOMINMAX WIN32_LEAN_AND_MEAN)
endif()
foreach(config Debug Release RelWithDebInfo MinSizeRel)
    string(TOUPPER "${config}" upper)
    set_target_properties(hdEevee PROPERTIES
        LIBRARY_OUTPUT_DIRECTORY_${upper} "${CMAKE_BINARY_DIR}/plugin/hdEevee/lib"
        RUNTIME_OUTPUT_DIRECTORY_${upper} "${CMAKE_BINARY_DIR}/plugin/hdEevee/lib")
endforeach()
file(MAKE_DIRECTORY "${CMAKE_BINARY_DIR}/plugin/hdEevee/resources")
configure_file(src/plugInfo.json.in "${CMAKE_BINARY_DIR}/plugin/hdEevee/resources/plugInfo.json" @ONLY)
configure_file(src/build-info.json.in "${CMAKE_BINARY_DIR}/plugin/hdEevee/build-info.json" @ONLY)
add_executable(hde_volume src/volumeCompose.cpp)
target_include_directories(hde_volume PRIVATE third_party)
target_link_libraries(hde_volume PRIVATE Houdini Houdini::Dep::openvdb_sesi)
set_target_properties(hde_volume PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
foreach(config Debug Release RelWithDebInfo MinSizeRel)
    string(TOUPPER "${config}" upper)
    set_target_properties(hde_volume PROPERTIES RUNTIME_OUTPUT_DIRECTORY_${upper} "${CMAKE_BINARY_DIR}/bin")
endforeach()
if(MSVC)
    target_compile_options(hdEevee PRIVATE /utf-8)
    target_compile_options(hde_volume PRIVATE /utf-8)
    # Houdini's USD headers use std::atomic_load on shared_ptr, which C++20 deprecates.
    # The warning repeats for every source file and buries real build errors.
    target_compile_definitions(hdEevee PRIVATE _SILENCE_CXX20_OLD_SHARED_PTR_ATOMIC_SUPPORT_DEPRECATION_WARNING)
    target_link_options(hde_volume PRIVATE /MANIFEST:EMBED "/MANIFESTINPUT:${CMAKE_CURRENT_SOURCE_DIR}/src/utf8.manifest")
endif()
install(TARGETS hdEevee LIBRARY DESTINATION plugin/hdEevee/lib RUNTIME DESTINATION plugin/hdEevee/lib)
install(TARGETS hde_volume RUNTIME DESTINATION bin)
install(FILES "${CMAKE_BINARY_DIR}/plugin/hdEevee/resources/plugInfo.json" DESTINATION plugin/hdEevee/resources)
install(FILES "${CMAKE_BINARY_DIR}/plugin/hdEevee/build-info.json" DESTINATION plugin/hdEevee)
if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/probes/registry_probe.cpp")
    add_executable(registry_probe EXCLUDE_FROM_ALL probes/registry_probe.cpp)
    target_link_libraries(registry_probe PRIVATE Houdini Houdini::Dep::pxr_hd Houdini::Dep::pxr_hf Houdini::Dep::pxr_tf Houdini::Dep::pxr_plug Houdini::Dep::python3.13)
    add_executable(hydra_harness EXCLUDE_FROM_ALL probes/hydra_harness.cpp)
    target_include_directories(hydra_harness PRIVATE third_party)
    target_link_libraries(hydra_harness PRIVATE Houdini Houdini::Dep::pxr_hd Houdini::Dep::pxr_hf Houdini::Dep::pxr_tf
        Houdini::Dep::pxr_gf Houdini::Dep::pxr_vt Houdini::Dep::pxr_sdf Houdini::Dep::pxr_plug Houdini::Dep::pxr_usd
        Houdini::Dep::pxr_usdGeom Houdini::Dep::pxr_usdShade Houdini::Dep::pxr_usdImaging Houdini::Dep::pxr_cameraUtil
        Houdini::Dep::pxr_work Houdini::Dep::pxr_python Houdini::Dep::pxr_ar Houdini::Dep::pxr_pcp Houdini::Dep::python3.13)
    if(WIN32)
        target_link_libraries(hydra_harness PRIVATE winmm)
    endif()
    add_library(volume_registry_probe SHARED EXCLUDE_FROM_ALL probes/volume_registry_probe.cpp)
    target_include_directories(volume_registry_probe PRIVATE third_party)
    target_link_libraries(volume_registry_probe PRIVATE Houdini Houdini::Dep::pxr_sdf)
endif()
