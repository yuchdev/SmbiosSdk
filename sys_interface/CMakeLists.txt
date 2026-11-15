# sys_interface (was smbios_source_interface) - milestone 0005, Story 01.1
# (ADR 0001); relocated to the repo root as part of the build-layout
# restructure that renamed smbios -> libsmbios and gave every per-OS/legacy
# library its own top-level directory.
#
# This is an INTERFACE library, not STATIC: every file it owns
# (ismbios_source.hpp, smbios_source_chain.hpp, byte_reader.hpp, smbios_anchor.hpp,
# smbios_entry_point.hpp, smbios_version_fixup.hpp) is header-only - there is no
# .cpp to compile, so there is no artifact for a STATIC library to produce.
# This is NOT a deviation from CLAUDE.md's "static libraries, not
# shared/dynamic" convention: that convention exists to avoid .so/.dll
# *runtime* linkage, which is orthogonal to CMake's INTERFACE target kind.
# INTERFACE here means "propagate an include directory to whoever links
# this; build nothing of its own" - the correct CMake target kind for a
# header-only library, not an oversight.
add_library(sys_interface INTERFACE)

# Absolute source paths are illegal in an exported target, so the include
# directory differs between the build tree and the installed package
# (milestone 0012, Story 01.0: libsmbiosTargets export set).
target_include_directories(sys_interface INTERFACE
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    $<INSTALL_INTERFACE:include>)

# Header-only: no ARCHIVE/LIBRARY/RUNTIME destination, only the export entry
# (milestone 0012, Story 01.0, Task 01).
install(TARGETS sys_interface EXPORT libsmbiosTargets)
install(DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/include/smbios
    DESTINATION include)
