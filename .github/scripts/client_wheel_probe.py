# SPDX-License-Identifier: Apache-2.0
"""Inspect an installed distribution and optionally make a real public RPC.

The E2E runner copies this script outside the checkout and runs it with -I.
Every loaded lmcache module must belong to the selected distribution's RECORD.
"""

# Standard
from importlib import metadata
from pathlib import Path
import argparse
import json
import sys

# First Party
from lmcache.v1.client.transport.factory import RequestClientFactory
import lmcache


def verify_installation(distribution_name: str, version: str) -> None:
    """Reject mixed distributions, wrong versions, and imports outside RECORD.

    Args:
        distribution_name: Either lmcache or lmcache-client.
        version: Expected distribution version from artifact provenance.

    Raises:
        RuntimeError: The installed environment or an imported module is invalid.
    """
    other = "lmcache" if distribution_name == "lmcache-client" else "lmcache-client"
    try:
        metadata.distribution(other)
    except metadata.PackageNotFoundError:
        pass
    else:
        raise RuntimeError(f"Unexpected distribution installed: {other}")
    distribution = metadata.distribution(distribution_name)
    if distribution.version != version:
        raise RuntimeError(
            f"Installed version mismatch: {distribution.version} != {version}"
        )
    files = distribution.files
    if not files:
        raise RuntimeError(f"Missing installed file manifest for {distribution_name}")
    owned = {Path(str(distribution.locate_file(entry))).resolve() for entry in files}
    prefix = Path(sys.prefix).resolve()
    imports: dict[str, str] = {}
    for name, module in tuple(sys.modules.items()):
        if name != "lmcache" and not name.startswith("lmcache."):
            continue
        filename = getattr(module, "__file__", None)
        if filename is None:
            continue
        path = Path(filename).resolve()
        if path not in owned or not path.is_relative_to(prefix):
            raise RuntimeError(f"Import outside installed wheel: {name} -> {path}")
        imports[name] = str(path)
    print(
        json.dumps(
            {
                "distribution": distribution_name,
                "version": distribution.version,
                "python": sys.executable,
                "lmcache": lmcache.__file__,
                "imports": imports,
            },
            sort_keys=True,
        ),
        flush=True,
    )


def main() -> None:
    """Check installation identity, then ping and query the configured chunk size."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--distribution", choices=("lmcache", "lmcache-client"), required=True
    )
    parser.add_argument("--version", required=True)
    parser.add_argument("--url")
    parser.add_argument("--timeout", type=float, default=5)
    args = parser.parse_args()
    verify_installation(args.distribution, args.version)
    if args.url:
        client = RequestClientFactory.create(args.url)
        try:
            if client.ping(None).result(timeout=args.timeout) is not True:
                raise RuntimeError("Server ping did not return True")
            chunk_size = client.get_chunk_size().result(timeout=args.timeout)
            if chunk_size != 128:
                raise RuntimeError(f"Unexpected server chunk size: {chunk_size}")
            print(json.dumps({"ping": True, "chunk_size": chunk_size}), flush=True)
        finally:
            client.close()
        # RPC construction lazily imports the selected endpoint and codecs.
        verify_installation(args.distribution, args.version)


if __name__ == "__main__":
    main()
