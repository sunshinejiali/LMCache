# SPDX-License-Identifier: Apache-2.0
"""Tests for packaging infrastructure, using synthetic canonical client sources."""

# Standard
from pathlib import Path
import base64
import csv
import hashlib
import io
import json
import shutil
import subprocess
import zipfile

# Third Party
import pytest

# First Party
from tools.client_wheel import audit_wheel, load_manifest, stage_sources


@pytest.fixture
def source_tree(tmp_path: Path) -> tuple[Path, Path]:
    """Create a fixture boundary; it is not a manifest for the actual client."""
    root = tmp_path / "source"
    packages = [
        "lmcache",
        "lmcache.sdk",
        "lmcache.integration",
        "lmcache.v1",
        "lmcache.v1.client",
        "lmcache.v1.common",
        "lmcache.v1.transport",
        "lmcache.v1.platform",
    ]
    files = {}
    for package in packages:
        name = package.replace(".", "/") + "/__init__.py"
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# SPDX-License-Identifier: Apache-2.0\n")
        files[name] = name
    (root / "lmcache/sdk/py.typed").write_text("")
    files["lmcache/sdk/py.typed"] = "lmcache/sdk/py.typed"
    project = root / "packaging/client/pyproject.toml"
    project.parent.mkdir(parents=True)
    project.write_text(
        '[build-system]\nrequires = ["setuptools>=77,<81", "wheel", '
        '"setuptools-scm>=8"]\n'
        'build-backend = "setuptools.build_meta"\n'
        '[project]\nname = "lmcache-client"\ndynamic = ["version"]\n'
        "[tool.setuptools]\npackages = " + json.dumps(packages) + "\n"
        '[tool.setuptools.package-data]\n"lmcache.sdk" = ["py.typed"]\n'
        '[tool.setuptools_scm]\nversion_file = "lmcache/_version.py"\n'
    )
    files["packaging/client/pyproject.toml"] = "pyproject.toml"
    raw = {
        "schema_version": 1,
        "files": files,
        "wheel_files": [p for p in files if p.startswith("lmcache/")]
        + ["lmcache/_version.py"],
        "metadata_files": ["METADATA", "WHEEL", "RECORD", "top_level.txt"],
        "requires_dist": [],
        "extras": [],
        "entry_points": {},
        "native_needed": {},
    }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(raw))
    return root, manifest


def write_wheel(
    directory: Path,
    manifest_path: Path,
    replacements: dict[str, bytes],
    tag: str = "py3-none-any",
) -> Path:
    """Write a wheel with valid RECORD checksums, optionally injecting bad artifacts."""
    manifest = load_manifest(manifest_path)
    info = "lmcache_client-0.1.0.dist-info"
    payload = {name: b"" for name in manifest.wheel_files}
    payload.update(
        {
            f"{info}/METADATA": (
                b"Metadata-Version: 2.4\nName: lmcache-client\nVersion: 0.1.0\n"
            ),
            f"{info}/WHEEL": (
                "Wheel-Version: 1.0\nRoot-Is-Purelib: "
                + ("true" if tag == "py3-none-any" else "false")
                + f"\nTag: {tag}\n"
            ).encode(),
            f"{info}/top_level.txt": b"lmcache\n",
        }
    )
    payload.update(replacements)
    records = io.StringIO(newline="")
    writer = csv.writer(records)
    for name, data in payload.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
        writer.writerow([name, "sha256=" + digest.decode(), str(len(data))])
    writer.writerow([f"{info}/RECORD", "", ""])
    payload[f"{info}/RECORD"] = records.getvalue().encode()
    wheel = directory / f"lmcache_client-0.1.0-{tag}.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in payload.items():
            archive.writestr(name, data)
    return wheel


def test_exact_staging_preserves_sources(
    source_tree: tuple[Path, Path], tmp_path: Path
) -> None:
    root, policy = source_tree
    secret = root / "lmcache/v1/server/cache_server/private.py"
    secret.parent.mkdir(parents=True)
    secret.write_text("must not be staged")
    before = {
        p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }
    output = stage_sources(root, tmp_path / "stage", load_manifest(policy))
    assert {
        p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()
    } == set(load_manifest(policy).files.values())
    assert not (output / "lmcache/v1/server").exists()
    assert before == {
        p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }
    assert not (root / "lmcache/_version.py").exists()
    with pytest.raises(FileExistsError):
        stage_sources(root, output, load_manifest(policy))


@pytest.mark.parametrize(
    "path",
    [
        "../outside.py",
        "/absolute.py",
        "lmcache/*.py",
        "lmcache//sdk.py",
        "lmcache/./sdk.py",
    ],
)
def test_reject_non_exact_paths(source_tree: tuple[Path, Path], path: str) -> None:
    _, policy = source_tree
    raw = json.loads(policy.read_text())
    raw["files"][path] = "lmcache/extra.py"
    policy.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="exact relative file"):
        load_manifest(policy)


@pytest.mark.parametrize(
    "path",
    [
        "lmcache/v1/server/app.py",
        "lmcache/v1/distributed/api.py",
        "lmcache/v1/multiprocess/client.py",
        "lmcache/cli/main.py",
    ],
)
def test_server_sources_never_allowed(
    source_tree: tuple[Path, Path], path: str
) -> None:
    _, policy = source_tree
    raw = json.loads(policy.read_text())
    raw["files"][path] = path
    policy.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="Server source"):
        load_manifest(policy)


def test_missing_canonical_source_is_blocker(
    source_tree: tuple[Path, Path], tmp_path: Path
) -> None:
    root, policy = source_tree
    (root / "lmcache/v1/client/__init__.py").unlink()
    with pytest.raises(ValueError, match="Canonical source missing"):
        stage_sources(root, tmp_path / "stage", load_manifest(policy))
    assert not (tmp_path / "stage").exists()


def test_symlink_source_is_rejected(
    source_tree: tuple[Path, Path], tmp_path: Path
) -> None:
    root, policy = source_tree
    source = root / "lmcache/sdk/py.typed"
    source.unlink()
    outside = tmp_path / "outside"
    outside.write_text("not canonical")
    source.symlink_to(outside)
    with pytest.raises(ValueError, match="symlinked"):
        stage_sources(root, tmp_path / "stage", load_manifest(policy))


def test_broad_package_discovery_is_rejected(
    source_tree: tuple[Path, Path], tmp_path: Path
) -> None:
    root, policy = source_tree
    (root / "packaging/client/pyproject.toml").write_text(
        '[project]\nname="lmcache-client"\n'
        '[tool.setuptools.packages.find]\ninclude=["lmcache*"]\n'
    )
    with pytest.raises(ValueError, match="explicit setuptools packages"):
        stage_sources(root, tmp_path / "stage", load_manifest(policy))


def test_valid_wheel_and_package_data(
    source_tree: tuple[Path, Path], tmp_path: Path
) -> None:
    _, policy = source_tree
    wheel = write_wheel(tmp_path, policy, {})
    audit_wheel(wheel, load_manifest(policy), "0.1.0")


@pytest.mark.parametrize(
    "name,data,match",
    [
        ("lmcache/v1/server/app.py", b"", "Server source"),
        ("lmcache/sdk/unexpected.dat", b"", "file inventory differs"),
        (
            "lmcache_client-0.1.0.dist-info/METADATA",
            b"Name: lmcache-client\nVersion: 0.1.0\nRequires-Dist: fastapi\n",
            "Server dependency",
        ),
        (
            "lmcache_client-0.1.0.dist-info/METADATA",
            b"Name: lmcache-client\nVersion: 0.1.0\nRequires-Dist: requests\n",
            "Requires-Dist",
        ),
        (
            "lmcache_client-0.1.0.dist-info/METADATA",
            b"Name: lmcache-client\nVersion: 0.1.1\n",
            "version differs",
        ),
        (
            "lmcache_client-0.1.0.dist-info/METADATA",
            b"Name: lmcache\nVersion: 0.1.0\n",
            "distribution name",
        ),
        (
            "lmcache_client-0.1.0.dist-info/METADATA",
            b"Name: lmcache-client\nVersion: 0.1.0\nProvides-Extra: unknown\n",
            "Provides-Extra",
        ),
        (
            "lmcache_client-0.1.0.dist-info/WHEEL",
            b"Wheel-Version: 1.0\nTag: cp312-cp312-linux_x86_64\n",
            "WHEEL version or tags",
        ),
    ],
)
def test_artifact_leaks_fail_audit(
    source_tree: tuple[Path, Path],
    tmp_path: Path,
    name: str,
    data: bytes,
    match: str,
) -> None:
    _, policy = source_tree
    wheel = write_wheel(tmp_path, policy, {name: data})
    with pytest.raises(ValueError, match=match):
        audit_wheel(wheel, load_manifest(policy), "0.1.0")


def test_unexpected_entrypoint_is_rejected(
    source_tree: tuple[Path, Path], tmp_path: Path
) -> None:
    _, policy = source_tree
    raw = json.loads(policy.read_text())
    raw["metadata_files"].append("entry_points.txt")
    policy.write_text(json.dumps(raw))
    wheel = write_wheel(
        tmp_path,
        policy,
        {
            "lmcache_client-0.1.0.dist-info/entry_points.txt": (
                b"[console_scripts]\nlmcache = lmcache.cli.main:main\n"
            ),
        },
    )
    with pytest.raises(ValueError, match="Entry points differ"):
        audit_wheel(wheel, load_manifest(policy), "0.1.0")


def test_record_integrity_is_checked(
    source_tree: tuple[Path, Path], tmp_path: Path
) -> None:
    _, policy = source_tree
    wheel = write_wheel(tmp_path, policy, {})
    with zipfile.ZipFile(wheel) as archive:
        payload = {name: archive.read(name) for name in archive.namelist()}
    payload["lmcache/sdk/py.typed"] = b"changed after RECORD was generated"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in payload.items():
            archive.writestr(name, data)
    with pytest.raises(ValueError, match="RECORD integrity failure"):
        audit_wheel(wheel, load_manifest(policy), "0.1.0")


def test_native_elf_dependencies_are_audited(
    source_tree: tuple[Path, Path], tmp_path: Path
) -> None:
    if shutil.which("gcc") is None or shutil.which("readelf") is None:
        pytest.skip("ELF audit test requires gcc and readelf")
    _, policy = source_tree
    library = tmp_path / "client.so"
    subprocess.run(
        ["gcc", "-shared", "-fPIC", "-x", "c", "-", "-o", str(library)],
        input="int client_probe(void) { return 1; }",
        text=True,
        check=True,
    )
    raw = json.loads(policy.read_text())
    raw["wheel_files"].append("lmcache/client.so")
    raw["native_needed"] = {"lmcache/client.so": []}
    policy.write_text(json.dumps(raw))
    wheel = write_wheel(
        tmp_path,
        policy,
        {"lmcache/client.so": library.read_bytes()},
        tag="cp312-cp312-linux_x86_64",
    )
    audit_wheel(wheel, load_manifest(policy), "0.1.0")
    raw["native_needed"] = {"lmcache/client.so": ["libunexpected.so"]}
    policy.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="Native dependencies differ"):
        audit_wheel(wheel, load_manifest(policy), "0.1.0")
