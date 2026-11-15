# Clean-room consumer of an INSTALLED libsmbios package (milestone 0012,
# Story 01.0 Task 03). Deliberately has no reference to this repository's
# source tree and no whole-archive/force_load/WHOLEARCHIVE line of its own: the
# exported libsmbios::libsmbios target must carry that link option by itself.
# Driven by scripts/ci/consumer_smoke.py; never part of the main build or ctest.
cmake_minimum_required(VERSION 3.13)
project(libsmbios_consumer CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(libsmbios CONFIG REQUIRED)

add_executable(consumer main.cpp)
target_link_libraries(consumer PRIVATE libsmbios::libsmbios)
