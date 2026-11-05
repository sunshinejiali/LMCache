.. SPDX-License-Identifier: Apache-2.0

External GDS Backends
========================================

GDS backends can be distributed as independent Python wheels. Install the
package in each environment that discovers or creates the backend, then select
it using the existing ``--gds-l1-backend`` option. No source file needs to be
added to the LMCache package.

Register a backend
------------------

Publish a concrete subclass of
``lmcache.v1.gpu_connector.gds_backends.base.GDSBackend`` through your package's
``pyproject.toml``:

.. code-block:: toml

    [project.entry-points."lmcache.gds_plugins"]
    vendor_gds = "vendor_gds.backend:Backend"

The class must have a no-argument constructor and ``name = "vendor_gds"``.
Entry-point names must match ``[a-z][a-z0-9_]*``. Choose a name that does not
conflict with ``auto`` or an existing built-in backend.

Implement the existing interfaces:

* ``GDSBackend`` opens the slab, registers/deregisters GPU buffers and streams,
  validates the runtime, and releases driver resources.
* ``GDSHandle`` owns a slab and submits stream-ordered reads and writes.
* ``Submission`` retains native IO arguments until the issuing stream completes.
  Its completion count must report actual transferred bytes or a deferred error.

Module import and construction must not load native drivers or require working
GPU hardware. Put runtime checks in ``validate_environment()`` and acquire
native resources in slab opening or registration. Keep registered buffers and
submission arguments alive through completion, and drain outstanding IO before
releasing them. Plugins can implement ``close_driver()`` directly; importing
LMCache's private driver helpers is not part of the external interface.

Install and select
------------------

Install your vendor wheel into the same Python environment as LMCache, then
restart affected processes. For example:

.. code-block:: bash

    uv pip install /path/to/vendor_gds.whl
    lmcache server \
        --gds-l1-backend vendor_gds \
        --gds-l1-path /path/to/dedicated-scratch \
        --l1-size-gb 16

The backend interprets ``--gds-l1-path``; it may require a directory or a
dedicated device. Follow the vendor's storage and driver requirements, including
any destructive initialization behavior. Installing a plugin does not install
or configure its native driver automatically.

``auto`` continues to select built-in backends only. External implementations
always require explicit selection, even if they override ``is_default()``.
Listing names and building CLI help read distribution metadata without
importing plugin modules. A listed name does not establish hardware readiness.

Conflicts and errors
--------------------

Invalid external names and attempts to override built-in names are logged and
ignored. If multiple registrations claim the same external name, listing names
still works, but selecting that name raises an ambiguity error identifying the
registrations. Remove or rename the conflicting packages before retrying.

An explicitly selected plugin must load, satisfy the class contract and pass
environment validation. Failures are reported rather than falling back to a
different backend. An unselected plugin with missing native dependencies does
not prevent other backends from being used.

Plugin wheels should declare and test an appropriate LMCache dependency range.
There is no interface-version negotiation in this mechanism. Entry points run
installed Python code in the LMCache process; deploy trusted packages.

CPU discovery example
---------------------

The checkout contains an independently packaged example at
``examples/gds_plugin``. In an activated LMCache development environment with
the build prerequisites installed, run from the repository root:

.. code-block:: bash

    uv pip install --no-deps --no-build-isolation ./examples/gds_plugin
    python -c "from lmcache.v1.gpu_connector._gds_backends import available_backends, create_backend; print(available_backends()); print(create_backend('example_gds').name)"

This verifies installation, discovery and construction on a CPU machine.
The example deliberately raises ``NotImplementedError`` on IO: it is not a
storage backend and does not validate DMA correctness or performance.
Uninstall the example after experimenting if it should no longer appear in
your backend choices.
