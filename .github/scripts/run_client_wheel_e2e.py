# SPDX-License-Identifier: Apache-2.0
"""Consume external full/client CPU wheels on Linux (#6379).

Each input directory must contain one wheel and provenance.json with string
fields distribution, version, revision (full Git SHA), and sha256 (wheel digest).
This provisional consumer contract must be aligned with #6315/#6316 before the
E2E becomes a release gate. This script never builds or publishes artifacts.
Revision identity is a producer attestation bound to the wheel's SHA-256, not
something this consumer can infer from an arbitrary wheel filename.
"""

# Standard
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path
from tempfile import TemporaryDirectory
import argparse
import hashlib
import json
import math
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE = Path(__file__).with_name("client_wheel_probe.py")
STOP_PROCESSES = (
    REPO_ROOT / ".buildkite/k3_tests/multiprocess/scripts/stop-processes.py"
)


@dataclass(frozen=True)
class WheelArtifact:
    """A wheel whose identity and digest match its producer's provenance."""

    path: Path
    distribution: str
    version: str
    revision: str
    sha256: str


def read_artifact(directory: Path, distribution: str) -> WheelArtifact:
    """Validate producer provenance against one wheel's bytes and METADATA.

    Args:
        directory: Directory containing one wheel and provenance.json.
        distribution: Expected distribution name.

    Returns:
        Validated artifact, with an absolute wheel path.

    Raises:
        ValueError: The wheel count, provenance, digest, or metadata is invalid.
    """
    wheels = sorted(directory.glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError(
            f"Expected exactly one wheel in {directory}, found {len(wheels)}"
        )
    provenance = json.loads((directory / "provenance.json").read_text())
    fields = ("distribution", "version", "revision", "sha256")
    if not isinstance(provenance, dict) or any(
        not isinstance(provenance.get(field), str) for field in fields
    ):
        raise ValueError(f"Invalid provenance fields in {directory}")
    artifact = WheelArtifact(
        wheels[0].resolve(), *(provenance[field] for field in fields)
    )
    if artifact.distribution != distribution:
        raise ValueError(f"Expected {distribution}, got {artifact.distribution}")
    if not re.fullmatch(r"[0-9a-f]{40}", artifact.revision):
        raise ValueError("Provenance revision must be a full lowercase Git SHA")
    if not re.fullmatch(r"[0-9a-f]{64}", artifact.sha256):
        raise ValueError("Provenance sha256 must be a lowercase SHA-256 digest")
    with artifact.path.open("rb") as wheel_file:
        digest = hashlib.file_digest(wheel_file, "sha256").hexdigest()
    if digest != artifact.sha256:
        raise ValueError(f"Wheel digest mismatch: {artifact.path}")
    with zipfile.ZipFile(artifact.path) as wheel:
        entries = [
            name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(entries) != 1:
            raise ValueError(f"Expected one METADATA in {artifact.path}")
        info = BytesParser().parsebytes(wheel.read(entries[0]))
    name = str(info["Name"]).lower().replace("_", "-")
    if name != distribution or info["Version"] != artifact.version:
        raise ValueError(f"Wheel METADATA does not match provenance: {artifact.path}")
    return artifact


def validate_pair(
    server_dir: Path, client_dir: Path, revision: str, version: str
) -> tuple[WheelArtifact, WheelArtifact]:
    """Reject mismatched artifacts before creating environments or services.

    Args:
        server_dir: Producer's full-wheel artifact directory.
        client_dir: Producer's client-wheel artifact directory.
        revision: Exact revision requested by the consuming CI workflow.
        version: Exact version requested by the consuming CI workflow.

    Returns:
        The full and client artifacts in that order.

    Raises:
        ValueError: Either artifact differs from the requested revision/version.
    """
    server = read_artifact(server_dir, "lmcache")
    client = read_artifact(client_dir, "lmcache-client")
    for artifact in (server, client):
        if artifact.revision != revision or artifact.version != version:
            raise ValueError(
                f"Artifact revision/version mismatch: {artifact.distribution}: "
                f"{artifact.revision}/{artifact.version} != {revision}/{version}"
            )
        print(json.dumps(vars(artifact), default=str), flush=True)
    return server, client


def run_command(
    command: list[str],
    log: Path,
    cwd: Path,
    environment: dict[str, str],
    timeout: float,
) -> None:
    """Run a bounded command and stop its process group on failure/interruption.

    Args:
        command: Executable and arguments, passed without a shell.
        log: File receiving stdout and stderr, including failed commands.
        cwd: Runtime directory outside the source checkout.
        environment: Sanitized environment shared by the test processes.
        timeout: Maximum command duration in seconds.

    Raises:
        subprocess.CalledProcessError: The command exits unsuccessfully.
        subprocess.TimeoutExpired: The deadline expires; the command and its
            descendants are terminated and the direct child is reaped.
    """
    print(f"$ {shlex.join(command)} [cwd={cwd}; log={log}]", flush=True)
    with log.open("ab") as output:
        with subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        ) as process:
            completed = False
            try:
                code = process.wait(timeout=timeout)
                if code:
                    raise subprocess.CalledProcessError(code, command)
                completed = True
            finally:
                if not completed:
                    # Installers can spawn build subprocesses; killing only the
                    # command PID would leave those test-owned children alive.
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)


def wait_ready(server: subprocess.Popen, port: int, timeout: float) -> None:
    """Wait for HTTP health with per-request and total deadlines.

    Args:
        server: Test-owned server process to monitor during startup.
        port: Loopback HTTP health port.
        timeout: Maximum readiness wait in seconds.

    Raises:
        RuntimeError: The server exits early or fails to become healthy.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError(f"Server exited during startup: {server.returncode}")
        try:
            with opener.open(
                f"http://127.0.0.1:{port}/healthcheck",
                timeout=min(2, max(0.01, deadline - time.monotonic())),
            ) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))
    raise RuntimeError(f"Server readiness timed out after {timeout}s")


def run_e2e(args: argparse.Namespace) -> None:
    """Install validated wheels, execute public RPCs, and verify shutdown.

    Args:
        args: Parsed artifact directories, expected revision/version, CPU
            torch version, transport, output directory, and stage deadlines.

    Raises:
        ValueError: Artifacts or runtime/log directories are invalid.
        RuntimeError: Readiness fails.
        subprocess.SubprocessError: Installation, inspection, RPC, or shutdown
            fails or exceeds its deadline. Logs survive temporary env removal.
    """
    server_artifact, client_artifact = validate_pair(
        args.server_artifacts, args.client_artifacts, args.revision, args.version
    )
    environment = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        environment.pop(name, None)
    environment["LMCACHE_TRACK_USAGE"] = "false"
    environment["TORCH_DEVICE_TYPE"] = "cpu"
    environment["LMCACHE_DISABLE_BANNER"] = "1"
    logs = args.output_dir.resolve()
    logs.mkdir(parents=True, exist_ok=True)
    if any(path.name != "runner.log" for path in logs.glob("*.log")):
        raise ValueError("Output directory contains logs; choose a fresh directory")
    with TemporaryDirectory(prefix="lmcache-wheel-e2e-") as temporary:
        work = Path(temporary).resolve()
        if work.is_relative_to(REPO_ROOT):
            raise ValueError("Temporary runtime directory must be outside the checkout")
        probe = work / PROBE.name
        probe.write_bytes(PROBE.read_bytes())
        constraints = work / "constraints.txt"
        constraints.write_text(f"torch=={args.torch_version}\n")
        for role, artifact in (
            ("server", server_artifact),
            ("client", client_artifact),
        ):
            python = work / role / "bin/python"
            log = logs / f"{role}-install.log"
            run_command(
                ["uv", "venv", "--python", sys.executable, str(work / role)],
                log,
                work,
                environment,
                args.install_timeout,
            )
            run_command(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    f"torch=={args.torch_version}",
                    "--index-url",
                    "https://download.pytorch.org/whl/cpu",
                ],
                log,
                work,
                environment,
                args.install_timeout,
            )
            run_command(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "--constraint",
                    str(constraints),
                    str(artifact.path),
                ],
                log,
                work,
                environment,
                args.install_timeout,
            )
            run_command(
                [
                    str(python),
                    "-I",
                    str(probe),
                    "--distribution",
                    artifact.distribution,
                    "--version",
                    args.version,
                ],
                logs / f"{role}-inspection.log",
                work,
                environment,
                args.startup_timeout,
            )
        with ExitStack() as sockets:
            listeners = [sockets.enter_context(socket.socket()) for _ in range(2)]
            for listener in listeners:
                listener.bind(("127.0.0.1", 0))
            request_port, http_port = [
                listener.getsockname()[1] for listener in listeners
            ]
        command = [
            str(work / "server/bin/python"),
            "-I",
            "-m",
            "lmcache.cli.main",
            "server",
            "--transport",
            args.transport,
            "--host",
            "127.0.0.1",
            "--port",
            str(request_port),
            "--http-host",
            "127.0.0.1",
            "--http-port",
            str(http_port),
            "--supported-transfer-mode",
            "engine_driven",
            "--l1-size-gb",
            "0.0625",
            "--no-l1-use-lazy",
            "--shm-name",
            "",
            "--eviction-policy",
            "LRU",
            "--chunk-size",
            "128",
            "--disable-observability",
        ]
        print(f"$ {shlex.join(command)} [cwd={work}]", flush=True)
        with (logs / "server.log").open("wb") as output:
            server = subprocess.Popen(
                command,
                cwd=work,
                env=environment,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            try:
                wait_ready(server, http_port, args.startup_timeout)
                scheme = "grpc" if args.transport == "grpc" else "tcp"
                run_command(
                    [
                        str(work / "client/bin/python"),
                        "-I",
                        str(probe),
                        "--distribution",
                        "lmcache-client",
                        "--version",
                        args.version,
                        "--url",
                        f"{scheme}://127.0.0.1:{request_port}",
                        "--timeout",
                        str(args.request_timeout),
                    ],
                    logs / "client.log",
                    work,
                    environment,
                    args.request_timeout * 2 + 30,
                )
            finally:
                try:
                    run_command(
                        [
                            sys.executable,
                            str(STOP_PROCESSES),
                            str(server.pid),
                            "--ports",
                            str(request_port),
                            str(http_port),
                            "--timeout",
                            str(args.shutdown_timeout),
                        ],
                        logs / "shutdown.log",
                        work,
                        environment,
                        args.shutdown_timeout + 20,
                    )
                finally:
                    if server.poll() is None:
                        server.kill()
                    server.wait(timeout=10)
    print(f"PASS: {args.transport} full/client wheel E2E; logs: {logs}", flush=True)


def main() -> None:
    """Parse external artifact inputs; never fall back to a source installation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-artifacts", type=Path, required=True)
    parser.add_argument("--client-artifacts", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--torch-version", required=True)
    parser.add_argument("--transport", choices=("zmq", "grpc"), default="zmq")
    parser.add_argument("--output-dir", type=Path, required=True)
    for name, default in (
        ("install", 600),
        ("startup", 90),
        ("request", 5),
        ("shutdown", 60),
    ):
        parser.add_argument(f"--{name}-timeout", type=float, default=default)
    args = parser.parse_args()
    if any(
        not math.isfinite(value) or value <= 0
        for value in (
            args.install_timeout,
            args.startup_timeout,
            args.request_timeout,
            args.shutdown_timeout,
        )
    ):
        parser.error("Timeouts must be finite and positive")
    signal.signal(signal.SIGTERM, _terminate)
    run_e2e(args)


def _terminate(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


if __name__ == "__main__":
    main()
