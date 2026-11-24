"""Cross-platform file locking, atomic replace, and PID liveness helpers.

Uses Python standard library only (macOS, Linux, and Windows).
"""

import contextlib
import errno
import os
import subprocess
import time

if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    import msvcrt

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
    _SYNCHRONIZE = 0x00100000
    _WAIT_TIMEOUT = 0x00000102
    _ERROR_LOCK_VIOLATION = 33
    _ERROR_IO_PENDING = 997

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _kernel32.LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_OVERLAPPED),
    ]
    _kernel32.LockFileEx.restype = wintypes.BOOL

    _kernel32.UnlockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_OVERLAPPED),
    ]
    _kernel32.UnlockFileEx.restype = wintypes.BOOL

    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE

    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD

    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
else:
    import fcntl


@contextlib.contextmanager
def file_lock(lock_path, exclusive=True, timeout=15.0):
    """Acquire an advisory inter-process lock on ``lock_path``.

    Supports shared (``exclusive=False``) and exclusive (``exclusive=True``)
    locks with bounded wait timeout on both POSIX (``fcntl.flock``) and
    Windows (``LockFileEx``).
    """
    parent = os.path.dirname(os.path.abspath(lock_path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    if os.name != "nt":
        lf = open(lock_path, "a+", encoding="utf-8")
        try:
            mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            deadline = time.monotonic() + max(0.5, float(timeout))
            locked = False
            while True:
                try:
                    fcntl.flock(lf.fileno(), mode | fcntl.LOCK_NB)
                    locked = True
                    break
                except (BlockingIOError, OSError) as exc:
                    if not isinstance(exc, BlockingIOError) and exc.errno not in (
                        errno.EAGAIN,
                        errno.EWOULDBLOCK,
                    ):
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"Timed out waiting for lock {lock_path} after {timeout}s"
                        )
                    time.sleep(0.02)
            try:
                yield lf
            finally:
                if locked:
                    try:
                        fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
        finally:
            lf.close()
        return

    # Windows: use LockFileEx on byte 0 (supports both shared and exclusive locks)
    lf = open(lock_path, "a+b")
    try:
        handle = msvcrt.get_osfhandle(lf.fileno())
        flags = _LOCKFILE_FAIL_IMMEDIATELY
        if exclusive:
            flags |= _LOCKFILE_EXCLUSIVE_LOCK

        deadline = time.monotonic() + max(0.5, float(timeout))
        locked = False
        while True:
            overlapped = _OVERLAPPED()
            ok = _kernel32.LockFileEx(
                handle, flags, 0, 1, 0, ctypes.byref(overlapped)
            )
            if ok:
                locked = True
                break
            err = ctypes.get_last_error()
            if err not in (_ERROR_LOCK_VIOLATION, _ERROR_IO_PENDING):
                raise ctypes.WinError(err)
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for lock {lock_path} after {timeout}s"
                )
            time.sleep(0.015)

        try:
            yield lf
        finally:
            if locked:
                try:
                    unlock_ov = _OVERLAPPED()
                    _kernel32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(unlock_ov))
                except Exception:
                    pass
    finally:
        lf.close()


def atomic_replace(src, dst, retries=8, base_delay=0.025):
    """Atomically replace ``dst`` with ``src``, retrying transient Windows locks."""
    if os.name != "nt":
        os.replace(src, dst)
        return
    last_exc = None
    for attempt in range(max(1, retries)):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            last_exc = exc
            if attempt + 1 >= retries:
                raise
            time.sleep(base_delay * (attempt + 1))
    if last_exc is not None:
        raise last_exc


def _posix_pid_not_zombie(pid):
    """Return False if POSIX process ``pid`` is a zombie (Z) or vanished; else True."""
    proc_stat = f"/proc/{pid}/stat"
    if os.path.isdir("/proc"):
        try:
            with open(proc_stat, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
            rparen = raw.rfind(")")
            if rparen != -1:
                after = raw[rparen + 1:].lstrip()
                if after and after[0] in ("Z", "z"):
                    return False
            return True
        except FileNotFoundError:
            return False
        except OSError:
            pass

    try:
        res = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        if res.returncode == 0:
            st = (res.stdout or "").strip()
            if not st:
                return False
            if st[0] in ("Z", "z"):
                return False
            return True
        # ps returned non-zero: check if process vanished right after kill(0)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True
    except (OSError, subprocess.SubprocessError):
        # Fail open only if ps cannot be executed
        return True


def is_pid_alive(pid):
    """Return True if process ``pid`` is currently alive (macOS/Linux/Windows).

    Zombies on POSIX are treated as not alive.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False

    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but belongs to another user.
            return True
        except OSError as exc:
            return exc.errno == errno.EPERM
        return _posix_pid_not_zombie(pid)

    handle = _kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
    if not handle:
        # Access denied (5) still means the PID exists.
        return ctypes.get_last_error() == 5
    try:
        return _kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
    finally:
        _kernel32.CloseHandle(handle)
