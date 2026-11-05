"""Windows GUI lifetime containment. Never assign an external process to this job.

The GUI itself joins a non-inheritable KILL_ON_JOB_CLOSE job *before* creating
children. Windows therefore assigns children atomically during CreateProcess.
Suspension plus membership verification is defense in depth, not a later job
assignment race. Keep the job handle for the entire GUI process lifetime: closing
it early would terminate the GUI too. The OS closes it on normal/abnormal exit.
"""

import ctypes
from ctypes import wintypes
import os
import subprocess
import threading


class BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class IOCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IOCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class ThreadEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG), ("dwFlags", wintypes.DWORD)]


def windows_api():
    if os.name != "nt":
        raise RuntimeError("Owned GUI workers require Windows Job Objects")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ((ctypes.c_void_p, wintypes.LPCWSTR), wintypes.HANDLE),
        "SetInformationJobObject": ((wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD), wintypes.BOOL),
        "AssignProcessToJobObject": ((wintypes.HANDLE, wintypes.HANDLE), wintypes.BOOL),
        "IsProcessInJob": ((wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)), wintypes.BOOL),
        "GetCurrentProcess": ((), wintypes.HANDLE),
        "CloseHandle": ((wintypes.HANDLE,), wintypes.BOOL),
        "CreateToolhelp32Snapshot": ((wintypes.DWORD, wintypes.DWORD), wintypes.HANDLE),
        "Thread32First": ((wintypes.HANDLE, ctypes.POINTER(ThreadEntry)), wintypes.BOOL),
        "Thread32Next": ((wintypes.HANDLE, ctypes.POINTER(ThreadEntry)), wintypes.BOOL),
        "OpenThread": ((wintypes.DWORD, wintypes.BOOL, wintypes.DWORD), wintypes.HANDLE),
        "GetProcessIdOfThread": ((wintypes.HANDLE,), wintypes.DWORD),
        "ResumeThread": ((wintypes.HANDLE,), wintypes.DWORD),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = arguments, result
    return api


class OwnedJob:
    def __init__(self, api=None):
        self.api = api or windows_api()
        self.handle = self.api.CreateJobObjectW(None, None)  # Not inheritable; unnamed.
        if not self.handle:
            raise RuntimeError("Could not create owned-child job")
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.api.CloseHandle(self.handle)
            raise RuntimeError("Could not configure owned-child job")
        if not self.api.AssignProcessToJobObject(self.handle, self.api.GetCurrentProcess()):
            # No child has been created; unsupported nested/restricted jobs stop here.
            self.api.CloseHandle(self.handle)
            raise RuntimeError("Could not contain GUI process; no worker started")
        # Deliberately no close/__del__: after this point the GUI is a job member.

    def resume_owned(self, process):
        member = wintypes.BOOL()
        # CPython's Windows Popen retains the original process HANDLE, avoiding a
        # PID-reuse race. Fail closed on runtimes that do not provide it.
        handle = getattr(process, "_handle", None)
        if (handle is None or not self.api.IsProcessInJob(int(handle), self.handle, ctypes.byref(member))
                or not member.value):
            raise RuntimeError("Suspended worker is not in the GUI job")
        snapshot = self.api.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
        if snapshot in (None, 0, ctypes.c_void_p(-1).value):
            raise RuntimeError("Could not inspect suspended worker thread")
        try:
            entry = ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            found, threads = self.api.Thread32First(snapshot, ctypes.byref(entry)), []
            while found:
                if entry.th32OwnerProcessID == process.pid:
                    threads.append(entry.th32ThreadID)
                entry.dwSize = ctypes.sizeof(entry)
                found = self.api.Thread32Next(snapshot, ctypes.byref(entry))
            if len(threads) != 1:
                raise RuntimeError("Suspended worker primary thread is ambiguous")
            thread = self.api.OpenThread(0x802, False, threads[0])  # SUSPEND_RESUME | QUERY_LIMITED_INFORMATION
            if not thread:
                raise RuntimeError("Could not open suspended worker thread")
            try:
                if self.api.GetProcessIdOfThread(thread) != process.pid or process.poll() is not None:
                    raise RuntimeError("Suspended worker thread identity changed")
                if self.api.ResumeThread(thread) != 1:
                    raise RuntimeError("Unexpected worker suspension state")
            finally:
                self.api.CloseHandle(thread)
        finally:
            self.api.CloseHandle(snapshot)

    def spawn(self, command, *, popen=subprocess.Popen, **options):
        options["creationflags"] = options.get("creationflags", 0) | 0x4  # CREATE_SUSPENDED
        process = popen(command, **options)
        try:
            self.resume_owned(process)
            return process
        except BaseException:
            # Never return an uncontained/unverified worker to the caller.
            try:
                if process.poll() is None:
                    process.terminate()
                process.wait()
            finally:
                for name in ("stdin", "stdout", "stderr"):
                    stream = getattr(process, name, None)
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
            raise


_job = None
_lock = threading.Lock()


def owned_job():
    global _job
    with _lock:
        if _job is None:
            _job = OwnedJob()
        return _job
