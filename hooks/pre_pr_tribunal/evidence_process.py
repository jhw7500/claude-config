"""Linux pidfd-owned execution with bounded, concurrently drained output.

Only observed descendants are owned. A daemon that reparents between samples
cannot be claimed; this is not a sandbox or a complete process containment API.
"""
from __future__ import annotations

import os
from pathlib import Path
import selectors
import select
import signal
import subprocess
import time

from .model import SchemaError


_DESCENDANT_SETTLE_SECONDS = 0.1


def _descendants(roots):
    parents, starts = {}, {}
    for item in Path('/proc').iterdir():
        if not item.name.isdigit():
            continue
        try:
            fields = (item / 'stat').read_text(encoding='ascii').rsplit(')', 1)[1].split()
            pid = int(item.name)
            parents[pid], starts[pid] = int(fields[1]), int(fields[19])
        except (OSError, ValueError, IndexError):
            continue
    found, frontier = set(), set(roots)
    while frontier:
        frontier = {pid for pid, parent in parents.items() if parent in frontier and pid not in found}
        found.update(frontier)
    return {pid: starts[pid] for pid in found}


def _track(root, handles):
    poller = select.poll()
    poller.register(handles[root], select.POLLIN)
    if poller.poll(0):
        return
    # Revalidate ancestry and start time after opening the stable kernel handle.
    for pid, start in _descendants([root]).items():
        if pid in handles:
            continue
        try:
            fd = os.pidfd_open(pid)
        except ProcessLookupError:
            continue
        if poller.poll(0) or _descendants([root]).get(pid) != start:
            os.close(fd)
        else:
            handles[pid] = fd


def _kill(handles):
    failed = False
    for fd in handles.values():
        try:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            failed = True
    return failed


def _alive(fd):
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    return not poller.poll(0)


def _settle_descendants(root, handles):
    """Give already-owned descendants one bounded chance to finish naturally."""
    children = {pid: fd for pid, fd in handles.items() if pid != root and _alive(fd)}
    deadline = time.monotonic() + _DESCENDANT_SETTLE_SECONDS
    while children:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        poller = select.poll()
        for fd in children.values():
            poller.register(fd, select.POLLIN)
        exited = {fd for fd, _event in poller.poll(max(1, int(remaining * 1000)))}
        children = {pid: fd for pid, fd in children.items()
                    if fd not in exited and _alive(fd)}
    return children


def run_owned(argv, *, cwd, env, timeout_seconds, limit, control_pipe=None,
              pid_namespace_contained=False):
    """Execute caller argv only; preserve root exit, timeout and overflow facts.

    ``pid_namespace_contained`` is reserved for a caller that independently
    established a PID namespace with a reaper. It permits a bounded teardown
    settle that would be unsafe for an ordinary host process tree.
    """
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise SchemaError('EVIDENCE_PROCESS_UNSUPPORTED')
    if (type(pid_namespace_contained) is not bool
            or pid_namespace_contained and control_pipe is None):
        raise SchemaError('EVIDENCE_PROCESS_UNSUPPORTED')
    started = time.monotonic()
    # Allocate fallible non-process resources before creating a live child.
    selector = selectors.DefaultSelector()
    process = None
    handles = {}
    streams = [bytearray(), bytearray()]
    control = bytearray()
    control_invalid = False
    timed_out = overflow = cleanup_failed = intervention = False
    control_read = control_write = None
    if control_pipe is not None:
        if (not isinstance(control_pipe, tuple) or len(control_pipe) != 2
                or not all(type(fd) is int and fd >= 0 for fd in control_pipe)):
            raise SchemaError('EVIDENCE_PROCESS_UNSUPPORTED')
        control_read, control_write = control_pipe
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, shell=False,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
            pass_fds=(() if control_write is None else (control_write,)))
        if control_write is not None:
            os.close(control_write)
            control_write = None
        handles[process.pid] = os.pidfd_open(process.pid)
        for index, pipe in enumerate((process.stdout, process.stderr)):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, index)
        if control_read is not None:
            os.set_blocking(control_read, False)
            selector.register(control_read, selectors.EVENT_READ, 2)
        deadline = started + timeout_seconds
        cleanup_deadline = None
        while selector.get_map() or process.poll() is None:
            _track(process.pid, handles)
            now = time.monotonic()
            if now >= deadline and not timed_out:
                timed_out = True
                cleanup_failed |= _kill(handles)
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        cleanup_failed = True
                cleanup_deadline = now + 1
            if cleanup_deadline is not None and now >= cleanup_deadline:
                break  # Unknown pipe holders cannot force an unbounded drain.
            for key, _ in selector.select(.01):
                if isinstance(key.fileobj, int):
                    chunk = os.read(key.fileobj, 65536)
                else:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == 2:
                    if len(control) + len(chunk) > 16:
                        control_invalid = True
                    control.extend(chunk[:max(0, 16 - len(control))])
                    continue
                remaining = limit - sum(map(len, streams))
                if len(chunk) > remaining:
                    overflow = True
                if remaining > 0:
                    streams[key.data].extend(chunk[:remaining])
        if pid_namespace_contained:
            # Bubblewrap's PID namespace reaper contains late descendants. Give
            # its already-owned teardown a short grace before judging survivors.
            children = _settle_descendants(process.pid, handles)
        else:
            children = {pid: fd for pid, fd in handles.items()
                        if pid != process.pid and _alive(fd)}
        intervention = bool(children)
        cleanup_failed |= _kill(children)
    finally:
        cleanup_failed |= _kill(handles)
        try:
            if process is not None:
                if process.poll() is None:
                    # Still-unreaped direct child remains owned if pidfd failed.
                    try:
                        process.kill()
                    except OSError:
                        cleanup_failed = True
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    cleanup_failed = True
        finally:
            for fd in handles.values():
                try:
                    os.close(fd)
                except OSError:
                    cleanup_failed = True
            try:
                selector.close()
            except OSError:
                cleanup_failed = True
            if process is not None:
                for pipe in (process.stdout, process.stderr):
                    try:
                        pipe.close()
                    except OSError:
                        cleanup_failed = True
            for fd in (control_read, control_write):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
    return {'stdout': bytes(streams[0]), 'stderr': bytes(streams[1]),
            'exit_code': process.returncode, 'timed_out': timed_out,
            'overflow': overflow, 'cleanup_failed': cleanup_failed, 'intervention': intervention,
            'duration_ms': (time.monotonic() - started) * 1000,
            'control': None if control_pipe is None or control_invalid else bytes(control)}
