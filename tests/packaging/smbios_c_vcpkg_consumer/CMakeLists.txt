# Clean-room consumer of the smbios-c vcpkg port (milestone 0012, Story 02.0
# Task 03). No reference to this repository's source tree: the package arrives
# only through vcpkg.json + vcpkg-configuration.json (the self-hosted registry),
# which scripts/ci/vcpkg_registry_smoke.py writes into a scratch copy of this
# directory. Never part of the main build or ctest.
cmake_minimum_required(VERSION 3.13)
project(smbios_c_vcpkg_consumer C)

# The vcpkg toolchain file (passed by the driver) puts the installed triplet
# tree on the search paths; smbios_c ships a pkg-config file, not a CMake
# package, so locate it by header and library.
find_path(SMBIOS_C_INCLUDE_DIR smbios/smbios.h REQUIRED)
find_path(SMBIOS_C_EXPORT_DIR smbios_c/smbios_c_export.h REQUIRED)
find_library(SMBIOS_C_LIBRARY NAMES smbios_c REQUIRED)

add_executable(consumer main.c)
target_include_directories(consumer PRIVATE ${SMBIOS_C_INCLUDE_DIR} ${SMBIOS_C_EXPORT_DIR})
target_link_libraries(consumer PRIVATE ${SMBIOS_C_LIBRARY})
