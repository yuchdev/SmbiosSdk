# libsmbios_linux - milestone 0005, Story 01.1 (ADR 0001). Only added
# by the root CMakeLists.txt's elseif(CMAKE_SYSTEM_NAME STREQUAL "Linux")
# branch, which also sets SMBIOS_LEGACY_LIB to libsmbios_posix_legacy.
set(TARGET libsmbios_linux)

file(GLOB SOURCES src/*.cpp ${CMAKE_CURRENT_SOURCE_DIR}/include/smbios/linux/*.hpp)

add_library(${TARGET} STATIC ${SOURCES})
set_target_properties(${TARGET} PROPERTIES PREFIX "")

target_include_directories(${TARGET} PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    $<INSTALL_INTERFACE:include>)

# PUBLIC libsmbios_posix_legacy: UnixNativeOrLegacyScanSource's internal
# fallback (linux_source_chain.cpp) needs LegacyMemoryScanSource.
target_link_libraries(${TARGET} PUBLIC sys_interface libsmbios_posix_legacy)

target_compile_options(${TARGET} PRIVATE ${SMBIOS_COMPILE_OPTIONS} ${SMBIOS_WARNING_OPTIONS})

# Milestone 0012, Story 01.0, Task 01: join the libsmbiosTargets export set
# and install this library's public headers (include/smbios/<segment>/...).
install(TARGETS ${TARGET} EXPORT libsmbiosTargets
    ARCHIVE DESTINATION lib
    LIBRARY DESTINATION lib
    RUNTIME DESTINATION bin)
install(DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/include/smbios
    DESTINATION include)
