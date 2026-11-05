# External GDS plugin example

This standalone Python package registers `example_gds` in
`lmcache.gds_plugins`. It demonstrates wheel packaging, lazy discovery and
CPU-side construction. It intentionally rejects all IO/registration operations;
it does not emulate GPU DMA or provide a usable storage backend.

From the repository root in an activated LMCache development environment:

```bash
uv pip install --no-deps --no-build-isolation ./examples/gds_plugin
python -c "from lmcache.v1.gpu_connector._gds_backends import available_backends, create_backend; print(available_backends()); print(create_backend('example_gds').name)"
```

To build a wheel instead, use `uv build --wheel ./examples/gds_plugin`.
The build needs setuptools and wheel, as declared in `pyproject.toml`.
The example depends on the matching LMCache checkout; real plugin packages
should constrain their dependency to the LMCache versions they test.

Replace the methods with your driver's `GDSBackend` implementation and choose
a unique entry-point/class name before publishing a real plugin. Preserve
stream ordering and `Submission` lifetimes, report actual completed IO counts,
and release registrations only after completion. Do not require native driver
availability during import or construction.

The developer guide at
`docs/source/developer_guide/extending_lmcache/gds_backends.rst` documents the
full registration and selection contract. Restart affected processes after
installing or removing plugin distributions.
