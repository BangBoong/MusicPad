cmake_minimum_required(VERSION 3.16)
project(MusicPadNative VERSION 1.0.0 LANGUAGES CXX RC)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
    message(FATAL_ERROR "MusicPadNative targets x64 Windows only.")
endif()

include_directories(${CMAKE_CURRENT_SOURCE_DIR}/include)

# 1. Static DSP library (linked into APO and test runner)
add_library(musicpad_dsp_static STATIC
    src/musicpad_dsp.cpp
)

# 2. Shared DSP library (for Python ctypes in Virtual Cable mode)
add_library(musicpad_dsp SHARED
    src/musicpad_dsp.cpp
)
target_compile_definitions(musicpad_dsp PRIVATE MPAD_DSP_BUILD_DLL)
set_target_properties(musicpad_dsp PROPERTIES
    OUTPUT_NAME "musicpad_dsp"
    PREFIX ""
)

# 3. Production EFX APO DLL (musicpad_apo.dll)
add_library(musicpad_apo SHARED
    src/musicpad_apo.cpp
    src/musicpad_apo.rc
    src/musicpad_apo.def
)
target_link_libraries(musicpad_apo PRIVATE
    musicpad_dsp_static
    ole32
    uuid
    advapi32
    version
)
set_target_properties(musicpad_apo PROPERTIES
    OUTPUT_NAME "musicpad_apo"
    PREFIX ""
)

# 4. Native C++ Unit Test Executable
enable_testing()
add_executable(test_native
    tests/test_native.cpp
)
target_link_libraries(test_native PRIVATE
    musicpad_dsp_static
    ole32
    uuid
    advapi32
    version
)
add_test(NAME test_native COMMAND test_native WORKING_DIRECTORY $<TARGET_FILE_DIR:musicpad_apo>)
