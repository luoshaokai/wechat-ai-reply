"""微信助手的进程管家（控制服务）。

桥接进程自己管不了自己死掉之后的事：桥接一停，8766 的网页控制台也跟着没了，
按钮也就点不着了。所以另起一个常驻的小进程（默认 8767），专门管「谁在跑」：

    网页控制台（8766，住在桥接里）
        │  调 8767
        ▼
    进程管家（8767）── 启动/停止/重启 桥接（8765）
                   └── 打开微信、看门狗、紧急刹车

只绑 127.0.0.1。接口带 Origin 校验：只放行本机页面（命令行没有 Origin，放行）。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import winproc

HERE = Path(__file__).resolve().parent
LOG = HERE / "supervisor.log"
#: 记着「桥接是不是该活着」。管家自己重启后还记得，免得它一重启就把桥接拉起来，
#: 违背用户在控制台上点的「停止」。
STATE = HERE / "supervisor.state"

#: 桥接的端口，起来了就算「桥接在跑」
BRIDGE_PORT = 8765
#: 网页控制台的端口（住在桥接里）
DASHBOARD_PORT = 8766

WEIXIN_EXE = Path(r"C:\Program Files\Tencent\Weixin\Weixin.exe")

log = logging.getLogger("wxsupervisor")


# ---------------------------------------------------------------- 要不要活着

def wanted() -> bool:
    """桥接是不是该活着（用户在控制台上点了「停止」就 False）。

    用 utf-8-sig 读：这文件也可能被人用记事本/PowerShell 改过，带上 BOM 的话
    「off」前面会多个隐形字符，判成「还该活着」，刚停的桥接 6 秒后又起来了。
    """
    try:
        return STATE.read_text(encoding="utf-8-sig").strip().lower() != "off"
    except Exception:  # noqa: BLE001
        return True


def set_wanted(value: bool) -> None:
    """记住「桥接该活着 / 该停着」。"""
    try:
        STATE.write_text("on" if value else "off", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 小工具

def is_admin() -> bool:
    """当前进程是不是管理员。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.6) -> bool:
    """端口上有没有东西在听。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_port(port: int, up: bool, timeout: float = 30.0) -> bool:
    """等端口开（up=True）或关（up=False）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(port) == up:
            return True
        time.sleep(0.4)
    return port_open(port) == up


def ps_pids(filter_name: str, like_cmd: str = "") -> List[int]:
    """按进程名（可选再按命令行关键字）找进程号。

    走 PowerShell 的 CIM 查询（wmic 在新版 Windows 上已经没了），但**必须**
    带 CREATE_NO_WINDOW：管家自己是隐藏进程，子进程默认会各建一个 console，
    桌面就一片黑框乱闪。而且这条只给「按命令行找」用，查名字有更快的原生路径。
    @param filter_name - 进程名，例如 python.exe。
    @param like_cmd - 命令行里要包含的片段，空表示不管。
    @returns 进程号列表。
    """
    script = (
        f"Get-CimInstance Win32_Process -Filter \"Name='{filter_name}'\" | "
        + (f"Where-Object {{ $_.CommandLine -like '*{like_cmd}*' }} | " if like_cmd else "")
        + "Select-Object -ExpandProperty ProcessId"
    )
    try:
        out = winproc.run_hidden(["powershell", "-NoProfile", "-Command", script], timeout=20)
    except Exception as exc:  # noqa: BLE001
        log.warning("查进程失败：%s", exc)
        return []
    pids: List[int] = []
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


#: 桥接进程号缓存：(取到的时间, pid 列表)。按命令行查要走 WMI，比较贵，
#: 状态接口又被前端每几秒轮询一次，不能每次都真查。
_BRIDGE_PIDS_CACHE: Dict[str, Any] = {"at": 0.0, "pids": []}
BRIDGE_PIDS_TTL = 8.0
#: 我们自己拉起的桥接把 pid 记在这，之后查状态只看这个文件 + 原生进程枚举，
#: 完全不用碰 WMI（WMI 查询虽然不弹窗了，但一次也要几百毫秒和不少 CPU）。
BRIDGE_PID_FILE = HERE / "bridge.pid"


def _read_pid_file() -> Optional[int]:
    try:
        text = BRIDGE_PID_FILE.read_text(encoding="utf-8").strip()
        return int(text) if text.isdigit() else None
    except Exception:  # noqa: BLE001
        return None


def _write_pid_file(pid: int) -> None:
    try:
        BRIDGE_PID_FILE.write_text(str(pid), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _clear_pid_file() -> None:
    try:
        BRIDGE_PID_FILE.unlink()
    except Exception:  # noqa: BLE001
        pass


def bridge_pids(refresh: bool = False) -> List[int]:
    """正在跑的桥接进程。

    顺序：缓存 → pid 文件 + 原生枚举 → （实在没有才）WMI 按命令行查。
    WMI 那条路只在「pid 文件丢了、桥接又是别的脚本起的」时走一次。
    """
    now = time.time()
    if not refresh and now - float(_BRIDGE_PIDS_CACHE["at"]) < BRIDGE_PIDS_TTL:
        return list(_BRIDGE_PIDS_CACHE["pids"])
    pids: List[int] = []
    recorded = _read_pid_file()
    if recorded is not None:
        # 原生枚举确认这个 pid 还活着、而且确实是 python：不用 WMI、不开子进程。
        if recorded in winproc.pids_by_name(["python.exe"]):
            pids = [recorded]
        else:
            _clear_pid_file()
    if not pids:
        pids = ps_pids("python.exe", "bridge.py")
        if pids:
            _write_pid_file(pids[0])
    _BRIDGE_PIDS_CACHE["at"] = now
    _BRIDGE_PIDS_CACHE["pids"] = pids
    return pids


def wechat_pids() -> List[int]:
    """正在跑的微信客户端（原生查询，不开子进程）。"""
    return winproc.pids_by_name(["Weixin.exe"])


def kill_pids(pids: List[int]) -> None:
    """按进程号强杀（含子进程）。"""
    for pid in pids:
        try:
            winproc.run_hidden(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=20)
        except Exception as exc:  # noqa: BLE001
            log.warning("杀进程 %s 失败：%s", pid, exc)


def python_exe() -> str:
    """用哪个 python 跑桥接。"""
    return sys.executable or "python"


# ---------------------------------------------------------------- 动作

def start_bridge() -> Dict[str, Any]:
    """起桥接。已经在了就什么都不做。"""
    set_wanted(True)
    running = bridge_pids()
    if running:
        return {"ok": True, "already": True, "pids": running}
    # 端口还被占着 = 老桥接没退干净。这时候再拉一个，新进程抢不到端口
    # 就会自己崩掉（10048），控制台连不上、看起来像「服务挂了」。
    if wait_port(BRIDGE_PORT, True, 1.0):
        pids = bridge_pids(refresh=True)
        log.info("8765 还占着（pids=%s），不重复起桥接", pids)
        return {"ok": True, "already": True, "pids": pids}
    cmd = [python_exe(), "-X", "utf8", "-u", "bridge.py",
           "--backend", "wechatauto", "--port", str(BRIDGE_PORT)]
    warning = ""
    if not is_admin():
        # 非管理员的桥接写不进 Weixin.dll 的激活字节，UIA 树起不来，
        # 发送退化成「坐标 + OCR」——窗口一动就废（实测就是这么卡死的）。
        warning = "进程管家不是管理员，拉起来的桥接发送会走不可靠的坐标+OCR；" \
                  "请改用 scripts\\start_admin.bat（会弹 UAC）"
        log.warning(warning)
    # 日志太大先转一下，免得 bridge.out.log 无限涨（实测一天近 1MB）。
    try:
        out_path = HERE / "bridge.out.log"
        if out_path.exists() and out_path.stat().st_size > 20 * 1024 * 1024:
            rotated = HERE / "bridge.out.log.1"
            if rotated.exists():
                rotated.unlink()
            out_path.replace(rotated)
            log.info("bridge.out.log 超过 20MB，已转成 bridge.out.log.1")
    except Exception as exc:  # noqa: BLE001
        log.debug("转日志失败（不影响起桥接）：%s", exc)
    out = open(HERE / "bridge.out.log", "ab", buffering=0)
    flags = 0
    if os.name == "nt":
        # 独立进程组：管家关了，桥接照样活着。
        flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    try:
        proc = subprocess.Popen(cmd, cwd=str(HERE), stdout=out, stderr=out,
                                stdin=subprocess.DEVNULL, creationflags=flags)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"起不来：{exc}"}
    log.info("已拉起桥接 pid=%s", proc.pid)
    _write_pid_file(proc.pid)
    winproc.clear_cache()
    bridge_pids(refresh=True)
    if not wait_port(BRIDGE_PORT, True, 40):
        return {"ok": False, "error": "桥接起了但 8765 端口没开，看 bridge.log"}
    result: Dict[str, Any] = {"ok": True, "pids": [proc.pid]}
    if warning:
        result["warning"] = warning
    return result


def stop_bridge() -> Dict[str, Any]:
    """停桥接。"""
    set_wanted(False)
    pids = bridge_pids(refresh=True)
    if not pids:
        return {"ok": True, "already": True, "pids": []}
    kill_pids(pids)
    wait_port(BRIDGE_PORT, False, 15)
    _clear_pid_file()
    winproc.clear_cache()
    bridge_pids(refresh=True)
    log.info("已停桥接 %s", pids)
    return {"ok": True, "pids": pids}


def restart_bridge() -> Dict[str, Any]:
    """重启桥接。"""
    stop_bridge()
    time.sleep(1.0)
    return start_bridge()


def open_wechat() -> Dict[str, Any]:
    """微信没开就开一个。"""
    running = wechat_pids()
    if running:
        return {"ok": True, "already": True, "pids": running}
    if not WEIXIN_EXE.exists():
        return {"ok": False, "error": f"找不到微信：{WEIXIN_EXE}"}
    try:
        proc = subprocess.Popen([str(WEIXIN_EXE)])
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"微信起不来：{exc}"}
    return {"ok": True, "pids": [proc.pid]}


ACTIONS = {
    "bridge/start": start_bridge,
    "bridge/stop": stop_bridge,
    "bridge/restart": restart_bridge,
    "wechat/open": open_wechat,
}


# ---------------------------------------------------------------- 看门狗

def ensure_hotkey() -> None:
    """保证紧急刹车（热键监听）在跑：它是个独立小进程，挂了就没刹车了。"""
    try:
        pids = ps_pids("python.exe", "panic_hotkey.py")
        if pids:
            return
        proc = subprocess.Popen(
            [python_exe(), "-X", "utf8", "-u", "panic_hotkey.py"],
            cwd=str(HERE), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) |
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        log.info("已拉起紧急刹车热键监听 pid=%s（Ctrl+Alt+X）", proc.pid)
    except Exception as exc:  # noqa: BLE001
        log.warning("拉起热键监听失败：%s", exc)


def watchdog_loop() -> None:
    """桥接没了就拉起来。

    桥接里有个「发送卡死」的保险丝（卡满 120 秒就硬退进程），退了通道就哑了。
    管家本来就常驻，顺手盯着桥接在不在。

    顺序很重要：**先探端口**（一个 socket 连接，几微秒、不建进程），端口开着就直接跳过。
    这样正常情况下永远不会去查 WMI——以前这里每 6 秒查一次，桌面上就一直闪黑框。
    """
    while True:
        time.sleep(6)
        try:
            if not wanted():
                continue
            if port_open(BRIDGE_PORT):
                continue
            # 端口没开，看看有没有进程还没起来（缓存 8 秒，不会猛查）
            if bridge_pids():
                continue
            log.warning("桥接不在了，重新拉起")
            start_bridge()
        except Exception as exc:  # noqa: BLE001
            log.warning("看门狗出错：%s", exc)


# ---------------------------------------------------------------- HTTP

class ControlHandler(BaseHTTPRequestHandler):
    """控制服务的接口。"""

    server_version = "wxbridge-control/1.0"

    #: 允许的来源：本机页面 + 命令行（无 Origin）。
    _ALLOWED_ORIGINS = (
        "http://127.0.0.1:8766",
        "http://localhost:8766",
        "http://127.0.0.1:8767",
        "http://localhost:8767",
    )

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        pass

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return origin in self._ALLOWED_ORIGINS

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin and origin in self._ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Vary", "Origin")

    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, data: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if not self._origin_ok():
            self._send_json({"error": "这个来源不让访问"}, 403)
            return
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            # 同一个控制台页面，住在 8767 这一份：桥接停了它还在，
            # 页面上照样能点「启动桥接」。
            try:
                page = (HERE / "web" / "index.html").read_bytes()
            except OSError:
                self._send_json({"error": "页面文件不在了"}, 500)
                return
            self._send_bytes(page, "text/html; charset=utf-8")
            return
        if path == "/api/control/status":
            self._send_json(self.status())
            return
        self._send_json({"error": "没有这个接口"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if not self._origin_ok():
            self._send_json({"error": "这个来源不让访问"}, 403)
            return
        path = urlparse(self.path).path
        name = path[len("/api/control/"):] if path.startswith("/api/control/") else ""
        action = ACTIONS.get(name)
        if action is None:
            self._send_json({"error": f"没有这个动作：{name}"}, 404)
            return
        try:
            result = action()
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": str(exc)}, 500)
            return
        self._send_json(result)

    def status(self) -> Dict[str, Any]:
        """现在谁在跑。"""
        bridges = bridge_pids()
        return {
            "ok": True,
            "bridge": {"running": bool(bridges), "pids": bridges,
                       "port": BRIDGE_PORT, "wanted": wanted()},
            "dashboard": {"running": port_open(DASHBOARD_PORT), "port": DASHBOARD_PORT},
            "wechat": {"running": bool(wechat_pids()), "pids": wechat_pids()},
            "admin": is_admin(),
            "port": self.server.server_address[1] if self.server else 0,
        }


class ExclusiveServer(ThreadingHTTPServer):
    """独占端口的管家服务。

    Windows 上 HTTPServer 默认 allow_reuse_address=1（SO_REUSEADDR），
    意思是「这端口被占了我也照绑」——于是能开出两个进程管家（实测 8767 上
    同时挂着 PID 22032 和 27552）。后果不是报错，是更坏的：两套看门狗同时
    数着同一个桥接，supervisor.log 里就会出现同一毫秒两行「桥接不在了，重新
    拉起」、拉起两个不同 pid 的桥接（后起的那个抢不到 8765 自己崩掉）；控制
    接口打到哪个管家也不确定，打到非管理员那个，拉起来的桥接写不了 UIA 激活
    字节，发送就退化成坐标+OCR，一卡一大片。
    所以这里关掉 reuse、开 SO_EXCLUSIVEADDRUSE：端口已经有管家，第二个就绑不上。
    """

    allow_reuse_address = False

    def server_bind(self) -> None:      # noqa: D102
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", -5)
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        except OSError:                 # 老/怪系统不支持，就靠 allow_reuse_address=0
            pass
        super().server_bind()


def main() -> None:
    parser = argparse.ArgumentParser(description="微信助手进程管家")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(LOG, encoding="utf-8")],
    )

    # 先抢端口，再干别的：抢不到说明已经有一个管家在跑，这个多开的实例
    # 立刻退出，免得它带着看门狗和热键一起抢活儿。
    if port_open(args.port):
        log.warning("端口 %d 已经有进程管家在听，这个实例退出（多开没用）", args.port)
        return
    try:
        httpd = ExclusiveServer((args.host, args.port), ControlHandler)
    except OSError as exc:
        log.warning("绑不上 %s:%d（%s），说明另一个管家刚抢到了，这个实例退出",
                    args.host, args.port, exc)
        return
    log.info("进程管家启动：http://%s:%d（管理员：%s）", args.host, args.port, is_admin())
    ensure_hotkey()
    threading.Thread(target=watchdog_loop, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
