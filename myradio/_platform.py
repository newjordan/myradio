"""Small operating-system shims so one source tree runs on Linux, macOS and Windows.

Linux and macOS keep their original behaviour (``fcntl`` locks, ``select`` on pipes, process groups).
On Windows, where those are missing, the same jobs are done with ``msvcrt`` byte-range locks, helper
threads that read and write pipes, and ``taskkill /T`` to stop a program together with everything it
started.
"""
from __future__ import annotations

import os
import queue
import subprocess
import threading

WINDOWS = os.name == "nt"

# os.open() opens in text mode on Windows unless O_BINARY is given, which would rewrite bytes in audio
# and archives. The other two do not exist there; 0 leaves the open unchanged.
O_BINARY = getattr(os, "O_BINARY", 0)
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)

if WINDOWS:
    import msvcrt

    def lock_file(handle, *, shared: bool = False) -> None:
        """Take a non-blocking lock on an open file; BlockingIOError if another process holds it.

        Windows has no shared mode here, so ``shared`` locks are exclusive too. The lock covers the
        file's first byte, which may lie past its end.
        """
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError(exc.errno, "The file is locked by another process.") from None

    def unlock_file(handle) -> None:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def lock_file(handle, *, shared: bool = False) -> None:
        fcntl.flock(handle, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)

    def unlock_file(handle) -> None:
        fcntl.flock(handle, fcntl.LOCK_UN)


# ---- pipes without select() (Windows can only select() on sockets) --------------------------------------------------

class PipeReader:
    """Reads a pipe on a helper thread and hands out blocks through a small bounded queue."""

    def __init__(self, stream, *, block: int = 65536, depth: int = 16):
        self._fd = stream.fileno()
        self._block = block
        self._queue: queue.Queue = queue.Queue(depth)
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._run, name="pipe-reader", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._closed.is_set():
            try:
                data = os.read(self._fd, self._block)
            except (OSError, ValueError):
                data = b""
            while not self._closed.is_set():
                try:
                    self._queue.put(data, timeout=0.1)
                    break
                except queue.Full:
                    continue
            if not data:
                return

    def read(self, timeout: float):
        """The next block, ``b""`` once the pipe has ended, or ``None`` if nothing arrived within ``timeout``."""
        try:
            return self._queue.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None

    def close(self, wait: float = 1.0) -> None:
        """Stop reading. Call after the other end has gone, and before closing the pipe itself."""
        self._closed.set()
        self._thread.join(wait)


class PipeWriter:
    """Writes to a pipe on a helper thread; ``offer`` never blocks the caller."""

    def __init__(self, stream, *, depth: int = 4):
        self._fd = stream.fileno()
        self._queue: queue.Queue = queue.Queue(depth)
        self._closed = threading.Event()
        self.failed = False
        self._thread = threading.Thread(target=self._run, name="pipe-writer", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._closed.is_set():
            try:
                data = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            view = memoryview(data)
            try:
                while view and not self._closed.is_set():
                    view = view[os.write(self._fd, view):]
            except (OSError, ValueError):
                self.failed = True
                return

    def offer(self, data) -> bool:
        """Queue ``data`` for writing; False if the writer is still busy with earlier blocks."""
        try:
            self._queue.put_nowait(data)
            return True
        except queue.Full:
            return False

    def close(self, wait: float = 1.0) -> None:
        """Stop writing. Call after the other end has gone, and before closing the pipe itself."""
        self._closed.set()
        self._thread.join(wait)


# ---- stopping a program and everything it started ------------------------------------------------------------------

def kill_tree(process: subprocess.Popen) -> None:
    """Windows: forcibly stop ``process`` and its child processes (the counterpart of killing a process group)."""
    if process.poll() is not None:
        return
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    taskkill = os.path.join(system_root, "System32", "taskkill.exe")
    try:
        subprocess.run([taskkill, "/F", "/T", "/PID", str(process.pid)], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        process.kill()
    except OSError:
        pass


# A new process group on Windows, so a console Ctrl+C aimed at myradio does not also hit its tools.
NEW_GROUP_FLAGS = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
