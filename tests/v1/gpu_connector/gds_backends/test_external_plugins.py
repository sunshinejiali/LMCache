# SPDX-License-Identifier: Apache-2.0
"""External GDS discovery using real distribution metadata and importable modules."""

# Standard
from collections.abc import Callable, Iterator
from pathlib import Path
import argparse
import sys
import textwrap
import uuid

# Third Party
import pytest
import torch

# First Party
from lmcache.v1.distributed.config import (
    EvictionConfig,
    GDSL1ManagerConfig,
    add_storage_manager_args,
)
from lmcache.v1.gpu_connector._gds_backends import available_backends, create_backend
from lmcache.v1.gpu_connector.gds_backends.base import GDSBackend

PluginInstaller = Callable[[str, str, str], str]
EXAMPLE_SOURCE = (
    "from lmcache_example_gds import Backend as ExampleBackend\n"
    "class Backend(ExampleBackend):\n"
    "    name = 'vendor_gds'\n"
)


@pytest.fixture
def install_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[PluginInstaller]:
    """Expose isolated distribution metadata without patching the loader itself."""
    monkeypatch.syspath_prepend(str(tmp_path))
    example = Path(__file__).resolve().parents[4] / "examples/gds_plugin/src"
    monkeypatch.syspath_prepend(str(example))
    modules: list[str] = []

    def install(name: str, source: str, distribution: str) -> str:
        module = f"test_gds_plugin_{uuid.uuid4().hex}"
        modules.append(module)
        (tmp_path / f"{module}.py").write_text(textwrap.dedent(source))
        info = tmp_path / f"{distribution.replace('-', '_')}-1.0.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {distribution}\nVersion: 1.0\n"
        )
        (info / "entry_points.txt").write_text(
            f"[lmcache.gds_plugins]\n{name} = {module}:Backend\n"
        )
        return module

    yield install
    for module in modules:
        sys.modules.pop(module, None)


def test_external_names_and_cli_are_lazy(
    install_plugin: PluginInstaller,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = install_plugin(
        "unavailable_gds",
        "raise ImportError('native runtime missing')",
        "missing-runtime",
    )
    assert "unavailable_gds" in available_backends()
    monkeypatch.setattr(
        "lmcache.v1.distributed.config.add_l2_adapters_args", lambda parser: None
    )
    parser = add_storage_manager_args(argparse.ArgumentParser())
    args = parser.parse_args(
        [
            "--l1-size-gb",
            "1",
            "--eviction-policy",
            "LRU",
            "--gds-l1-path",
            "/scratch",
            "--gds-l1-backend",
            "unavailable_gds",
        ]
    )
    assert args.gds_l1_backend == "unavailable_gds"
    assert "unavailable_gds" in parser.format_help()
    assert module not in sys.modules
    with pytest.raises(RuntimeError, match="unavailable_gds.*native runtime missing"):
        create_backend("unavailable_gds")


def test_selected_plugin_only_and_fresh_instances(
    install_plugin: PluginInstaller,
) -> None:
    selected = install_plugin("vendor_gds", EXAMPLE_SOURCE, "vendor-one")
    other = install_plugin("other_gds", "raise OSError('do not load')", "vendor-two")
    first = create_backend("vendor_gds")
    assert isinstance(first, GDSBackend)
    assert first.name == "vendor_gds"
    assert first is not create_backend("vendor_gds")
    assert selected in sys.modules
    assert other not in sys.modules
    with pytest.raises(NotImplementedError, match="discovery-only"):
        first.open_slab("unused", 4096, True)


def test_auto_ignores_external_default(
    install_plugin: PluginInstaller,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = EXAMPLE_SOURCE + (
        "    @classmethod\n"
        "    def is_default(cls):\n"
        "        raise AssertionError('external auto selection is forbidden')\n"
    )
    module = install_plugin("vendor_gds", source, "external-default")
    monkeypatch.setattr(torch.version, "cuda", "test")
    monkeypatch.setattr(torch.version, "hip", None)
    assert create_backend("auto").name == "cufile"
    assert module not in sys.modules


@pytest.mark.parametrize("name", ["cufile", "ugds", "auto"])
def test_reserved_names_cannot_be_overridden(
    install_plugin: PluginInstaller,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    module = install_plugin(name, "raise RuntimeError('must not import')", "collision")
    monkeypatch.setattr(torch.version, "cuda", "test")
    monkeypatch.setattr(torch.version, "hip", None)
    available_backends()
    assert create_backend(name).name == ("cufile" if name == "auto" else name)
    assert module not in sys.modules


def test_duplicate_names_fail_only_on_selection(
    install_plugin: PluginInstaller,
) -> None:
    first = install_plugin(
        "vendor_gds", "raise RuntimeError('not imported')", "vendor-one"
    )
    second = install_plugin(
        "vendor_gds", "raise RuntimeError('not imported')", "vendor-two"
    )
    assert available_backends().count("vendor_gds") == 1
    with pytest.raises(
        ValueError, match="Ambiguous GDS plugin.*vendor-one.*vendor-two"
    ):
        create_backend("vendor_gds")
    assert first not in sys.modules and second not in sys.modules


@pytest.mark.parametrize("name", ["UPPER", "bad-name", "_private", "with.dot"])
def test_invalid_metadata_names_are_not_choices(
    install_plugin: PluginInstaller,
    name: str,
) -> None:
    module = install_plugin(name, "raise RuntimeError('not imported')", "bad-name")
    assert name not in available_backends()
    with pytest.raises(ValueError, match="unsupported GDS L1 backend"):
        create_backend(name)
    assert module not in sys.modules


@pytest.mark.parametrize(
    "source,exception,match",
    [
        ("Backend = object", TypeError, "concrete GDSBackend subclass"),
        (
            "from lmcache.v1.gpu_connector.gds_backends.base import GDSBackend\n"
            "Backend = GDSBackend",
            TypeError,
            "concrete GDSBackend subclass",
        ),
        ("def Backend(): return None", TypeError, "concrete GDSBackend subclass"),
        ("Backend = None", TypeError, "concrete GDSBackend subclass"),
        ("# missing Backend attribute", RuntimeError, "Cannot load GDS plugin"),
        (
            EXAMPLE_SOURCE + "    def __init__(self, required): pass\n",
            TypeError,
            "no-argument constructor",
        ),
        (
            EXAMPLE_SOURCE + "    def __init__(self):\n"
            "        raise RuntimeError('constructor failed')\n",
            RuntimeError,
            "Cannot construct GDS plugin.*constructor failed",
        ),
        (
            EXAMPLE_SOURCE + "    name = 'different'\n",
            ValueError,
            "Backend.name must match",
        ),
        (
            EXAMPLE_SOURCE + "    def validate_environment(self):\n"
            "        raise ValueError('requires a CUDA build')\n",
            RuntimeError,
            "environment validation failed.*requires a CUDA build",
        ),
        (
            EXAMPLE_SOURCE + "    def validate_environment(self):\n"
            "        raise OSError('libvendor_gds.so missing')\n",
            RuntimeError,
            "environment validation failed.*libvendor_gds.so missing",
        ),
        (
            "raise OSError('libvendor_gds.so not found')",
            RuntimeError,
            "Cannot load GDS plugin.*libvendor_gds.so",
        ),
    ],
)
def test_selected_plugin_diagnostics(
    install_plugin: PluginInstaller,
    source: str,
    exception: type[Exception],
    match: str,
) -> None:
    install_plugin("vendor_gds", source, "vendor-one")
    with pytest.raises(exception, match=match):
        create_backend("vendor_gds")


def test_unknown_name_lists_installed_plugins(install_plugin: PluginInstaller) -> None:
    install_plugin("vendor_gds", EXAMPLE_SOURCE, "vendor-one")
    with pytest.raises(ValueError, match="Choose from:.*vendor_gds"):
        create_backend("missing_gds")


def test_tagged_config_keeps_external_backend_lazy(
    install_plugin: PluginInstaller,
) -> None:
    module = install_plugin("vendor_gds", EXAMPLE_SOURCE, "vendor-one")
    config = GDSL1ManagerConfig.from_dict(
        {
            "tag": "scratch",
            "size_gb": 1,
            "path": "/scratch",
            "align_bytes": 4096,
            "backend": "vendor_gds",
        },
        EvictionConfig("LRU"),
    )
    assert config.gds_l1_config is not None
    assert config.gds_l1_config.backend == "vendor_gds"
    assert module not in sys.modules
    assert create_backend(config.gds_l1_config.backend).name == "vendor_gds"
