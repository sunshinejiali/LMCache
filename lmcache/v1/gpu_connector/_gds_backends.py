# SPDX-License-Identifier: Apache-2.0
"""Discover built-in and external async GDS implementations on demand.

Add a public module exporting a Backend subclass to the gds_backends package;
external distributions export a subclass through ``lmcache.gds_plugins``.
Listing names reads metadata only. External backends require explicit selection.
"""

# Standard
import importlib
import importlib.metadata
import inspect
import pkgutil
import re

# First Party
from lmcache.logging import init_logger
from lmcache.v1.gpu_connector import gds_backends
from lmcache.v1.gpu_connector.gds_backends.base import GDSBackend

logger = init_logger(__name__)
GDS_PLUGIN_ENTRY_POINT_GROUP = "lmcache.gds_plugins"


def _builtin_backend_names() -> tuple[str, ...]:
    """List bundled backend modules without executing their code."""
    return tuple(
        sorted(
            module.name
            for module in pkgutil.iter_modules(gds_backends.__path__)
            if not module.name.startswith("_") and module.name != "base"
        )
    )


def _describe_entry_point(entry: importlib.metadata.EntryPoint) -> str:
    """Identify a registration using distribution metadata only."""
    distribution = entry.dist.name if entry.dist is not None else "unknown distribution"
    return f"{distribution}: {entry.value}"


def _external_backend_entries(
    builtin_names: tuple[str, ...],
) -> dict[str, list[importlib.metadata.EntryPoint]]:
    """Group eligible external registrations without importing plugin modules."""
    entries: dict[str, list[importlib.metadata.EntryPoint]] = {}
    for entry in importlib.metadata.entry_points(group=GDS_PLUGIN_ENTRY_POINT_GROUP):
        if re.fullmatch(r"[a-z][a-z0-9_]*", entry.name) is None:
            logger.warning("Ignoring GDS plugin with invalid name %r", entry.name)
            continue
        if entry.name == "auto" or entry.name in builtin_names:
            logger.warning(
                "Ignoring GDS plugin %r (%s): name is reserved",
                entry.name,
                _describe_entry_point(entry),
            )
            continue
        entries.setdefault(entry.name, []).append(entry)
    return entries


def _create_external_backend(
    name: str, entry: importlib.metadata.EntryPoint
) -> GDSBackend:
    """Load and validate one explicitly selected external backend."""
    description = _describe_entry_point(entry)
    try:
        backend_class = entry.load()
    except (ImportError, AttributeError, OSError) as error:
        raise RuntimeError(
            f"Cannot load GDS plugin {name!r} ({description}): {error}"
        ) from error
    if (
        not isinstance(backend_class, type)
        or not issubclass(backend_class, GDSBackend)
        or inspect.isabstract(backend_class)
    ):
        raise TypeError(
            f"GDS plugin {name!r} ({description}) must export a concrete "
            "GDSBackend subclass"
        )
    try:
        backend = backend_class()
    except TypeError as error:
        raise TypeError(
            f"Cannot construct GDS plugin {name!r} ({description}); "
            f"a no-argument constructor is required: {error}"
        ) from error
    except (ValueError, RuntimeError, ImportError, OSError) as error:
        raise RuntimeError(
            f"Cannot construct GDS plugin {name!r} ({description}): {error}"
        ) from error
    if getattr(backend, "name", None) != name:
        raise ValueError(
            f"GDS plugin {name!r} ({description}): Backend.name must match "
            "the entry-point name"
        )
    try:
        backend.validate_environment()
    except (ValueError, RuntimeError, ImportError, OSError) as error:
        raise RuntimeError(
            f"GDS plugin {name!r} ({description}) environment validation failed: "
            f"{error}"
        ) from error
    return backend


def available_backends() -> tuple[str, ...]:
    """List built-in and external names without importing their implementations.

    Duplicate external names appear once; ambiguity is reported on explicit
    selection. Names alone do not establish native-library or hardware readiness.

    Returns:
        Sorted names from bundled modules and installed plugin metadata.
    """
    builtin_names = _builtin_backend_names()
    entries = _external_backend_entries(builtin_names)
    return tuple(sorted(set(builtin_names) | entries.keys()))


def create_backend(name: str) -> GDSBackend:
    """Construct and validate a backend without loading its native driver.

    Explicit selection imports only the requested implementation. ``auto``
    imports built-in candidates in name order until one accepts default
    selection; external plugins never participate. Explicit external selection
    loads only that plugin and never falls back. No backend instance is cached.

    Args:
        name: Built-in module name, external entry-point name, or ``auto``.

    Returns:
        A newly constructed backend whose environment validation succeeded.

    Raises:
        ValueError: The name is unknown, ambiguous, mismatched, or a built-in
            backend rejects the environment.
        TypeError: The plugin is not a concrete subclass or cannot be constructed.
        RuntimeError: A selected plugin cannot load or validate its environment.
        Exception: Other exceptions raised by the selected implementation.
    """
    names = _builtin_backend_names()
    if name == "auto":
        for candidate in names:
            backend_class = _load_backend_class(candidate)
            if backend_class.is_default():
                break
        else:
            raise ValueError("no default GDS backend for this environment")
    elif name in names:
        backend_class = _load_backend_class(name)
    else:
        entries = _external_backend_entries(names)
        if name not in entries:
            choices = ", ".join(("auto", *sorted(set(names) | entries.keys())))
            raise ValueError(
                f"unsupported GDS L1 backend: {name!r}. Choose from: {choices}"
            )
        candidates = entries[name]
        if len(candidates) != 1:
            descriptions = "; ".join(
                sorted(_describe_entry_point(entry) for entry in candidates)
            )
            raise ValueError(
                f"Ambiguous GDS plugin {name!r}: {descriptions}. "
                "Remove or rename the conflicting registrations."
            )
        return _create_external_backend(name, candidates[0])
    backend = backend_class()
    backend.validate_environment()
    return backend


def _load_backend_class(name: str) -> type[GDSBackend]:
    module = importlib.import_module(f"{gds_backends.__name__}.{name}")
    try:
        backend_class = module.Backend
    except AttributeError as error:
        raise TypeError(f"GDS backend {name!r} must export a Backend class") from error
    if not isinstance(backend_class, type) or not issubclass(backend_class, GDSBackend):
        raise TypeError(f"GDS backend {name!r} must export a GDSBackend subclass")
    return backend_class
