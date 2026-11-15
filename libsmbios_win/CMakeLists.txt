# libsmbios_win - milestone 0005, Story 01.1 (ADR 0001). Only
# added by the root CMakeLists.txt's if(WIN32) branch, which also sets
# SMBIOS_LEGACY_LIB to libsmbios_win_legacy. Public headers stay at the
# smbios/windows/... segment (unchanged) even though the library/directory
# itself uses the "_win" abbreviation.
set(TARGET libsmbios_win)

file(GLOB SOURCES src/*.cpp ${CMAKE_CURRENT_SOURCE_DIR}/include/smbios/windows/*.hpp)

add_library(${TARGET} STATIC ${SOURCES})
set_target_properties(${TARGET} PROPERTIES PREFIX "")

target_include_directories(${TARGET} PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    $<INSTALL_INTERFACE:include>)

# PUBLIC libsmbios_win_legacy: windows_native_source.cpp's
# make_default_source_chain() needs LegacyMemoryScanSource as the chain's
# second entry (milestone 0005, Story 04.0 - a separate top-level entry,
# not WindowsNativeSource's own internal fallback as it was before).
target_link_libraries(${TARGET} PUBLIC sys_interface libsmbios_win_legacy)

target_compile_options(${TARGET} PRIVATE ${SMBIOS_COMPILE_OPTIONS} ${SMBIOS_WARNING_OPTIONS})

# Milestone 0012, Story 01.0, Task 01: join the libsmbiosTargets export set
# and install this library's public headers (include/smbios/<segment>/...).
install(TARGETS ${TARGET} EXPORT libsmbiosTargets
    ARCHIVE DESTINATION lib
    LIBRARY DESTINATION lib
    RUNTIME DESTINATION bin)
install(DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/include/smbios
    DESTINATION include)
