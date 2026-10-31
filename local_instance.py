"""Stable installation identity and Windows process lifetime guards."""

import ctypes
import hashlib
import os
import time
from ctypes import wintypes
from pathlib import Path


def instance_id(root):
    value = os.path.normcase(str(Path(root).resolve())).encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:24]


class InstanceGuard:
    """A named object is kept alive for exactly one backend process lifetime."""

    def __init__(self, root, component="backend"):
        self.name = "Local\\XYCompanion." + component + "." + instance_id(root)
        self.handle = None
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel.CloseHandle.restype = wintypes.BOOL

    def acquire(self, wait_seconds=0):
        if self.handle:
            return True
        ctypes.set_last_error(0)
        handle = self.kernel.CreateMutexW(None, False, self.name)
        error = ctypes.get_last_error()
        if not handle:
            raise ctypes.WinError(error)
        if error == 183:  # ERROR_ALREADY_EXISTS
            self.kernel.CloseHandle(handle)
            if wait_seconds > 0:
                deadline = time.monotonic() + wait_seconds
                while time.monotonic() < deadline:
                    time.sleep(0.1)
                    if self.acquire():
                        return True
            return False
        self.handle = handle
        return True

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
