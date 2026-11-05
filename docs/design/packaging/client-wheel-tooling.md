# Exact client wheel staging and artifact auditing

This preparatory slice for #6315 follows the package-boundary contract from
PR #6244. The canonical source and dependency boundary remains a prerequisite;
this change does not extract a client artifact from the current legacy tree.

`tools/client_wheel.py` consumes a reviewed exact manifest. It validates source
and target paths and checks that the static client project names packages
explicitly. It copies only listed files into a new staging root. Builds run
there through the isolated PEP 517 frontend with the exact version passed via
`SETUPTOOLS_SCM_PRETEND_VERSION_FOR_LMCACHE_CLIENT`. Root packaging and sources
are not patched to switch between distributions.

The audit compares payload, metadata, extras, entry points and native libraries
with exact inventories. It rejects canonical/legacy server paths and verifies
versions, wheel tags and RECORD hashes. ELF auditing checks `DT_NEEDED`; other
native formats require a separate implementation. The audit is an artifact gate,
not proof of import-closure or client/server compatibility.

Only an audited artifact is copied out of the temporary build. Exclusive file
creation preserves existing artifacts. Generators and native builds are required
to operate in staging through the dedicated static client project.

Tests use synthetic canonical sources and fixture wheels, including a small
compiled ELF library. They cover source isolation, broad discovery, leakage,
metadata, checksums and native dependencies. These fixtures do not substitute
for actual SDK/integration and cache-reuse acceptance tests.

See [the tooling guide](../../../packaging/client/README.md) for the schema,
commands and remaining acceptance work. Root/full-wheel build behavior is
unchanged.
