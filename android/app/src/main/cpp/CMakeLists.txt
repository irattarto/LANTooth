# Native Opus for LANTooth: the reference libopus (fetched as the official
# release tarball, pinned by SHA-256) statically linked into a small JNI
# library, liblantooth_opus.so. See NativeOpus.kt / Codec.kt.
cmake_minimum_required(VERSION 3.22)
project(lantooth_opus C)

include(FetchContent)

set(OPUS_BUILD_SHARED_LIBRARY OFF CACHE BOOL "" FORCE)
set(OPUS_BUILD_TESTING OFF CACHE BOOL "" FORCE)
set(OPUS_BUILD_PROGRAMS OFF CACHE BOOL "" FORCE)
set(OPUS_INSTALL_PKG_CONFIG_MODULE OFF CACHE BOOL "" FORCE)
set(OPUS_INSTALL_CMAKE_CONFIG_MODULE OFF CACHE BOOL "" FORCE)
# The DNN features (DRED, OSCE, deep PLC) only help SILK/speech modes and add
# megabytes of model weights; the CELT-only low-delay mode we use can't use them.
set(OPUS_DRED OFF CACHE BOOL "" FORCE)
set(OPUS_OSCE OFF CACHE BOOL "" FORCE)
set(OPUS_DEEP_PLC OFF CACHE BOOL "" FORCE)

FetchContent_Declare(
    opus
    URL https://downloads.xiph.org/releases/opus/opus-1.5.2.tar.gz
    URL_HASH SHA256=65c1d2f78b9f2fb20082c38cbe47c951ad5839345876e46941612ee87f9a7ce1
    DOWNLOAD_EXTRACT_TIMESTAMP TRUE
)
FetchContent_MakeAvailable(opus)

add_library(lantooth_opus SHARED opus_jni.c)
target_link_libraries(lantooth_opus PRIVATE opus)

# Always optimize the codec, including in debug APKs, so a debug build performs
# like a release one on-device. The later -O2 overrides the Debug type's -O0.
target_compile_options(opus PRIVATE -O2)
target_compile_options(lantooth_opus PRIVATE -O2 -Wall)
# Keep libopus internal: only the Java_* JNI entry points are exported.
target_link_options(lantooth_opus PRIVATE -Wl,--exclude-libs,ALL)
