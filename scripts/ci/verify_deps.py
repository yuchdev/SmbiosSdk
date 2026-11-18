#!/usr/bin/env python3
"""The Boost a release consumer brings, for the ``verify`` stage (milestone 0012 Story 04.0 Task 04).

``libsmbiosConfig.cmake`` calls ``find_dependency(Boost COMPONENTS iostreams)``,
so the clean-room ``find_package(libsmbios CONFIG)`` consumer needs a Boost
that is **not** part of the archive (ADR 0004's named "no dependency-graph
integration" consequence). No hosted runner image ships Boost on all three
OSes, so the ``verify`` stage provides one the same way everywhere:

- ``vcpkg`` at the pinned baseline (``bootstrap_vcpkg.py``; ADR 0005 D3) installs
  only ``boost-iostreams`` (the one component the package config asks for),
  for the leg's triplet, from a two-file manifest written into
  ``$CI_TEMP_DIR/verify-deps/manifest`` - the same Boost version the archive
  was built and tested against;
- into its own install root ``$CI_TEMP_DIR/verify-deps/installed`` (outside the
  checkout and outside ``VCPKG_ROOT``), which the verifier receives as a
  ``dependency_prefixes`` entry;
- cold: ``VCPKG_BINARY_SOURCES=clear`` and no cache restore (P15);
- Release only: an overlay triplet of the same name (the built-in triplet file
  plus ``set(VCPKG_BUILD_TYPE release)``) halves the build, because the
  consumer is built in the manifest's ``config`` (``Release``) only. That keeps
  the slowest (Windows) leg inside inventory C11's 30-minute job ceiling.

This is **not** vcpkg in the clean room: the archive itself is still found
only through ``CMAKE_PREFIX_PATH`` = the extracted directory, the C check uses
no Boost at all, every ``VCPKG_*`` variable is dropped from the consumer's
environment, and the verifier fails the run if any command line, tool output
or generated file names the vcpkg root or this repository.

A library: it prints nothing (P11). Python 3.9+ standard library only.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

#: The vcpkg triplet of each packaging leg (the build stage's default triplets).
TRIPLETS: Dict[str, str] = {"linux-x64": "x64-linux", "windows-x64": "x64-windows",
                            "macos-arm64": "arm64-osx"}
#: What the consumer's package config asks for (libsmbiosConfig.cmake.in).
DEPENDENCIES = ("boost-iostreams",)
ROOT_NAME = "verify-deps"


class DepsError(Exception):
    """The dependency prefix cannot be prepared; the message is value-free."""


def deps_root(temp_dir: Path) -> Path:
    """``$CI_TEMP_DIR/verify-deps``."""
    return temp_dir / ROOT_NAME


def manifest_dir(temp_dir: Path) -> Path:
    """Where the two-file vcpkg manifest is written."""
    return deps_root(temp_dir) / "manifest"


def install_root(temp_dir: Path) -> Path:
    """vcpkg's ``--x-install-root`` for the dependency prefix."""
    return deps_root(temp_dir) / "installed"


def installed_prefix(temp_dir: Path, leg: str) -> Path:
    """The prefix handed to the verifier (``installed/<triplet>``).

    Raises:
        DepsError: ``leg`` is not a packaging leg.
    """
    triplet = TRIPLETS.get(leg)
    if triplet is None:
        raise DepsError("dependency prefix: expected a packaging leg -> actual another leg")
    return install_root(temp_dir) / triplet


def write_manifest(temp_dir: Path, repo_root: Path) -> Path:
    """Write ``vcpkg.json`` (only :data:`DEPENDENCIES`) and the repository's default registry.

    The default registry (and so its pinned baseline) is copied from this
    repository's ``vcpkg-configuration.json``, so the Boost version equals the
    one the build stage used.

    Returns:
        The manifest directory.

    Raises:
        DepsError: The repository's configuration is unreadable or has no default registry.
    """
    try:
        root_cfg = json.loads((repo_root / "vcpkg-configuration.json").read_text(
            encoding="utf-8"))
        registry = root_cfg["default-registry"]
    except (OSError, ValueError, KeyError, TypeError):
        raise DepsError("dependency prefix: vcpkg-configuration.json has no default-registry") \
            from None
    out = manifest_dir(temp_dir)
    if out.is_symlink():
        raise DepsError("dependency prefix: manifest path is a symlink")
    out.mkdir(parents=True, exist_ok=True)
    (out / "vcpkg.json").write_text(json.dumps({"name": "libbios-verify-deps",
                                                "version": "0.0.0",
                                                "dependencies": list(DEPENDENCIES)},
                                               indent=2) + "\n", encoding="utf-8")
    (out / "vcpkg-configuration.json").write_text(
        json.dumps({"default-registry": registry, "registries": []}, indent=2) + "\n",
        encoding="utf-8")
    return out


def triplet_dir(temp_dir: Path) -> Path:
    """Where the release-only overlay triplet is written."""
    return deps_root(temp_dir) / "triplets"


def write_release_triplet(temp_dir: Path, vcpkg_root: Path, leg: str) -> Path:
    """Write ``<triplet>.cmake``: the built-in triplet plus ``VCPKG_BUILD_TYPE release``.

    Returns:
        The overlay triplet directory.

    Raises:
        DepsError: ``leg`` is not a packaging leg, or vcpkg has no such built-in triplet.
    """
    triplet = TRIPLETS.get(leg)
    if triplet is None:
        raise DepsError("dependency prefix: expected a packaging leg -> actual another leg")
    source = vcpkg_root / "triplets" / f"{triplet}.cmake"
    if source.is_symlink() or not source.is_file():
        raise DepsError(f"dependency prefix: vcpkg has no built-in triplet {triplet}")
    out = triplet_dir(temp_dir)
    if out.is_symlink():
        raise DepsError("dependency prefix: triplet path is a symlink")
    out.mkdir(parents=True, exist_ok=True)
    text = source.read_text(encoding="utf-8")
    (out / f"{triplet}.cmake").write_text(
        text.rstrip("\n") + "\n# 0012 Story 04.0 Task 04: the verify consumer is Release only.\n"
        "set(VCPKG_BUILD_TYPE release)\n", encoding="utf-8")
    return out


def install_argv(vcpkg_tool: Path, temp_dir: Path, leg: str) -> List[str]:
    """``vcpkg install`` of the manifest into :func:`install_root` for ``leg``'s triplet.

    Uses the release-only overlay triplet of :func:`write_release_triplet`.

    Raises:
        DepsError: ``leg`` is not a packaging leg.
    """
    triplet = TRIPLETS.get(leg)
    if triplet is None:
        raise DepsError("dependency prefix: expected a packaging leg -> actual another leg")
    return [str(vcpkg_tool), "install", f"--x-manifest-root={manifest_dir(temp_dir)}",
            f"--x-install-root={install_root(temp_dir)}", f"--triplet={triplet}",
            f"--overlay-triplets={triplet_dir(temp_dir)}", "--clean-after-build"]
