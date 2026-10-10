"""Optional Anthropic-compatible capture proxy manager."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any, TextIO
from urllib.error import URLError
from urllib.request import urlopen

from flowark.config import load_capture_proxy_config_defaults
from flowark.timeutil import timestamp_slug_tz8

_DEFAULT_CAPTURE_PROXY_HOST = "127.0.0.1"
_DEFAULT_CAPTURE_PROXY_PORT = 0
_DEFAULT_CAPTURE_PROXY_STRIP_PREFIX = "/api/anthropic"
_HEALTHCHECK_TIMEOUT_SECONDS = 10.0
_HEALTHCHECK_INTERVAL_SECONDS = 0.1


def _is_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_strip_prefix(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return _DEFAULT_CAPTURE_PROXY_STRIP_PREFIX
    if text == "/":
        return ""
    if not text.startswith("/"):
        text = f"/{text}"
    return text.rstrip("/")


def _normalize_listen_host(value: str | None) -> str:
    text = str(value or "").strip()
    return text or _DEFAULT_CAPTURE_PROXY_HOST


def _normalize_port(value: str | None) -> int:
    text = str(value or "").strip()
    if not text:
        return _DEFAULT_CAPTURE_PROXY_PORT
    number = int(text)
    if number < 0 or number > 65535:
        raise ValueError("capture_proxy.yaml 中的 port 必须在 0-65535 之间")
    return number


def _pick_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _build_local_base_url(*, host: str, port: int, strip_prefix: str) -> str:
    prefix = _normalize_strip_prefix(strip_prefix)
    return f"http://{host}:{port}{prefix}"


def _default_capture_dir(*, workspace_root: Path, run_dir: Path | None) -> Path:
    if run_dir is not None:
        return run_dir / "anthropic_capture"
    return workspace_root / "artifacts" / "anthropic_capture" / timestamp_slug_tz8()


@dataclass(frozen=True, slots=True)
class AnthropicCaptureProxyConfig:
    enabled: bool
    listen_host: str
    port: int
    strip_prefix: str
    output_root: Path | None = None


@dataclass(slots=True)
class ManagedAnthropicCaptureProxy:
    upstream_base_url: str
    base_url: str
    capture_dir: Path
    listen_host: str
    port: int
    strip_prefix: str
    log_path: Path
    process: subprocess.Popen[str]
    _log_file: TextIO

    def metadata(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "base_url": self.base_url,
            "upstream_base_url": self.upstream_base_url,
            "capture_dir": str(self.capture_dir),
            "listen_host": self.listen_host,
            "port": self.port,
            "strip_prefix": self.strip_prefix,
            "log_path": str(self.log_path),
            "pid": self.process.pid,
        }

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self._log_file.close()


def resolve_capture_proxy_config(
    workspace_root: Path | str,
) -> AnthropicCaptureProxyConfig:
    raw = load_capture_proxy_config_defaults(workspace_root)
    return AnthropicCaptureProxyConfig(
        enabled=_is_truthy(str(raw.get("enabled", False))),
        listen_host=_normalize_listen_host(str(raw.get("listen_host") or "")),
        port=_normalize_port(str(raw.get("port") or "")),
        strip_prefix=_normalize_strip_prefix(str(raw.get("strip_prefix") or "")),
        output_root=(
            Path(str(raw.get("output_root"))).expanduser().resolve()
            if str(raw.get("output_root") or "").strip()
            else None
        ),
    )


def _wait_for_proxy_health(*, process: subprocess.Popen[str], health_url: str, timeout_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error: str | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"抓包代理进程已提前退出，exit_code={process.returncode}")
        try:
            with urlopen(health_url, timeout=1.0) as resp:  # noqa: S310
                payload = json.loads(resp.read().decode("utf-8"))
                if isinstance(payload, dict) and payload.get("ok") is True:
                    return payload
        except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
            last_error = str(exc)
        time.sleep(_HEALTHCHECK_INTERVAL_SECONDS)
    if last_error:
        raise RuntimeError(f"抓包代理健康检查超时: {last_error}")
    raise RuntimeError("抓包代理健康检查超时")


def start_capture_proxy(
    *,
    workspace_root: Path | str,
    upstream_base_url: str | None,
    run_dir: Path | None = None,
    capture_dir: Path | None = None,
) -> ManagedAnthropicCaptureProxy | None:
    workspace = Path(workspace_root).expanduser().resolve()
    config = resolve_capture_proxy_config(workspace)
    if not config.enabled:
        return None

    upstream = str(upstream_base_url or "").strip().rstrip("/")
    if not upstream:
        raise ValueError("启用抓包代理时必须提供真实的 ANTHROPIC_BASE_URL")

    resolved_capture_dir = (
        capture_dir.expanduser().resolve()
        if capture_dir is not None
        else (
            config.output_root / timestamp_slug_tz8()
            if config.output_root is not None
            else _default_capture_dir(workspace_root=workspace, run_dir=run_dir)
        )
    )
    resolved_capture_dir.mkdir(parents=True, exist_ok=True)

    port = config.port or _pick_free_port(config.listen_host)
    base_url = _build_local_base_url(host=config.listen_host, port=port, strip_prefix=config.strip_prefix)
    log_path = resolved_capture_dir / "proxy.log"
    log_file = log_path.open("w", encoding="utf-8")
    cmd = [
        sys.executable,
        str(workspace / "tools" / "anthropic_capture_proxy.py"),
        "--upstream-base-url",
        upstream,
        "--capture-dir",
        str(resolved_capture_dir),
        "--listen-host",
        config.listen_host,
        "--port",
        str(port),
        "--strip-prefix",
        config.strip_prefix,
    ]
    process = subprocess.Popen(
        cmd,
        cwd=str(workspace),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
        env=dict(os.environ),
    )

    try:
        _wait_for_proxy_health(
            process=process,
            health_url=f"http://{config.listen_host}:{port}/__health",
            timeout_seconds=_HEALTHCHECK_TIMEOUT_SECONDS,
        )
    except Exception:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        log_file.close()
        raise

    return ManagedAnthropicCaptureProxy(
        upstream_base_url=upstream,
        base_url=base_url,
        capture_dir=resolved_capture_dir,
        listen_host=config.listen_host,
        port=port,
        strip_prefix=config.strip_prefix,
        log_path=log_path,
        process=process,
        _log_file=log_file,
    )
