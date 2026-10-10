"""Small process-tree termination helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
import signal
import subprocess


@dataclass(frozen=True, slots=True)
class ProcessTreeSnapshot:
    root_pid: int
    pids: tuple[int, ...]
    pgids: tuple[int, ...]


def collect_descendant_pids(root_pid: int) -> list[int]:
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            capture_output=True,
            check=False,
            text=True,
            timeout=2,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []

    children_by_parent: dict[int, list[int]] = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        children_by_parent.setdefault(ppid, []).append(pid)

    descendants: list[int] = []
    stack = list(children_by_parent.get(int(root_pid), []))
    while stack:
        pid = stack.pop()
        descendants.append(pid)
        stack.extend(children_by_parent.get(pid, []))
    return descendants


def snapshot_process_tree(root_pid: int | None) -> ProcessTreeSnapshot | None:
    if not isinstance(root_pid, int) or root_pid <= 0:
        return None
    target_pids = [*collect_descendant_pids(root_pid), root_pid]
    seen_pgids: set[int] = set()
    pgids: list[int] = []
    pids: list[int] = []
    root_pgid: int | None = None
    for pid in target_pids:
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            continue
        except Exception:
            pgid = None
        pids.append(pid)
        if pid == root_pid and isinstance(pgid, int) and pgid > 0:
            root_pgid = pgid
        if isinstance(pgid, int) and pgid > 0 and pgid not in seen_pgids:
            seen_pgids.add(pgid)
            pgids.append(pgid)
    if root_pgid is None and root_pid not in seen_pgids:
        pgids.append(root_pid)
    return ProcessTreeSnapshot(root_pid=root_pid, pids=tuple(pids), pgids=tuple(pgids))


def signal_process_tree(
    root_pid: int | None,
    sig: signal.Signals | int,
    *,
    snapshot: ProcessTreeSnapshot | None = None,
) -> bool:
    tree = snapshot if snapshot is not None else snapshot_process_tree(root_pid)
    if tree is None:
        return False
    signaled_group = False
    for pgid in tree.pgids:
        if not isinstance(pgid, int) or pgid <= 0:
            continue
        try:
            os.killpg(pgid, sig)
            signaled_group = True
        except ProcessLookupError:
            continue
        except Exception:
            pass
    if signaled_group:
        return True
    signaled_pid = False
    for pid in tree.pids:
        if not isinstance(pid, int) or pid <= 0:
            continue
        try:
            os.kill(pid, sig)
            signaled_pid = True
        except ProcessLookupError:
            continue
        except Exception:
            pass
    return signaled_pid


async def terminate_asyncio_process_tree(
    proc: asyncio.subprocess.Process,
    *,
    wait_timeout_seconds: float = 5.0,
) -> None:
    snapshot = snapshot_process_tree(getattr(proc, "pid", None))
    signaled = signal_process_tree(getattr(proc, "pid", None), signal.SIGKILL, snapshot=snapshot)
    if proc.returncode is not None:
        return
    if not signaled:
        try:
            proc.kill()
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(proc.wait(), timeout=wait_timeout_seconds)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=wait_timeout_seconds)
        except asyncio.TimeoutError:
            return
