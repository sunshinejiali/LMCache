Client Wheel Tooling Prototype
========================================

The preparatory tooling for packaging task #6315 is available as
``tools/client_wheel.py``. It copies an exact manifest into a new staging root,
builds there through an isolated PEP 517 frontend, and audits payload files,
METADATA, entry points, package data, RECORD hashes and ELF dependencies before
copying an artifact to the output directory.

This infrastructure requires a reviewed canonical source manifest and static
client project. It does not provide a release-ready client build from the
current legacy tree. Missing sources and broad discovery fail rather than
copying server implementation. Root packaging files remain unchanged.

See ``packaging/client/README.md`` for commands, the manifest schema and remaining
acceptance criteria. Python 3.12, ``build`` and ``packaging`` are required; native
auditing also needs ``readelf``. CPU tests run with:

.. code-block:: bash

    python -m pytest --noconftest -q tests/tools/test_client_wheel.py

Fixture tests and fixture-wheel installation validate the infrastructure. They
do not establish actual client/server compatibility, replacement semantics,
full-wheel completeness or cache reuse. Those checks require the completed
source/dependency boundary and real distribution artifacts.
