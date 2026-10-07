"""Deleting a test project folder on Windows inside OneDrive: right after a
test wrote its files, OneDrive (or a virus scanner) can hold the folder for
a moment and a plain shutil.rmtree fails with "Access is denied". Retry for
a few seconds instead of failing the test over its own cleanup."""
import os
import shutil
import stat
import sys
import time


def _writable_then_retry(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def rmtree(path, seconds=8):
    deadline = time.time() + seconds
    kw = {"onexc": _writable_then_retry} if sys.version_info >= (3, 12) else {"onerror": _writable_then_retry}
    while os.path.exists(path):
        try:
            shutil.rmtree(path, **kw)
        except PermissionError:
            if time.time() > deadline:
                raise
            time.sleep(0.25)
