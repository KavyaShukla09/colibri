"""
Clean and Optimize Windows System RAM for Colibri DeepSeek-V4 Inference.
"""
import ctypes
from ctypes import wintypes
import subprocess
import time

kernel32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi
advapi32 = ctypes.windll.advapi32
ntdll = ctypes.windll.ntdll

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_SET_QUOTA = 0x0100
TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_QUERY = 0x0008
SE_PRIVILEGE_ENABLED = 0x00000002

class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]

class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]

class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]

def get_memory_info():
    ms = MEMORYSTATUSEX()
    ms.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
    return {
        "total_gb": ms.ullTotalPhys / (1024.0 ** 3),
        "avail_gb": ms.ullAvailPhys / (1024.0 ** 3),
        "used_gb": (ms.ullTotalPhys - ms.ullAvailPhys) / (1024.0 ** 3),
        "load_pct": ms.dwMemoryLoad,
        "avail_commit_gb": ms.ullAvailPageFile / (1024.0 ** 3)
    }

def enable_privilege(priv_name):
    h_token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, ctypes.byref(h_token)):
        return False
    luid = LUID()
    if not advapi32.LookupPrivilegeValueW(none, priv_name, ctypes.byref(luid)):
        kernel32.CloseHandle(h_token)
        return False
    tp = TOKEN_PRIVILEGES()
    tp.PrivilegeCount = 1
    tp.Privileges[0].Luid = luid
    tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
    res = advapi32.AdjustTokenPrivileges(h_token, False, ctypes.byref(tp), ctypes.sizeof(tp), None, None)
    err = kernel32.GetLastError()
    kernel32.CloseHandle(h_token)
    return res and err == 0

def purge_standby_list():
    if enable_privilege("SeProfileSingleProcessPrivilege"):
        cmd = wintypes.ULONG(4)
        status = ntdll.NtSetSystemInformation(80, ctypes.byref(cmd), ctypes.sizeof(cmd))
        return status == 0
    return False

def trim_all_working_sets():
    pids = (wintypes.DWORD * 4096)()
    bytes_returned = wintypes.DWORD()
    psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(bytes_returned))
    count = int(bytes_returned.value / ctypes.sizeof(wintypes.DWORD))

    trimmed = 0
    for i in range(count):
        pid = pids[i]
        if pid <= 4:
            continue
        h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA, False, pid)
        if h:
            if psapi.EmptyWorkingSet(h):
                trimmed += 1
            kernel32.CloseHandle(h)
    return trimmed

def kill_orphaned_engines():
    try:
        cmd = "Get-Process -Name *deepseek*, *iobench* -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id"
        out = subprocess.check_output(["powershell", "-Command", cmd], text=True).strip()
        if out:
            pids = out.split()
            subprocess.run(["powershell", "-Command", f"Stop-Process -Id {','.join(pids)} -Force"], check=False)
            return len(pids)
    except Exception:
        pass
    return 0

def main():
    print("=" * 65)
    print("       COLIBRI RAM OPTIMIZATION & RECOVERY UTILITY")
    print("=" * 65)

    before = get_memory_info()
    print(f"[BEFORE] Total Physical RAM: {before['total_gb']:.2f} GB")
    print(f"[BEFORE] Available RAM:      {before['avail_gb']:.2f} GB")
    print(f"[BEFORE] Used RAM:           {before['used_gb']:.2f} GB ({before['load_pct']}% load)")
    print("-" * 65)

    killed = kill_orphaned_engines()
    if killed > 0:
        print(f"[CLEANUP] Terminated {killed} orphaned engine process(es).")

    print("[OPTIMIZING] Flushing working sets of idle background processes...")
    trimmed = trim_all_working_sets()
    print(f"[OPTIMIZING] Successfully trimmed working sets for {trimmed} processes.")

    if purge_standby_list():
        print("[ADMIN] Purged Windows System Standby Cache.")
    else:
        print("[INFO] System standby cache purge skipped (requires Administrator terminal).")

    time.sleep(1.0)

    after = get_memory_info()
    freed_mb = (after['avail_gb'] - before['avail_gb']) * 1024.0

    print("=" * 65)
    print(f"[AFTER]  Available RAM:      {after['avail_gb']:.2f} GB  (+{max(0.0, freed_mb):.1f} MB freed)")
    print(f"[AFTER]  Used RAM:           {after['used_gb']:.2f} GB ({after['load_pct']}% load)")
    print("=" * 65)
    print("System RAM is clean and optimized for Colibri DeepSeek-V4 inference.")

if __name__ == "__main__":
    main()
