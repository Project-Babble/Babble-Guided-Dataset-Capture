cmake_minimum_required(VERSION 3.24)
project(babble_dataset_capture VERSION 0.2.1 LANGUAGES C CXX)

# Protocol version of the runtime control socket this build speaks (HANDOFF.md
# section 3); written into manifest.json so the runtime can pick a compatible
# release without downloading it.
set(CAPTURE_PROTOCOL 2)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

include(FetchContent)
set(FETCHCONTENT_QUIET ON)

# OpenXR loader, linked statically: the app must not depend on a system loader
# (the device has SteamVR's runtime but no guaranteed libopenxr_loader.so).
set(DYNAMIC_LOADER OFF CACHE BOOL "" FORCE)
set(BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(BUILD_API_LAYERS OFF CACHE BOOL "" FORCE)
set(BUILD_CONFORMANCE_TESTS OFF CACHE BOOL "" FORCE)
set(BUILD_SDK_TESTS OFF CACHE BOOL "" FORCE)
# The SDK insists on a presentation backend for its samples (not built);
# Xlib headers satisfy it (libx11-dev in CI)
set(BUILD_WITH_SYSTEM_JSONCPP OFF CACHE BOOL "" FORCE)
FetchContent_Declare(openxr
  GIT_REPOSITORY https://github.com/KhronosGroup/OpenXR-SDK.git
  GIT_TAG release-1.1.63
  GIT_SHALLOW TRUE
  EXCLUDE_FROM_ALL)
FetchContent_Declare(glm
  GIT_REPOSITORY https://github.com/g-truc/glm.git
  GIT_TAG 1.0.3
  GIT_SHALLOW TRUE
  EXCLUDE_FROM_ALL)
FetchContent_Declare(nlohmann_json
  URL https://github.com/nlohmann/json/releases/download/v3.11.3/json.tar.xz
  EXCLUDE_FROM_ALL)
FetchContent_Declare(stb
  GIT_REPOSITORY https://github.com/nothings/stb.git
  GIT_TAG 2c980bb59875b0d32144a71867fbdebb2f77cd20
  EXCLUDE_FROM_ALL)
FetchContent_MakeAvailable(openxr glm nlohmann_json stb)

find_package(Vulkan REQUIRED)
find_package(Threads REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(PIPEWIRE IMPORTED_TARGET libpipewire-0.3)

# --- shaders: GLSL -> SPIR-V, embedded as uint32 initializer lists ----------
find_program(GLSLC glslc REQUIRED)
set(SHADER_DIR ${CMAKE_CURRENT_SOURCE_DIR}/app/shaders)
set(SHADER_OUT ${CMAKE_CURRENT_BINARY_DIR}/shaders)
file(MAKE_DIRECTORY ${SHADER_OUT})
set(SHADER_INCLUDES)
foreach(shader mesh.vert mesh.frag sky.vert sky.frag)
  set(out ${SHADER_OUT}/${shader}.spv.inc)
  add_custom_command(
    OUTPUT ${out}
    COMMAND ${GLSLC} --target-env=vulkan1.1 -O -mfmt=num -o ${out} ${SHADER_DIR}/${shader}
    DEPENDS ${SHADER_DIR}/${shader}
    COMMENT "glslc ${shader}")
  list(APPEND SHADER_INCLUDES ${out})
endforeach()
add_custom_target(shaders DEPENDS ${SHADER_INCLUDES})

configure_file(app/src/version.h.in ${CMAKE_CURRENT_BINARY_DIR}/generated/version.h @ONLY)
configure_file(packaging/manifest.json.in ${CMAKE_CURRENT_BINARY_DIR}/manifest.json @ONLY)

add_library(stb_impl STATIC app/src/stb_impl.cpp)
target_include_directories(stb_impl PUBLIC ${stb_SOURCE_DIR})

add_executable(babble-dataset-capture
  app/src/main.cpp
  app/src/app.cpp
  app/src/audio.cpp
  app/src/avatar.cpp
  app/src/capture.cpp
  app/src/dialog.cpp
  app/src/flow.cpp
  app/src/meshes.cpp
  app/src/renderer.cpp
  app/src/settings.cpp
  app/src/text.cpp
  app/src/textures.cpp
  app/src/vk_context.cpp
  app/src/xr_context.cpp)
add_dependencies(babble-dataset-capture shaders)
target_include_directories(babble-dataset-capture PRIVATE
  app/src ${SHADER_OUT} ${CMAKE_CURRENT_BINARY_DIR}/generated)
target_compile_definitions(babble-dataset-capture PRIVATE
  XR_USE_GRAPHICS_API_VULKAN GLM_FORCE_DEPTH_ZERO_TO_ONE GLM_FORCE_RADIANS)
target_compile_options(babble-dataset-capture PRIVATE -Wall -Wextra -Wno-missing-field-initializers)
target_link_libraries(babble-dataset-capture PRIVATE
  openxr_loader Vulkan::Vulkan glm::glm nlohmann_json::nlohmann_json stb_impl
  Threads::Threads ${CMAKE_DL_LIBS})
if(PIPEWIRE_FOUND)
  target_link_libraries(babble-dataset-capture PRIVATE PkgConfig::PIPEWIRE)
  target_compile_definitions(babble-dataset-capture PRIVATE HAVE_PIPEWIRE=1)
else()
  message(WARNING "libpipewire-0.3 not found: audio cues are disabled in this build")
endif()
# Self-contained binary: no runtime dependency on libstdc++ versions
target_link_options(babble-dataset-capture PRIVATE -static-libstdc++ -static-libgcc)

option(BUILD_APP_TESTS "Build test helpers (tests/cpp)" ON)
if(BUILD_APP_TESTS)
  add_executable(capture_test tests/cpp/capture_test.cpp app/src/capture.cpp)
  target_include_directories(capture_test PRIVATE app/src ${CMAKE_CURRENT_BINARY_DIR}/generated)
  target_link_libraries(capture_test PRIVATE nlohmann_json::nlohmann_json)
endif()

# --- install layout = release tarball layout ----------------------------------
#   manifest.json  LICENSE  bin/babble-dataset-capture  assets/{avatar.bin,textures,fonts,routines.json}
# Component "app" only: the OpenXR SDK's own install rules (headers, static
# loader) must stay out of the tarball (`cmake --install --component app`).
set(ASSET_BUILD_DIR ${CMAKE_CURRENT_SOURCE_DIR}/dist-assets CACHE PATH
    "tools/convert_assets.py output to package")
install(TARGETS babble-dataset-capture RUNTIME DESTINATION bin COMPONENT app)
install(FILES ${CMAKE_CURRENT_BINARY_DIR}/manifest.json LICENSE DESTINATION . COMPONENT app)
install(FILES assets/routines.json DESTINATION assets COMPONENT app)
install(DIRECTORY assets/fonts DESTINATION assets COMPONENT app)
install(DIRECTORY ${ASSET_BUILD_DIR}/ DESTINATION assets OPTIONAL COMPONENT app)
install(DIRECTORY assets/narration DESTINATION assets OPTIONAL COMPONENT app)
