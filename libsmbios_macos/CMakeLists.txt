# libsmbios_macos - milestone 0005, Story 01.1 (ADR 0001). Only added
# by the root CMakeLists.txt's elseif(APPLE) branch, which also sets
# SMBIOS_LEGACY_LIB to libsmbios_posix_legacy. Story 03.0, Task 02 adds
# MacOsUefiSource, macOS's first native ISmbiosSource; macos_source_chain.cpp's
# make_default_source_chain() is now {MacOsUefiSource, LegacyMemoryScanSource}.
set(TARGET libsmbios_macos)

file(GLOB SOURCES src/*.cpp ${CMAKE_CURRENT_SOURCE_DIR}/include/smbios/macos/*.hpp)

add_library(${TARGET} STATIC ${SOURCES})
set_target_properties(${TARGET} PROPERTIES PREFIX "")

target_include_directories(${TARGET} PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    $<INSTALL_INTERFACE:include>)

target_link_libraries(${TARGET} PUBLIC sys_interface libsmbios_posix_legacy)

# Milestone 0005, Story 03.0, Task 01: macos_efi_reader.cpp reads SMBIOS
# properties off the IORegistry via IOKit/CoreFoundation - system
# frameworks, not vcpkg packages. This library only builds under the root
# CMakeLists.txt's elseif(APPLE) branch, so no extra __APPLE__ guard is
# needed here.
find_library(IOKIT_FRAMEWORK IOKit REQUIRED)
find_library(COREFOUNDATION_FRAMEWORK CoreFoundation REQUIRED)
# find_library() resolves to an absolute path under this machine's SDK, which
# an installed package must not bake in (milestone 0012, Story 01.0): the
# exported form names the frameworks, resolved by the consumer's own SDK.
# (-Wl,-framework,X is one token: CMake splits a spaced "-framework X" item
# and turns the second half into -lX.)
target_link_libraries(${TARGET} PUBLIC
    $<BUILD_INTERFACE:${IOKIT_FRAMEWORK}>
    $<BUILD_INTERFACE:${COREFOUNDATION_FRAMEWORK}>
    $<INSTALL_INTERFACE:-Wl,-framework,IOKit>
    $<INSTALL_INTERFACE:-Wl,-framework,CoreFoundation>)

target_compile_options(${TARGET} PRIVATE ${SMBIOS_COMPILE_OPTIONS} ${SMBIOS_WARNING_OPTIONS})

# Milestone 0012, Story 01.0, Task 01: join the libsmbiosTargets export set
# and install this library's public headers (include/smbios/<segment>/...).
install(TARGETS ${TARGET} EXPORT libsmbiosTargets
    ARCHIVE DESTINATION lib
    LIBRARY DESTINATION lib
    RUNTIME DESTINATION bin)
install(DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/include/smbios
    DESTINATION include)
