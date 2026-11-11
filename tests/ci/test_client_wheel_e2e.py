# SPDX-License-Identifier: Apache-2.0
"""Exercise the wheel consumer's public CLI and artifact rejection boundaries.

The tiny wheel fixtures below contain only METADATA, not an LMCache runtime.
They test preflight failures, never stand in for a real client/server E2E.
"""

# Standard
from importlib import util
from pathlib import Path
from types import ModuleType
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import zipfile

# Third Party
import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / ".github/scripts/run_client_wheel_e2e.py"
PROBE = ROOT / ".github/scripts/client_wheel_probe.py"
REVISION = "a" * 40
VERSION = "0.0.0+e2e"


def load_runner() -> ModuleType:
    """Load the stdlib-only consumer without importing the lmcache checkout."""
    spec = util.spec_from_file_location("client_wheel_e2e", RUNNER)
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def artifact(directory: Path, name: str) -> Path:
    """Create a METADATA-only wheel and producer provenance for preflight tests."""
    directory.mkdir()
    normalized = name.replace("-", "_")
    wheel = directory / f"{normalized}-{VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            f"{normalized}-{VERSION}.dist-info/METADATA",
            f"Metadata-Version: 2.3\nName: {name}\nVersion: {VERSION}\n",
        )
    provenance = {
        "distribution": name,
        "version": VERSION,
        "revision": REVISION,
        "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
    }
    (directory / "provenance.json").write_text(json.dumps(provenance))
    return wheel


def change_provenance(directory: Path, **updates: str) -> None:
    """Alter producer metadata without changing wheel contents."""
    path = directory / "provenance.json"
    data = json.loads(path.read_text())
    data.update(updates)
    path.write_text(json.dumps(data))


def test_matching_artifacts_are_accepted(tmp_path: Path) -> None:
    """The requested revision and version identify both independently hashed wheels."""
    server = tmp_path / "server"
    client = tmp_path / "client"
    artifact(server, "lmcache")
    artifact(client, "lmcache-client")
    pair = load_runner().validate_pair(server, client, REVISION, VERSION)
    assert [item.distribution for item in pair] == ["lmcache", "lmcache-client"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("revision", "b" * 40, "revision/version mismatch"),
        ("revision", "abcdef0", "full lowercase Git SHA"),
        ("version", "0.0.1", "METADATA does not match"),
        ("distribution", "lmcache", "Expected lmcache-client"),
        ("sha256", "0" * 64, "digest mismatch"),
    ],
)
def test_bad_artifact_fails_before_installation(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    """No installer or server can run when provenance or wheel identity differs."""
    server = tmp_path / "server"
    client = tmp_path / "client"
    artifact(server, "lmcache")
    artifact(client, "lmcache-client")
    change_provenance(client, **{field: value})
    logs = tmp_path / "logs"
    environment = os.environ.copy()
    # An empty PATH proves preflight cannot silently invoke an installer.
    environment["PATH"] = ""
    result = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--server-artifacts",
            str(server),
            "--client-artifacts",
            str(client),
            "--revision",
            REVISION,
            "--version",
            VERSION,
            "--torch-version",
            "2.13.0",
            "--output-dir",
            str(logs),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert message in result.stderr
    assert not logs.exists()


def test_same_wrong_pair_does_not_satisfy_requested_revision(tmp_path: Path) -> None:
    """Two matching stale artifacts must still fail a new revision's CI gate."""
    server = tmp_path / "server"
    client = tmp_path / "client"
    artifact(server, "lmcache")
    artifact(client, "lmcache-client")
    with pytest.raises(ValueError, match="revision/version mismatch"):
        load_runner().validate_pair(server, client, "b" * 40, VERSION)


def test_duplicate_wheels_are_rejected(tmp_path: Path) -> None:
    """A consumer must not pick an arbitrary wheel from an ambiguous artifact."""
    wheel = artifact(tmp_path / "client", "lmcache-client")
    wheel.with_name("another.whl").write_bytes(wheel.read_bytes())
    with pytest.raises(ValueError, match="exactly one wheel"):
        load_runner().read_artifact(wheel.parent, "lmcache-client")


def test_wheel_bytes_are_bound_to_provenance(tmp_path: Path) -> None:
    """A changed wheel fails even when its name and METADATA have not changed."""
    wheel = artifact(tmp_path / "client", "lmcache-client")
    with wheel.open("ab") as output:
        output.write(b"changed")
    with pytest.raises(ValueError, match="digest mismatch"):
        load_runner().read_artifact(wheel.parent, "lmcache-client")


@pytest.fixture
def probe_environment(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create an offline venv with a minimal installed client distribution fixture."""
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(tmp_path / "venv")],
        check=True,
        timeout=20,
    )
    python = tmp_path / "venv/bin/python"
    site = Path(
        subprocess.check_output(
            [
                str(python),
                "-I",
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ],
            text=True,
            timeout=10,
        ).strip()
    )
    package = site / "lmcache"
    factory = package / "v1/client/transport/factory.py"
    factory.parent.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "0.0.0+e2e"\n')
    factory.write_text(
        "from importlib import import_module\n"
        "from types import SimpleNamespace\n"
        "class RequestClientFactory:\n"
        "    @staticmethod\n"
        "    def create(url):\n"
        "        import_module('lmcache.v1.transport.grpc._proto_gen.common_pb2')\n"
        "        return SimpleNamespace(\n"
        "            ping=lambda _: SimpleNamespace(result=lambda timeout: True),\n"
        "            get_chunk_size=lambda: "
        "SimpleNamespace(result=lambda timeout: 128),\n"
        "            close=lambda: None)\n"
    )
    info = site / f"lmcache_client-{VERSION}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Name: lmcache-client\nVersion: {VERSION}\n")
    (info / "RECORD").write_text(
        "lmcache/__init__.py,,\nlmcache/v1/client/transport/factory.py,,\n"
    )
    probe = tmp_path / "probe.py"
    probe.write_bytes(PROBE.read_bytes())
    return python, site, probe


def probe_command(
    environment: tuple[Path, Path, Path], *extra_args: str
) -> subprocess.CompletedProcess:
    """Execute the real inspection CLI with Python isolation from a temporary cwd."""
    python, _, probe = environment
    return subprocess.run(
        [
            str(python),
            "-I",
            str(probe),
            "--distribution",
            "lmcache-client",
            "--version",
            VERSION,
            *extra_args,
        ],
        cwd=probe.parent,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_installed_distribution_is_inspected(
    probe_environment: tuple[Path, Path, Path],
) -> None:
    """The inspector accepts only files recorded by the selected installed wheel."""
    result = probe_command(probe_environment)
    assert result.returncode == 0, result.stderr
    assert (
        json.loads(result.stdout.splitlines()[-1])["distribution"] == "lmcache-client"
    )


def test_missing_transport_retains_installed_identity(
    probe_environment: tuple[Path, Path, Path],
) -> None:
    """Broken wheel imports must not hide the installed distribution metadata."""
    _, site, _ = probe_environment
    (site / "lmcache/v1/client/transport/factory.py").unlink()
    result = probe_command(probe_environment)
    assert result.returncode != 0
    identity = json.loads(result.stdout.splitlines()[0])
    assert identity["distribution"] == "lmcache-client"
    assert identity["version"] == VERSION
    assert "ModuleNotFoundError" in result.stderr


@pytest.mark.parametrize("recorded", [True, False])
def test_lazy_shared_binding_must_belong_to_installed_wheel(
    probe_environment: tuple[Path, Path, Path], recorded: bool
) -> None:
    """Post-request inspection must audit lazily imported shared gRPC bindings.

    The fixture client returns constants without networking. This verifies
    installed-file ownership after factory.create, not an RPC E2E result.
    """
    _, site, _ = probe_environment
    relative = "lmcache/v1/transport/grpc/_proto_gen/common_pb2.py"
    binding = site / relative
    binding.parent.mkdir(parents=True)
    binding.write_text("SCHEMA_VERSION = 1\n")
    if recorded:
        with (site / f"lmcache_client-{VERSION}.dist-info/RECORD").open("a") as record:
            record.write(f"{relative},,\n")
    result = probe_command(probe_environment, "--url", "grpc://127.0.0.1:1")
    assert '"ping": true' in result.stdout
    if recorded:
        assert result.returncode == 0, result.stderr
        identity = json.loads(result.stdout.splitlines()[-1])
        assert "lmcache.v1.transport.grpc._proto_gen.common_pb2" in identity["imports"]
    else:
        assert result.returncode != 0
        assert "Import outside installed wheel" in result.stderr
        assert "lmcache.v1.transport.grpc._proto_gen.common_pb2" in result.stderr


def test_mixed_distributions_fail(probe_environment: tuple[Path, Path, Path]) -> None:
    """A full distribution hidden beside the client must fail inspection."""
    _, site, _ = probe_environment
    info = site / f"lmcache-{VERSION}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Name: lmcache\nVersion: {VERSION}\n")
    result = probe_command(probe_environment)
    assert result.returncode != 0
    assert "Unexpected distribution installed: lmcache" in result.stderr


def test_source_injected_by_pth_fails(
    probe_environment: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """An editable-style .pth escape must fail even under Python -I."""
    _, site, _ = probe_environment
    source = tmp_path / "checkout"
    package = source / "lmcache"
    factory = package / "v1/client/transport/factory.py"
    factory.parent.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    factory.write_text("class RequestClientFactory: pass\n")
    (site / "checkout.pth").write_text(
        f"import sys; sys.path.insert(0, {str(source)!r})\n"
    )
    result = probe_command(probe_environment)
    assert result.returncode != 0
    assert "Import outside installed wheel" in result.stderr


def test_readiness_detects_early_server_exit() -> None:
    """Startup failure must be reported immediately rather than consume the deadline."""
    process = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
    process.wait(timeout=5)
    with pytest.raises(RuntimeError, match="exited during startup: 3"):
        load_runner().wait_ready(process, 1, 90)


def test_command_timeout_reaps_child_and_keeps_log(tmp_path: Path) -> None:
    """A timed-out command must also stop a descendant holding a listening port."""
    log = tmp_path / "probe.log"
    listener = (
        "import socket,time; s=socket.socket(); s.bind(('127.0.0.1',0)); "
        "s.listen(); print(s.getsockname()[1],flush=True); time.sleep(60)"
    )
    parent = (
        "import json,os,subprocess,sys,time; "
        f"p=subprocess.Popen([sys.executable,'-u','-c',{listener!r}],"
        "stdout=subprocess.PIPE,text=True); "
        "port=int(p.stdout.readline()); "
        "print(json.dumps({'parent':os.getpid(),'child':p.pid,'port':port}),"
        "flush=True); "
        "time.sleep(60)"
    )
    command = [
        sys.executable,
        "-u",
        "-c",
        parent,
    ]
    with pytest.raises(subprocess.TimeoutExpired):
        load_runner().run_command(command, log, tmp_path, os.environ.copy(), 2)
    data = json.loads(log.read_text())
    try:
        with pytest.raises(ProcessLookupError):
            os.kill(data["parent"], 0)
        # SIGKILL delivery to descendants can lag the direct child's wait().
        deadline = time.monotonic() + 5
        while True:
            try:
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", data["port"]))
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
    finally:
        # Keep a failing regression from leaving its listener running.
        try:
            os.kill(data["child"], signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_command_failure_preserves_output(tmp_path: Path) -> None:
    """The failing probe's exit status and stderr remain available to CI."""
    log = tmp_path / "probe.log"
    command = [
        sys.executable,
        "-c",
        "import sys; print('probe failed', file=sys.stderr); sys.exit(7)",
    ]
    with pytest.raises(subprocess.CalledProcessError) as result:
        load_runner().run_command(command, log, tmp_path, os.environ.copy(), 5)
    assert result.value.returncode == 7
    assert "probe failed" in log.read_text()
