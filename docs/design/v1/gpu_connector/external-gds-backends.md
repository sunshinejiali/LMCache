# External GDS backends

External distributions can implement the existing `GDSBackend`, `GDSHandle`,
and `Submission` contracts without adding files to LMCache's backend package.
This extends discovery and selection; it does not alter GDSContext, IO ordering,
the slab allocator, KV layouts, or CUDA transfer kernels.

## Registration and selection

```toml
[project.entry-points."lmcache.gds_plugins"]
vendor_gds = "vendor_gds.backend:Backend"
```

The value resolves to a concrete `GDSBackend` subclass with a no-argument
constructor. Its `name` equals the entry-point name. Names match
`[a-z][a-z0-9_]*`; `auto` and names of bundled backend modules are reserved.

`available_backends()` combines bundled module names with external distribution
metadata, without importing either implementation. This also populates the
existing `--gds-l1-backend` choices. Explicit selection loads only the selected
implementation and constructs a fresh backend. `auto` scans bundled modules
only, preserving its existing behavior and ordering.

```text
available_backends -> bundled module names + external metadata -> CLI choices
create_backend(name)
    auto         -> bundled default selection
    bundled name -> bundled module.Backend
    external     -> one entry point -> concrete class -> constructor -> validation
```

## Conflicts and errors

- Invalid external names are warned about and ignored.
- Reserved-name registrations are warned about and ignored; plugins cannot
  replace built-in backends, including when their native runtime is unavailable.
- Duplicate external registrations produce one discoverable name. Selecting it
  fails with a deterministic diagnostic listing distributions and entry-point
  values. Neither implementation is imported to resolve a conflict.
- A selected plugin's import, interface, construction or environment failure
  propagates to the caller; no fallback backend is used. Expected load and
  validation errors include the plugin registration and retain their cause.
- Broken unselected plugin modules do not affect listing, CLI help, or built-in
  selection, because metadata discovery does not execute them.

The discovery function does not cache native backend objects. Installing or
removing distributions in running worker environments is not a supported hot
reload mechanism; restart affected processes after package changes.

## Runtime and compatibility contract

Import and construction must be driver-free. Environment checks belong in
`validate_environment`; native resource acquisition belongs to slab opening and
registration. A discoverable name is not evidence of hardware readiness.

Async IO must use the supplied stream and retain pointer-backed submission
arguments until completion. Returned byte counts must represent actual IO, and
teardown must complete IO before freeing registrations. External packages can
override `close_driver()` without importing private shared-driver helpers.

Plugins declare and test their supported LMCache version range in distribution
dependencies. This change adds neither an interface-version handshake nor a
native ABI. Installed Python plugins execute trusted code in the LMCache
process; entry points do not provide isolation.

## Validation

CPU tests use real distribution metadata and importable external modules to
verify lazy discovery, CLI selection, duplicate/reserved names, interface
validation, failure diagnostics and fresh instances. Existing built-in discovery
and GDS tests cover backward compatibility. The independently packaged
`examples/gds_plugin` demonstrates wheel discovery and construction without
native libraries; it explicitly rejects IO.

A real driver plugin still needs accelerator round-trip tests to establish DMA
correctness, stream ordering and performance. Implementing a vendor transport,
GDS L2, auto-selection of external plugins and moving built-ins out of tree are
separate changes.
