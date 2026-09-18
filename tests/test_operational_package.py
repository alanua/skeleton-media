from __future__ import annotations

import importlib

import pytest


@pytest.mark.parametrize(
    "name",
    [
        "player",
        "media_state",
        "iptv",
        "site_registry",
        "media_discovery",
        "native_app_update_manifest",
        "media_release_monitor",
        "media_search",
        "resolver",
        "trakt_sync",
        "volume_api",
    ],
)
def test_cast_module_is_importable_from_package(name: str) -> None:
    module = importlib.import_module(f"skeleton_media.cast.{name}")
    assert module.__name__ == f"skeleton_media.cast.{name}"


@pytest.mark.parametrize(
    "name",
    [
        "player",
        "media_state",
        "iptv",
        "site_registry",
        "media_discovery",
        "native_app_update_manifest",
        "media_release_monitor",
        "media_search",
        "resolver",
        "trakt_sync",
        "volume_api",
    ],
)
def test_legacy_runtime_shim_delegates_to_package(name: str) -> None:
    shim = importlib.import_module(name)
    implementation = importlib.import_module(f"skeleton_media.cast.{name}")
    assert shim is implementation


@pytest.mark.parametrize(
    "name",
    [
        "player",
        "media_state",
        "iptv",
        "site_registry",
        "media_discovery",
        "native_app_update_manifest",
        "media_release_monitor",
        "media_search",
        "resolver",
        "trakt_sync",
        "volume_api",
    ],
)
def test_legacy_runtime_shim_supports_script_entrypoint(name: str) -> None:
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "ops"
        / "skeleton_cast"
        / "runtime"
        / f"{name}.py"
    ).read_text()
    assert 'runpy.run_module(_TARGET, run_name="__main__")' in source
