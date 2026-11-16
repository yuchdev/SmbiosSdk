# smbios-c: the C ABI shim (smbios_c) over libsmbios, built from the whole
# SmbiosSdk repository (milestone 0012, Story 02.0). smbios_c is a
# subdirectory of the root CMakeLists.txt, not a standalone CMake project, so
# the port configures the root project unmodified and installs what it
# installs; the repository's own CMakeLists.txt files are not edited for it.
#
# REF is a full commit SHA of the source repository (vcpkg_from_git has no
# SHA512: the commit hash is the integrity check). It is bumped, together with
# "version" in vcpkg.json and the registry's versions/ files, by the release
# procedure in docs/dev/vcpkg-registry.md.
# smbios_c is a SHARED library by construction (smbios_c/CMakeLists.txt): a
# static triplet cannot request a static build of it, and vcpkg rejects DLLs
# in a static-linkage tree on Windows.
vcpkg_check_linkage(ONLY_DYNAMIC_LIBRARY)

vcpkg_from_git(
    OUT_SOURCE_PATH SOURCE_PATH
    URL https://github.com/yuchdev/SmbiosSdk.git
    REF 0bfc9a0bb6acc14cc43e0e02777be31b02ce351b
    HEAD_REF master
)

vcpkg_cmake_configure(
    SOURCE_PATH "${SOURCE_PATH}"
    OPTIONS
        # The library-consumer example programs are not part of the package.
        -DSMBIOS_BUILD_EXAMPLES=OFF
        # Never let a package build create or delete git tags (root
        # CMakeLists.txt's build-versioning side effect; OFF by default, made
        # explicit so a future default change cannot reach a port build).
        -DGIT_TAG=OFF
)

vcpkg_cmake_install()
# Windows: keep the debug/release PDBs next to their DLLs (a no-op elsewhere).
vcpkg_copy_pdbs()

# Ship the C ABI only. The root install also carries the libsmbios C++ static
# libraries, headers and CMake package (Story 01.0), which smbios_c already
# links in whole (smbios_c/CMakeLists.txt): they are not a second package
# surface here, and the C++ surface is a non-goal of this port (ADR 0004).
# lib/cmake held only the libsmbios package, so the whole directory goes (vcpkg
# rejects empty installed directories).
file(REMOVE_RECURSE "${CURRENT_PACKAGES_DIR}/lib/cmake"
                    "${CURRENT_PACKAGES_DIR}/debug/lib/cmake")
foreach(_dir "" "debug/")
    foreach(_lib libsmbios libsmbios_linux libsmbios_macos libsmbios_win
                 libsmbios_posix_legacy libsmbios_win_legacy)
        file(REMOVE "${CURRENT_PACKAGES_DIR}/${_dir}lib/${_lib}.a"
                    "${CURRENT_PACKAGES_DIR}/${_dir}lib/${_lib}.lib")
    endforeach()
endforeach()
file(REMOVE_RECURSE "${CURRENT_PACKAGES_DIR}/include/libsmbios")
foreach(_segment sys_interface legacy linux macos windows)
    file(REMOVE_RECURSE "${CURRENT_PACKAGES_DIR}/include/smbios/${_segment}")
endforeach()

# smbios_c.pc is installed by the repository's own install() rules; this only
# rewrites its prefix for the vcpkg layout, never its contents or location
# (lib/pkgconfig/smbios_c.pc, milestone 0007).
vcpkg_fixup_pkgconfig()

# Headers are identical in the debug tree.
file(REMOVE_RECURSE "${CURRENT_PACKAGES_DIR}/debug/include")

vcpkg_install_copyright(FILE_LIST "${SOURCE_PATH}/LICENSE")
