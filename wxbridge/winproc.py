"""查进程、跑命令的小工具——重点是「别闪黑窗口」。

之前有个坑：进程管家每 6 秒用 PowerShell 查一次进程，网页控制台又每 4~6 秒
轮询一次状态接口，每次都真的起一个 powershell.exe + conhost.exe，桌面上就是
不停地闪黑框（有人一天被闪几百次）。

这里换掉两件事：
1. 查进程用 Windows 原生 API（ctypes 直接 EnumProcesses），不开子进程，
   自然不会有窗口；
2. 实在要开子进程（跑 taskkill 这类），一律带 CREATE_NO_WINDOW。

结果：查「微信在不在 / 桥接在不在」不再有任何窗口；只有真的启停进程时才动系统。
"""

from __future__ import annotations

import ctypes
import subprocess
import time
from ctypes import wintypes
from typing import Dict, Iterable, List

#: 开子进程时统一带的标志：不建窗口。
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
#: 只要查信息，别要能改内存的权限。
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_psapi = ctypes.WinDLL("psapi.dll")
#: 名字 → (取到的时间, pid 列表)
_cache: Dict[str, tuple] = {}
#: 同一个查询至少隔这么久才重新枚举一次，避免高频接口把 CPU 拉满。
CACHE_TTL = 2.0


def run_hidden(cmd: List[str], timeout: float = 15.0) -> "subprocess.CompletedProcess":
    """跑一个命令，不弹窗口。"""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )


def _pids() -> List[int]:
    """当前所有进程号。"""
    arr = (wintypes.DWORD * 4096)()
    needed = wintypes.DWORD()
    if not _psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(needed)):
        return []
    count = needed.value // ctypes.sizeof(wintypes.DWORD)
    return [int(arr[i]) for i in range(count) if arr[i]]


def _image_name(pid: int) -> str:
    """拿进程的可执行文件名；拿不到就空字符串。"""
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return ""
    finally:
        _kernel32.CloseHandle(handle)


def pids_by_name(names: Iterable[str]) -> List[int]:
    """按可执行文件名找进程号（例如 Weixin.exe）。不开子进程、不闪窗口。"""
    wanted = {str(n).lower() for n in names}
    key = "|".join(sorted(wanted))
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return list(hit[1])
    found: List[int] = []
    try:
        for pid in _pids():
            if _image_name(pid).lower() in wanted:
                found.append(pid)
    except Exception:  # noqa: BLE001 - 查不到就当没有
        found = []
    _cache[key] = (now, found)
    return list(found)


def clear_cache() -> None:
    """启停进程之后立刻重新查，别拿旧结果。"""
    _cache.clear()
