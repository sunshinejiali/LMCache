# Client wheel staging and audit infrastructure (WIP for #6315)

This preparatory slice implements exact staging, an isolated PEP 517 build and
artifact audits. It requires a reviewed manifest and static client project; it
does not infer either from the legacy source tree or provide a release-ready
client distribution.

The contract is `docs/design/v1/package_boundaries.md` at PR #6244 revision
`92a1ffc8`. Upstream `dev` inspected at `ee84f015` does not yet contain the
canonical `v1/client`, `v1/common` and `v1/transport` implementation boundary.
The ownership/dependency migrations must land before a production allowlist,
dependency set, platform closure and engine extras can be checked in.

## Commands

Use Python 3.12 with `build` and `packaging` installed. Native ELF inspection
also requires `readelf`. From the repository root:

```bash
python tools/client_wheel.py --manifest /path/to/reviewed-manifest.json stage \
  --source-root . --destination /tmp/lmcache-client-stage

python tools/client_wheel.py --manifest /path/to/reviewed-manifest.json build \
  --source-root . --version 0.5.7 --out-dir /tmp/client-dist

python tools/client_wheel.py --manifest /path/to/reviewed-manifest.json audit \
  --wheel /tmp/client-dist/lmcache_client-0.5.7-py3-none-any.whl --version 0.5.7
```

The illustrated manifest and static project are prerequisites, not files
currently provided for the actual LMCache client. The filename is a pure-Python
example; real native builds require matching platform tags and inventories.

Staging copies exact files into a directory that must not exist. The builder
uses `python -m build` with isolation and
`SETUPTOOLS_SCM_PRETEND_VERSION_FOR_LMCACHE_CLIENT`. Static packaging files are
copied unchanged. Root packaging and requirements are never patched, renamed or
restored to switch distribution identities. Only an audited wheel is copied to
the output directory, and existing artifacts cannot be overwritten. Generators
and native builds must operate inside staging through the dedicated static
project, without reusing the root full-package setup.

## Exact manifest schema

The JSON object has exactly these keys:

| Field | Value |
| --- | --- |
| `schema_version` | `1` |
| `files` | Mapping of exact checkout-relative files to exact staging-relative targets |
| `wheel_files` | Exact payload files, including generated bindings, native libraries and package data |
| `metadata_files` | Exact paths relative to `.dist-info`; includes `METADATA`, `WHEEL`, `RECORD` |
| `requires_dist` | Expected direct requirements, including optional-extra markers |
| `extras` | Expected `Provides-Extra` names |
| `entry_points` | Mapping of group to exact entry-point name/value pairs |
| `native_needed` | Mapping of every native file to its expected ELF `DT_NEEDED` libraries |

For example, a final `files` mapping will contain entries like these, alongside
the entire approved source closure:

```json
{
  "packaging/client/project/pyproject.toml": "pyproject.toml",
  "lmcache/sdk/__init__.py": "lmcache/sdk/__init__.py"
}
```

Those two entries alone are not a valid manifest. Required canonical package
markers cover SDK, integrations, client/common/transport and platform, but
markers alone cannot establish import correctness. The static setuptools
configuration must list packages explicitly rather than discovering `lmcache*`.
Globs, path traversal, symlinked/missing sources and destination collisions fail.
Missing canonical sources never trigger copying a whole legacy mixed directory.

## Artifact checks

The audit rejects missing/unexpected payload or metadata files, canonical and
legacy server/frontend/coordinator/orchestrator/storage/server-CLI paths, common
server-only direct dependencies, unexpected extras/entry points, wrong versions
or tags, bad RECORD hashes, unlisted native artifacts and unexpected ELF
libraries. Native files are also detected by their ELF header. Non-ELF native
formats require a separate platform audit implementation.

Direct METADATA checks do not prove the absence of transitive dependencies or
server imports. The boundary subtasks must establish the actual supported
SDK/integration/platform closure. This tool is not an import sandbox or a
replacement for clean-environment runtime validation.

## Validation and remaining work

```bash
python -m pytest --noconftest -q tests/tools/test_client_wheel.py
```

Tests use synthetic post-migration sources and fixture wheels. They compile a
small ELF library where `gcc`/`readelf` are available. A fixture wheel can be
built, audited and installed/imported in a clean environment without changing
its source project. This validates infrastructure, not actual LMCache runtime.

Before #6315 can be completed:

1. Land source ownership/dependency boundaries, then check in the final static
   project, exact manifest, platform/native build support and engine extras.
2. Generate and audit the actual required protobuf/native outputs in staging.
3. Validate real SDK/integrations and alternative-distribution replacement and
   uninstall behavior in clean environments; do not co-install both products.
4. Build/install the unchanged full wheel separately and verify its complete
   client and all server surfaces.
5. Execute real client/server requests across separate environments and show
   non-zero cache reuse on the second request.

This infrastructure alone does not close #6315 or establish wheel-size,
server-compatibility or cache-reuse claims.
