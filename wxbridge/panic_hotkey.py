"""紧急刹车：按一个热键，立刻停掉一切界面操作。

为什么要有：自动化在点微信界面，万一微信卡住、或者它跟你抢鼠标，
你需要能立刻叫停——而不是等它 75 秒后被保险丝干掉。

默认热键：Ctrl + Alt + X
按下之后：
1. 写一个 PAUSE 文件 → 桥接每次发送前都会看它，看到就一条都不点界面；
2. 顺手调一次进程管家的「停止桥接」（停止 = 连尝试都不再做）；
3. 控制台上会显示「已暂停」，点「恢复」或再按一次热键解除。

这个脚本自己是常驻的，单独一个进程，桥接卡死了它也照样能刹车。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import logging
import os
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAUSE_FILE = HERE / "PAUSE"
LOG_FILE = HERE / "panic.log"
CONTROL = "http://127.0.0.1:8767"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
log = logging.getLogger("panic")

#: 热键组合：CTRL + ALT + X  （想换就改这三行）
MOD_CTRL_ALT = 0x0002 | 0x0001      # MOD_CONTROL | MOD_ALT
VK_X = 0x58
HOTKEY_ID = 1
WM_HOTKEY = 0x0312

user32 = ctypes.windll.user32


def call_control(action: str) -> None:
    """调管家停/起桥接（失败了也不影响暂停）。"""
    try:
        req = urllib.request.Request(f"{CONTROL}/api/control/{action}", data=b"{}",
                                     headers={"content-type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=60).read()
    except Exception as exc:  # noqa: BLE001
        log.warning("调管家 %s 失败：%s", action, exc)


def pause() -> None:
    PAUSE_FILE.write_text(str(int(time.time())), encoding="utf-8")
    log.warning("已暂停（写了 PAUSE 文件），正在停桥接")
    threading.Thread(target=call_control, args=("bridge/stop",), daemon=True).start()


def resume() -> None:
    try:
        PAUSE_FILE.unlink()
    except FileNotFoundError:
        pass
    log.info("已恢复（删了 PAUSE 文件）")
    threading.Thread(target=call_control, args=("bridge/start",), daemon=True).start()


def main() -> None:
    if os.name != "nt":
        raise SystemExit("这个只在 Windows 上用")
    if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_CTRL_ALT, VK_X):
        log.error("注册热键失败（可能被别的程序占了）")
        raise SystemExit(1)
    log.info("紧急刹车已就绪：Ctrl+Alt+X 暂停/恢复（PAUSE 文件：%s）", PAUSE_FILE)
    msg = wt.MSG()
    while True:
        got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if got in (0, -1):
            break
        if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
            if PAUSE_FILE.exists():
                resume()
            else:
                pause()


if __name__ == "__main__":
    main()
