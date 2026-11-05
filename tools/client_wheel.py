# SPDX-License-Identifier: Apache-2.0
"""Stage exact client sources, build in isolation, and audit the resulting wheel."""

# Standard
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import cast
import argparse
import base64
import configparser
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile

# Third Party
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

# Both canonical server packages and their legacy implementations are excluded.
SERVER_ROOTS = (
    "lmcache/v1/server",
    "lmcache/lmcache_frontend",
    "lmcache/v1/mp_coordinator",
    "lmcache/v1/memory_orchestrator",
    "lmcache/v1/distributed",
    "lmcache/v1/multiprocess",
    "lmcache/v1/storage_backend",
    "lmcache/storage_backend",
    "lmcache/inprocess",
    "lmcache/cli",
)
SERVER_DEPENDENCIES = frozenset(
    {
        "fastapi",
        "starlette",
        "uvicorn",
        "httptools",
        "redis",
        "google-cloud-bigtable",
    }
)
REQUIRED_PACKAGE_MARKERS = frozenset(
    f"{package}/__init__.py"
    for package in (
        "lmcache",
        "lmcache/sdk",
        "lmcache/integration",
        "lmcache/v1",
        "lmcache/v1/client",
        "lmcache/v1/common",
        "lmcache/v1/transport",
        "lmcache/v1/platform",
    )
)


def _relative(value: str) -> str:
    """Require canonical relative POSIX file names, without glob syntax."""
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or value != path.as_posix()
        or value == "."
        or any(c in value for c in "\\\x00*?[]")
    ):
        raise ValueError(f"Expected an exact relative file path: {value!r}")
    return value


def _reject_server(path: str) -> None:
    """Reject known canonical and legacy server implementations."""
    if any(path == prefix or path.startswith(prefix + "/") for prefix in SERVER_ROOTS):
        raise ValueError(f"Server source is not permitted in a client artifact: {path}")


def _strings(value: object) -> tuple[str, ...]:
    """Validate a unique JSON string array."""
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError("Expected an array of strings")
    result = tuple(cast(list[str], value))
    if len(set(result)) != len(result):
        raise ValueError("Duplicate manifest entries")
    return result


def _mapping(value: object) -> dict[str, str]:
    """Validate a JSON string-to-string mapping."""
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise ValueError("Expected a string-to-string mapping")
    return cast(dict[str, str], value)


def _requirements(values: tuple[str, ...]) -> set[str]:
    """Normalize direct requirements and reject server-only dependencies."""
    result = set()
    for value in values:
        requirement = Requirement(value)
        if canonicalize_name(requirement.name) in SERVER_DEPENDENCIES:
            raise ValueError(f"Server dependency in client metadata: {value}")
        requirement.name = canonicalize_name(requirement.name)
        result.add(str(requirement))
    return result


@dataclass(frozen=True)
class Manifest:
    """Reviewed exact source, artifact, metadata and native dependency inventory.

    ``files`` maps repository paths to staging paths. All other lists describe
    expected built-wheel contents, including generated files; no glob expansion
    or recursive source copying is performed.
    """

    files: dict[str, str]
    wheel_files: tuple[str, ...]
    metadata_files: tuple[str, ...]
    requires_dist: tuple[str, ...]
    extras: tuple[str, ...]
    entry_points: dict[str, dict[str, str]]
    native_needed: dict[str, tuple[str, ...]]


def load_manifest(path: Path) -> Manifest:
    """Read and validate an exact inventory; raise ValueError for invalid policy.

    Args:
        path: JSON manifest with schema_version=1 and all Manifest fields.

    Returns:
        A validated manifest suitable for staging and artifact auditing.
    """
    raw = json.loads(path.read_text())
    fields = {
        "schema_version",
        "files",
        "wheel_files",
        "metadata_files",
        "requires_dist",
        "extras",
        "entry_points",
        "native_needed",
    }
    if not isinstance(raw, dict) or set(raw) != fields or raw["schema_version"] != 1:
        raise ValueError("Manifest must use schema_version=1 and the documented fields")
    files = _mapping(raw["files"])
    if not files or "pyproject.toml" not in files.values():
        raise ValueError("Manifest must explicitly stage a static pyproject.toml")
    if len(set(files.values())) != len(files):
        raise ValueError("Multiple sources map to one staging path")
    wheel_files = _strings(raw["wheel_files"])
    if not REQUIRED_PACKAGE_MARKERS.issubset(wheel_files):
        raise ValueError("Wheel inventory omits required canonical client packages")
    metadata_files = _strings(raw["metadata_files"])
    for source, target in files.items():
        _reject_server(_relative(source))
        _reject_server(_relative(target))
    for target in wheel_files:
        _reject_server(_relative(target))
        if not target.startswith("lmcache/"):
            raise ValueError(f"Wheel payload must use the lmcache namespace: {target}")
    for target in metadata_files:
        _relative(target)
    if not {"METADATA", "WHEEL", "RECORD"}.issubset(metadata_files):
        raise ValueError("metadata_files must include METADATA, WHEEL and RECORD")
    entries = raw["entry_points"]
    native = raw["native_needed"]
    if not isinstance(entries, dict) or not isinstance(native, dict):
        raise ValueError("entry_points and native_needed must be mappings")
    entry_points = {str(group): _mapping(values) for group, values in entries.items()}
    for group in entry_points.values():
        for target in group.values():
            _reject_server(_relative(target.split(":", 1)[0].replace(".", "/")))
    requires_dist = _strings(raw["requires_dist"])
    _requirements(requires_dist)
    native_needed = {str(name): _strings(values) for name, values in native.items()}
    if not set(native_needed).issubset(wheel_files):
        raise ValueError("native_needed contains a file outside wheel_files")
    return Manifest(
        files,
        wheel_files,
        metadata_files,
        requires_dist,
        _strings(raw["extras"]),
        entry_points,
        native_needed,
    )


def stage_sources(source_root: Path, destination: Path, manifest: Manifest) -> Path:
    """Copy only exact sources and static packaging files to a fresh directory.

    Args:
        source_root: Canonical repository checkout, never edited by this function.
        destination: New staging directory; must not already exist.
        manifest: Inventory loaded by load_manifest.

    Returns:
        The populated staging root.

    Raises:
        ValueError: A source is absent, symlinked, or the static project uses broad
            package discovery or is not named lmcache-client.
        FileExistsError: The destination already exists.
    """
    root = source_root.resolve()
    sources = []
    for source, target in manifest.files.items():
        path = root / source
        # Reject intermediate symlinks as well as symlinked leaf files.
        if not path.is_file() or path.resolve() != path.absolute():
            raise ValueError(f"Canonical source missing or symlinked: {source}")
        sources.append((path, target))
    project_source = next(p for p, target in sources if target == "pyproject.toml")
    project = tomllib.loads(project_source.read_text())
    if project.get("project", {}).get("name") != "lmcache-client":
        raise ValueError("Static project name must be lmcache-client")
    packages = project.get("tool", {}).get("setuptools", {}).get("packages")
    if (
        not isinstance(packages, list)
        or not packages
        or not all(isinstance(p, str) and "*" not in p for p in packages)
    ):
        raise ValueError("Static project must use an explicit setuptools packages list")
    destination.mkdir(parents=True, exist_ok=False)
    for path, target in sources:
        output = destination / target
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, output)
    return destination


def audit_wheel(wheel: Path, manifest: Manifest, version: str) -> None:
    """Fail on unexpected payload, metadata, entry points or ELF dependencies.

    Args:
        wheel: Built client wheel to inspect without importing its code.
        manifest: Exact expected artifact inventory.
        version: Exact expected PEP 440 version string.

    Raises:
        ValueError: Files, metadata, RECORD integrity or native dependencies differ.
        subprocess.CalledProcessError: readelf cannot inspect a declared native file.
    """
    Version(version)
    distribution, parsed_version, _, tags = parse_wheel_filename(wheel.name)
    if distribution != "lmcache-client" or str(parsed_version) != version:
        raise ValueError("Wheel filename does not match client name and exact version")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if len(set(names)) != len(names):
            raise ValueError("Duplicate wheel members")
        for name in names:
            _reject_server(_relative(name))
        infos = {name.split("/", 1)[0] for name in names if ".dist-info/" in name}
        if len(infos) != 1:
            raise ValueError("Expected exactly one dist-info directory")
        info = infos.pop()
        if info != f"lmcache_client-{version}.dist-info":
            raise ValueError(
                "dist-info directory does not match client name and version"
            )
        expected = set(manifest.wheel_files) | {
            f"{info}/{name}" for name in manifest.metadata_files
        }
        if set(names) != expected:
            raise ValueError(
                f"Wheel file inventory differs: {sorted(set(names) ^ expected)}"
            )
        metadata = BytesParser().parsebytes(archive.read(f"{info}/METADATA"))
        if canonicalize_name(metadata["Name"] or "") != "lmcache-client":
            raise ValueError("Wheel distribution name must be lmcache-client")
        if metadata["Version"] != version:
            raise ValueError("Wheel version differs from requested exact version")
        wheel_metadata = BytesParser().parsebytes(archive.read(f"{info}/WHEEL"))
        if (
            wheel_metadata["Wheel-Version"] != "1.0"
            or set(wheel_metadata.get_all("Tag", [])) != {str(tag) for tag in tags}
            or wheel_metadata["Root-Is-Purelib"] not in {"true", "false"}
        ):
            raise ValueError("WHEEL version or tags differ from the artifact filename")
        if _requirements(tuple(metadata.get_all("Requires-Dist", []))) != _requirements(
            manifest.requires_dist
        ):
            raise ValueError("Requires-Dist differs from the reviewed inventory")
        if set(metadata.get_all("Provides-Extra", [])) != set(manifest.extras):
            raise ValueError("Provides-Extra differs from the reviewed inventory")
        parser = _EntryPointParser(interpolation=None)
        if f"{info}/entry_points.txt" in names:
            parser.read_string(archive.read(f"{info}/entry_points.txt").decode())
        actual_entries = {group: dict(parser[group]) for group in parser.sections()}
        if actual_entries != manifest.entry_points:
            raise ValueError("Entry points differ from the reviewed inventory")
        records = list(csv.reader(io.StringIO(archive.read(f"{info}/RECORD").decode())))
        if (
            any(len(row) != 3 for row in records)
            or len(records) != len(names)
            or {row[0] for row in records} != set(names)
        ):
            raise ValueError("RECORD inventory differs from wheel members")
        for name, digest, size in records:
            if name == f"{info}/RECORD":
                continue
            algorithm, separator, encoded = digest.partition("=")
            if separator != "=" or algorithm not in {"sha256", "sha384", "sha512"}:
                raise ValueError(f"Missing or unsupported RECORD hash: {name}")
            data = archive.read(name)
            actual = base64.urlsafe_b64encode(hashlib.new(algorithm, data).digest())
            if actual.rstrip(b"=").decode() != encoded or str(len(data)) != size:
                raise ValueError(f"RECORD integrity failure: {name}")
        native_names = {
            name
            for name in manifest.wheel_files
            if re.search(r"\.so(?:\.[0-9]+)*$", name)
            or name.endswith((".pyd", ".dylib"))
            or archive.read(name).startswith(b"\x7fELF")
        }
        if native_names != set(manifest.native_needed):
            raise ValueError(
                "Every native artifact needs an explicit dependency inventory"
            )
        if native_names and wheel_metadata["Root-Is-Purelib"] != "false":
            raise ValueError("Native artifacts cannot be marked as a pure Python wheel")
        if native_names and any(tag.platform == "any" for tag in tags):
            raise ValueError("Native artifacts require platform-specific wheel tags")
        for name, needed in manifest.native_needed.items():
            data = archive.read(name)
            if not data.startswith(b"\x7fELF"):
                raise ValueError(f"Native audit currently supports ELF only: {name}")
            with tempfile.TemporaryDirectory(prefix="lmcache-native-audit-") as tmp:
                native_file = Path(tmp) / PurePosixPath(name).name
                native_file.write_bytes(data)
                output = subprocess.check_output(
                    ["readelf", "-d", str(native_file)], text=True
                )
            actual_needed = set(re.findall(r"\(NEEDED\).*\[([^]]+)\]", output))
            if actual_needed != set(needed):
                raise ValueError(f"Native dependencies differ: {name}: {actual_needed}")


def build_client_wheel(
    source_root: Path,
    manifest: Manifest,
    version: str,
    output: Path,
) -> Path:
    """Build from a temporary staging root and publish only an audited wheel.

    Args:
        source_root: Checkout containing canonical sources and static build files.
        manifest: Reviewed source/artifact inventory.
        version: Exact version passed to setuptools-scm's client-specific variable.
        output: Directory for the audited artifact; existing artifacts are preserved.

    Returns:
        Path to the copied, audited wheel.

    Raises:
        ValueError: Staging or artifact auditing fails.
        FileExistsError: The final artifact already exists.
        subprocess.CalledProcessError: The isolated PEP 517 wheel build fails.
    """
    Version(version)
    with tempfile.TemporaryDirectory(prefix="lmcache-client-build-") as tmp:
        root = Path(tmp)
        stage = stage_sources(source_root, root / "project", manifest)
        environment = os.environ.copy()
        environment["SETUPTOOLS_SCM_PRETEND_VERSION_FOR_LMCACHE_CLIENT"] = version
        subprocess.run(
            [sys.executable, "-m", "build", "--wheel", "--outdir", str(root / "dist")],
            cwd=stage,
            env=environment,
            check=True,
        )
        wheels = list((root / "dist").glob("*.whl"))
        if len(wheels) != 1:
            raise ValueError("Build must produce exactly one wheel")
        audit_wheel(wheels[0], manifest, version)
        output.mkdir(parents=True, exist_ok=True)
        destination = output / wheels[0].name
        # Exclusive creation prevents an audit run from replacing a release artifact.
        with destination.open("xb") as stream:
            stream.write(wheels[0].read_bytes())
        return destination


def main() -> None:
    """Run exact staging, isolated building or artifact auditing from a manifest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    stage = commands.add_parser("stage")
    stage.add_argument("--source-root", type=Path, required=True)
    stage.add_argument("--destination", type=Path, required=True)
    audit = commands.add_parser("audit")
    audit.add_argument("--wheel", type=Path, required=True)
    audit.add_argument("--version", required=True)
    build = commands.add_parser("build")
    build.add_argument("--source-root", type=Path, required=True)
    build.add_argument("--version", required=True)
    build.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    manifest = load_manifest(args.manifest)
    if args.command == "stage":
        print(stage_sources(args.source_root, args.destination, manifest))
    elif args.command == "audit":
        audit_wheel(args.wheel, manifest, args.version)
        print("Client wheel inventory and metadata audit passed")
    else:
        print(
            build_client_wheel(args.source_root, manifest, args.version, args.out_dir)
        )


class _EntryPointParser(configparser.ConfigParser):
    """Preserve the case of entry-point names while parsing wheel metadata."""

    def optionxform(self, optionstr: str) -> str:
        return optionstr


if __name__ == "__main__":
    main()
