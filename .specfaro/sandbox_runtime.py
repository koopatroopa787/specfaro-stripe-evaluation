"""Credential-free Docker command execution for repair and verification jobs."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid


DEFAULT_IMAGE = "node:22-bookworm-slim"
MAX_OUTPUT_CHARS = 8_000


class SandboxUnavailable(RuntimeError):
    """The required container boundary could not be established."""


class DockerCommandRunner:
    def __init__(self, image: str = DEFAULT_IMAGE):
        if os.name != "posix" or not hasattr(os, "getuid"):
            raise SandboxUnavailable("Docker repair isolation requires a Linux runner")
        self.docker = shutil.which("docker")
        if not self.docker:
            raise SandboxUnavailable("Docker CLI is unavailable")
        if not image or any(char.isspace() for char in image):
            raise SandboxUnavailable("Invalid sandbox image")
        self.image = image

    def _invoke(self, workspace: Path, command: list[str], timeout: int) -> dict:
        root = workspace.resolve(strict=True)
        if not root.is_dir() or workspace.is_symlink():
            raise SandboxUnavailable("Sandbox workspace must be a real directory")
        # The controller can hold model and GitHub credentials; Docker CLI and
        # container processes receive a fixed, credential-free environment.
        client_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                      "HOME": "/tmp", "DOCKER_CONFIG": "/tmp/specfaro-docker-config"}
        container_name = "specfaro-" + uuid.uuid4().hex[:20]
        argv = [self.docker, "run", "--rm", "--name", container_name,
                "--network=none", "--read-only",
                "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--pids-limit=128", "--memory=768m", "--cpus=1",
                "--user", f"{os.getuid()}:{os.getgid()}",
                "--workdir", "/workspace",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m",
                "--mount", f"type=bind,source={root},target=/workspace",
                "--env", "HOME=/tmp", "--env", "CI=true",
                "--env", "npm_config_cache=/tmp/npm-cache",
                "--env", "PATH=/usr/local/bin:/usr/bin:/bin",
                self.image, *command]
        started = time.monotonic()
        try:
            result = subprocess.run(argv, cwd=root, env=client_env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, errors="replace",
                                    timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            try:
                subprocess.run([self.docker, "rm", "-f", container_name], env=client_env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=10, check=False)
            except (OSError, subprocess.TimeoutExpired) as cleanup_error:
                raise SandboxUnavailable("Timed-out sandbox could not be removed") from cleanup_error
            return {"status": "timeout", "reason": f"Timed out after {timeout} seconds",
                    "seconds": round(time.monotonic() - started, 2),
                    "stdout": str(exc.stdout or "")[-MAX_OUTPUT_CHARS:],
                    "stderr": str(exc.stderr or "")[-MAX_OUTPUT_CHARS:]}
        except OSError as exc:
            return {"status": "sandbox_error", "reason": str(exc)[:500],
                    "seconds": round(time.monotonic() - started, 2),
                    "stdout": "", "stderr": ""}
        # Docker reserves these exit codes for launch/runtime failures. Do not
        # mistake an unavailable sandbox or command for a reproduced test.
        status = ("passed" if result.returncode == 0 else
                  "sandbox_error" if result.returncode in {125, 126, 127} else "failed")
        return {"status": status,
                "exit_code": result.returncode,
                "seconds": round(time.monotonic() - started, 2),
                "stdout": result.stdout[-MAX_OUTPUT_CHARS:],
                "stderr": result.stderr[-MAX_OUTPUT_CHARS:]}

    def run_argv(self, argv: list[str], workspace: Path, timeout: int) -> dict:
        if not argv or len(argv) > 24 or any(not isinstance(x, str) or not x for x in argv):
            raise ValueError("Invalid validation command")
        return self._invoke(workspace, argv, timeout)

    def run_shell(self, command: str, workspace: Path, timeout: int) -> dict:
        if not command or len(command) > 16_000:
            raise ValueError("Invalid sandbox shell command")
        return self._invoke(workspace, ["/bin/sh", "-lc", command], timeout)
