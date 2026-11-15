set(TARGET libsmbios)

# file(GLOB) is non-recursive, so this only ever picks up *.cpp sitting
# directly in libsmbios/src/ itself (the SMBios class, the entry-type
# classes, the factory) - never any of the sibling per-OS/legacy/
# sys_interface top-level libraries, each of which globs its own directory
# in its own CMakeLists.txt.
file(GLOB SOURCES ${CMAKE_CURRENT_SOURCE_DIR}/src/*.cpp
        ${CMAKE_CURRENT_SOURCE_DIR}/include/libsmbios/*.hpp
        ${CMAKE_CURRENT_SOURCE_DIR}/include/libsmbios/tables/*.hpp
        ${CMAKE_CURRENT_SOURCE_DIR}/include/libsmbios/formatters/*.hpp)

# fallback_source_chain.cpp supplies make_default_source_chain() only for
# platforms with no per-OS library at all (SMBIOS_PLATFORM_LIB empty - BSD/
# Solaris/other *nix, set by the root CMakeLists.txt's else() branch) -
# remove it from the default glob here and re-add it explicitly only in
# that case, so it never coexists in the same build as libsmbios_win/
# libsmbios_macos/libsmbios_linux's own definition of the same symbol
# (which would otherwise be a duplicate-definition link error on every
# platform that *does* have a per-OS library).
list(REMOVE_ITEM SOURCES ${CMAKE_CURRENT_SOURCE_DIR}/src/fallback_source_chain.cpp)

include_directories(${Boost_INCLUDE_DIRS}
        ${CMAKE_CURRENT_SOURCE_DIR}/include)

if(NOT SMBIOS_PLATFORM_LIB)
    # BSD/Solaris/other *nix: no per-OS library exists for these targets
    # (ADR 0001) - fallback_source_chain.cpp (removed from SOURCES above,
    # re-added only here) supplies make_default_source_chain() directly
    # inside libsmbios core, reproducing today's real behavior on these
    # platforms exactly (legacy scan only).
    list(APPEND SOURCES ${CMAKE_CURRENT_SOURCE_DIR}/src/fallback_source_chain.cpp)
endif()

add_library(${TARGET} ${SOURCES})
set_target_properties(${TARGET} PROPERTIES PREFIX "")

target_compile_options(${TARGET} PRIVATE ${SMBIOS_COMPILE_OPTIONS} ${SMBIOS_WARNING_OPTIONS})
target_link_options(${TARGET} PRIVATE ${SMBIOS_LINK_OPTIONS})

target_link_libraries(${TARGET} PUBLIC sys_interface ${SMBIOS_LEGACY_LIB} ${SMBIOS_PLATFORM_LIB})

# Milestone 0012, Story 01.0, Task 01: first install rules libsmbios has had.
# include_directories() above is directory-scoped and exports nothing, so the
# public header root is also declared on the target for consumers.
target_include_directories(${TARGET} PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    $<INSTALL_INTERFACE:include>)
target_compile_features(${TARGET} PUBLIC cxx_std_20)

set(LIBSMBIOS_REQUIRES_LIBCXX OFF)
if(CMAKE_CXX_COMPILER_ID MATCHES "Clang" AND CMAKE_SYSTEM_NAME STREQUAL "Linux")
    set(LIBSMBIOS_REQUIRES_LIBCXX ON)
    target_compile_options(${TARGET} INTERFACE -stdlib=libc++)
    target_link_options(${TARGET} INTERFACE -stdlib=libc++)
endif()

install(TARGETS ${TARGET} EXPORT libsmbiosTargets
    ARCHIVE DESTINATION lib
    LIBRARY DESTINATION lib
    RUNTIME DESTINATION bin)
# One recursive copy covers libsmbios/{,tables/,formatters/}*.hpp.
install(DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/include/libsmbios
    DESTINATION include)

# ---------------------------------------------------------------------------
# Milestone 0012, Story 01.0, Task 02: exported package (contract C1).
#
# REGISTER_SMBIOS_ENTRY / REGISTER_OUTPUT_FORMATTER run from static
# initializers that only execute for object files actually linked in, so a
# consumer that links libsmbios.a normally silently gets a nullptr from
# SMBiosEntryFactory::create() for every type. The whole-archive flag is
# therefore part of the exported target's INTERFACE, keyed the same
# three-way way smbios/src/CMakeLists.txt keys its own PRIVATE one.
#
# INSTALL_INTERFACE only: this repo's own consumers (smbios, smbios_c, the
# tests) already apply whole-archive themselves, and CMake does NOT rewrite a
# bare $<TARGET_FILE:libsmbios> to the imported name on export (it would name a
# target that does not exist in a consumer's build), so the installed form
# spells libsmbios::libsmbios explicitly.
# ---------------------------------------------------------------------------
if(APPLE)
    target_link_options(${TARGET} INTERFACE
        "$<INSTALL_INTERFACE:LINKER:-force_load,$<TARGET_FILE:libsmbios::libsmbios>>")
elseif(MSVC)
    target_link_options(${TARGET} INTERFACE
        "$<INSTALL_INTERFACE:/WHOLEARCHIVE:$<TARGET_FILE:libsmbios::libsmbios>>")
else()
    target_link_options(${TARGET} INTERFACE
        "$<INSTALL_INTERFACE:LINKER:--whole-archive,$<TARGET_FILE:libsmbios::libsmbios>,--no-whole-archive>")
endif()

# Independent of SMBIOS_C_VERSION (no shared version policy yet); both are
# checked by scripts/ci/check_versions.py via scripts/ci/version_sites.json.
set(LIBSMBIOS_VERSION "1.0.0")

include(CMakePackageConfigHelpers)
write_basic_package_version_file(
    ${CMAKE_CURRENT_BINARY_DIR}/libsmbiosConfigVersion.cmake
    VERSION ${LIBSMBIOS_VERSION}
    COMPATIBILITY SameMajorVersion)
configure_package_config_file(
    ${CMAKE_CURRENT_SOURCE_DIR}/cmake/libsmbiosConfig.cmake.in
    ${CMAKE_CURRENT_BINARY_DIR}/libsmbiosConfig.cmake
    INSTALL_DESTINATION lib/cmake/libsmbios)

install(EXPORT libsmbiosTargets
    FILE libsmbiosTargets.cmake
    NAMESPACE libsmbios::
    DESTINATION lib/cmake/libsmbios)
install(FILES
    ${CMAKE_CURRENT_BINARY_DIR}/libsmbiosConfig.cmake
    ${CMAKE_CURRENT_BINARY_DIR}/libsmbiosConfigVersion.cmake
    DESTINATION lib/cmake/libsmbios)
