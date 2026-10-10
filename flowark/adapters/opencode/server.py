"""OpenCode server lifecycle helpers."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import errno
import fcntl
import os
from pathlib import Path
import signal
from typing import Any

from .settings import OpenCodeRuntime


@dataclass(slots=True)
class OpenCodeServerHandle:
    url: str
    command: list[str]
    pid: int | None
    log_path: str | None = None


class OpenCodeServerProcess:
    """Start a local headless OpenCode server and parse its listening URL."""

    def __init__(
        self,
        *,
        runtime: OpenCodeRuntime,
        hostname: str = "127.0.0.1",
        port: int = 0,
        startup_timeout_seconds: float = 300.0,
    ) -> None:
        self.runtime = runtime
        self.hostname = hostname
        self.port = port
        self.startup_timeout_seconds = startup_timeout_seconds
        self._proc: asyncio.subprocess.Process | None = None
        self._drain_task: asyncio.Task[None] | None = None
        self._output: list[str] = []
        self._startup_lock: _OpenCodeStartupLock | None = None

    async def __aenter__(self) -> OpenCodeServerHandle:
        self.runtime.ensure_directories()
        self.runtime.server_log_path.write_text("", encoding="utf-8")
        env = build_opencode_child_env(self.runtime)
        command = build_opencode_server_command(
            self.runtime,
            hostname=self.hostname,
            port=self.port,
        )
        self._startup_lock = _OpenCodeStartupLock(self.runtime.shared_runtime_cache_dir / "startup.lock")
        await self._startup_lock.acquire()
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.runtime.cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            url = await asyncio.wait_for(
                self._wait_for_listening_url(),
                timeout=self.startup_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            await self._stop()
            output = "\n".join(self._output[-40:]).strip()
            await asyncio.to_thread(self.runtime.cleanup_runtime_directory)
            message = f"OpenCode server failed to start within {self.startup_timeout_seconds:.0f}s"
            if output:
                message = f"{message}:\n{output}"
            raise RuntimeError(message) from exc
        except Exception:
            await self._stop()
            output = "\n".join(self._output[-40:]).strip()
            await asyncio.to_thread(self.runtime.cleanup_runtime_directory)
            if output:
                raise RuntimeError(f"OpenCode server failed to start:\n{output}")
            raise
        finally:
            await self._release_startup_lock()
        return OpenCodeServerHandle(
            url=url,
            command=command,
            pid=self._proc.pid if self._proc is not None else None,
            log_path=str(self.runtime.server_log_path),
        )

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self._release_startup_lock()
        await self._stop()
        await asyncio.to_thread(self.runtime.cleanup_runtime_directory)

    async def _wait_for_listening_url(self) -> str:
        if self._proc is None or self._proc.stdout is None:
            raise RuntimeError("OpenCode process stdout is unavailable")
        while True:
            line_bytes = await self._proc.stdout.readline()
            if not line_bytes:
                code = await self._proc.wait()
                raise RuntimeError(f"OpenCode server exited before ready with code {code}")
            line = line_bytes.decode("utf-8", errors="replace").strip()
            self._record_output(line)
            marker = "opencode server listening on "
            if marker in line:
                self._drain_task = asyncio.create_task(self._drain_remaining_output())
                return line.split(marker, 1)[1].strip()

    async def _drain_remaining_output(self) -> None:
        if self._proc is None or self._proc.stdout is None:
            return
        while True:
            line_bytes = await self._proc.stdout.readline()
            if not line_bytes:
                return
            self._record_output(line_bytes.decode("utf-8", errors="replace").strip())

    def _record_output(self, line: str) -> None:
        if not line:
            return
        self._output.append(line)
        with self.runtime.server_log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    async def _stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None:
            _signal_process_group(proc.pid, signal.SIGTERM)
        drain_task = self._drain_task
        self._drain_task = None
        if drain_task is not None:
            drain_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await drain_task
        if proc is not None:
            try:
                remaining, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            except asyncio.TimeoutError:
                _signal_process_group(proc.pid, signal.SIGKILL)
                remaining, _ = await proc.communicate()
            if remaining:
                for line in remaining.decode("utf-8", errors="replace").splitlines():
                    self._record_output(line.strip())

    async def _release_startup_lock(self) -> None:
        startup_lock = self._startup_lock
        self._startup_lock = None
        if startup_lock is not None:
            await startup_lock.release()


def _signal_process_group(pid: int | None, sig: signal.Signals) -> None:
    if pid is None:
        return
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


class _OpenCodeStartupLock:
    """Serialize cold OpenCode starts that may install shared plugin dependencies."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any | None = None

    async def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a", encoding="utf-8")
        try:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._handle = handle
                    return
                except BlockingIOError:
                    await asyncio.sleep(0.1)
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise
                    await asyncio.sleep(0.1)
        except BaseException:
            handle.close()
            raise

    async def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is not None:
            self._unlock_and_close(handle)

    @staticmethod
    def _unlock_and_close(handle: Any) -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def build_opencode_child_env(
    runtime: OpenCodeRuntime,
    *,
    base_environ: dict[str, str] | None = None,
) -> dict[str, str]:
    env = dict(os.environ if base_environ is None else base_environ)
    for key in list(env):
        if key.startswith("OPENCODE_"):
            env.pop(key, None)
    env.update(runtime.env)
    return env


def _command_has_log_level(command: list[str]) -> bool:
    return any(arg == "--log-level" or arg.startswith("--log-level=") for arg in command)


def build_opencode_server_command(runtime: OpenCodeRuntime, *, hostname: str, port: int) -> list[str]:
    command = [
        *runtime.command,
        "serve",
        f"--hostname={hostname}",
        f"--port={port}",
    ]
    if not _command_has_log_level(runtime.command):
        command.append(f"--log-level={runtime.log_level}")
    return command


__all__ = [
    "OpenCodeServerHandle",
    "OpenCodeServerProcess",
    "build_opencode_child_env",
    "build_opencode_server_command",
]
