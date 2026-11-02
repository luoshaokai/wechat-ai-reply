"""一条命令管好整套服务：start / stop / restart / status / console / doctor / panic。

不用记端口，也不用碰 curl。scripts 目录里那几个 .bat 就是在调它::

    python wxbridge/ctl.py status
    python wxbridge/ctl.py doctor
    python wxbridge/ctl.py start

三个端口各管一件事：

    8765  桥接的 WebSocket（消息通道）
    8766  网页控制台（住在桥接进程里）
    8767  进程管家（常驻，桥接死了它也还在，能点「启动」）
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Dict, Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BRIDGE_PORT = 8765
DASHBOARD_PORT = 8766
CONTROL_PORT = 8767
CONTROL_BASE = f"http://127.0.0.1:{CONTROL_PORT}"
PAUSE_FILE = HERE / "PAUSE"

#: 发送库（wechatauto）被导入时会把 sys.stdout 换成它自己的 UTF-8 流，cmd 的
#: 936 代码页下从那以后的中文全成乱码。这里在还没 import 任何东西之前把启动
#: 时的流存下来，体检输出统一写回它 —— 编码由启动参数/环境变量决定，最稳。
_CONSOLE_OUT = sys.stdout


# ---------------------------------------------------------------- 小工具

# 都要 flush=True：发送库导入时会接管 sys.stdout，缓冲区里没写出去
# 的字会被丢掉——重定向到文件时，最前面几行日志就是这么消失的。
def _ok(text: str) -> None:
    print(f"[OK]   {text}", file=_CONSOLE_OUT, flush=True)


def _warn(text: str) -> None:
    print(f"[注意] {text}", file=_CONSOLE_OUT, flush=True)


def _bad(text: str) -> None:
    print(f"[不行] {text}", file=_CONSOLE_OUT, flush=True)


def port_open(port: int, timeout: float = 0.35) -> bool:
    """端口有没有人听着。"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def http_json(path: str, payload: Optional[Dict[str, Any]] = None,
              timeout: float = 8.0) -> Dict[str, Any]:
    """调管家的接口。payload 给了就是 POST。"""
    url = CONTROL_BASE + path
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ok": False, "error": f"调不通 {url}：{exc}"}


def spawn_supervisor() -> Dict[str, Any]:
    """管家不在就把他拉起来（独立进程，关掉这个命令行窗口也不影响）。"""
    if port_open(CONTROL_PORT):
        return {"ok": True, "already": True}
    log = ROOT / "wxbridge" / "supervisor.out.log"
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    try:
        with log.open("ab", buffering=0) as out:
            subprocess.Popen(
                [sys.executable, "-X", "utf8", "-u", "supervisor.py"],
                cwd=str(HERE), stdout=out, stderr=out,
                stdin=subprocess.DEVNULL, creationflags=flags,
            )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"管家起不来：{exc}"}
    for _ in range(30):
        time.sleep(0.3)
        if port_open(CONTROL_PORT):
            return {"ok": True}
    return {"ok": False, "error": "管家起来了但 8767 没在听，看看 wxbridge/supervisor.log"}


# ---------------------------------------------------------------- 命令

def cmd_status(_: argparse.Namespace) -> int:
    got = http_json("/api/control/status")
    if not got.get("ok", True) or "bridge" not in got:
        _bad(got.get("error") or "管家没在跑（先 ctl.py start）")
        return 1
    bridge = got.get("bridge", {})
    dash = got.get("dashboard", {})
    wechat = got.get("wechat", {})
    _ok(f"桥接：{'在跑' if bridge.get('running') else '没跑'}"
        f"（该活着={bridge.get('wanted')}，端口 {bridge.get('port')}）")
    _ok(f"控制台：{'能打开 http://127.0.0.1:%d' % DASHBOARD_PORT if dash.get('running') else '没开（桥接起来就有）'}")
    _ok(f"微信客户端：{'在跑' if wechat.get('running') else '没跑'}")
    _ok(f"管理员权限：{'是' if got.get('admin') else '否 —— 发送会走不可靠的坐标+OCR'}")
    _ok(f"紧急刹车：{'踩着（PAUSE 存在）' if PAUSE_FILE.exists() else '没踩'}")
    return 0


def cmd_start(_: argparse.Namespace) -> int:
    if not port_open(CONTROL_PORT):
        _warn("进程管家没在跑，先拉起管家…")
        got = spawn_supervisor()
        if not got.get("ok"):
            _bad(got.get("error", "拉不起来"))
            return 1
        _ok("管家已就绪（http://127.0.0.1:%d）" % CONTROL_PORT)
    got = http_json("/api/control/bridge/start", {})
    if not got.get("ok"):
        _bad(got.get("error", "启动失败"))
        return 1
    _ok("桥接已启动：消息通道 ws://127.0.0.1:%d，控制台 http://127.0.0.1:%d"
        % (BRIDGE_PORT, DASHBOARD_PORT))
    if got.get("warning"):
        _warn(str(got["warning"]))
    print(f"浏览器打开 http://127.0.0.1:{DASHBOARD_PORT}/ 就能看状态、改名单、改人设。", file=_CONSOLE_OUT, flush=True)
    return 0


def cmd_stop(_: argparse.Namespace) -> int:
    got = http_json("/api/control/bridge/stop", {})
    print(("桥接已停止。" if got.get("ok") else f"停止没成功：{got.get('error')}"), file=_CONSOLE_OUT, flush=True)
    # 顺手把管家也关掉：留着它过几秒又会把桥接拉起来（除非你刚在界面上点了停止）。
    import supervisor
    pids = supervisor.ps_pids("python.exe", "supervisor.py")
    if pids:
        supervisor.kill_pids(pids)
        print(f"进程管家已退出（{pids}）。", file=_CONSOLE_OUT, flush=True)
    return 0


def cmd_restart(_: argparse.Namespace) -> int:
    got = http_json("/api/control/bridge/restart", {})
    if got.get("ok"):
        _ok("桥接已重启，新配置生效。")
        return 0
    return cmd_start(argparse.Namespace())


def cmd_console(_: argparse.Namespace) -> int:
    if not port_open(DASHBOARD_PORT):
        _warn(f"控制台端口 {DASHBOARD_PORT} 没在听——桥接可能没跑，先 ctl.py start")
    webbrowser.open(f"http://127.0.0.1:{DASHBOARD_PORT}/")
    return 0


def cmd_panic(args: argparse.Namespace) -> int:
    if args.resume:
        if PAUSE_FILE.exists():
            PAUSE_FILE.unlink()
            _ok("紧急刹车已松开，恢复正常回复。")
        else:
            _warn("本来就没踩刹车。")
        return 0
    PAUSE_FILE.write_text("paused by ctl.py\n", encoding="utf-8")
    _ok("紧急刹车踩下了：立刻停止一切界面操作，不再发任何消息。")
    _warn("要恢复：python wxbridge/ctl.py panic --resume")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """不连微信，拿假消息把整条链路跑一遍（要在另一个窗口先看一眼效果）。"""
    _warn("mock 后端不碰微信，只验证「消息 → 大脑 → 回复」这条链路。")
    _warn(f"监听 {args.port}（跟正式服务的 8765 不冲突），Ctrl+C 退出。")
    os.execv(sys.executable, [sys.executable, "-X", "utf8", "-u",
                              str(HERE / "bridge.py"),
                              "--backend", "mock", "--demo",
                              "--port", str(args.port), "--http-port", "0"])


def cmd_doctor(_: argparse.Namespace) -> int:
    """体检：环境、依赖、配置、微信、权限、端口，一项项过。"""
    bad = 0

    ver = sys.version_info
    if (ver.major, ver.minor) >= (3, 9):
        _ok(f"Python {ver.major}.{ver.minor}.{ver.micro}（要求 3.9 以上）")
    else:
        _bad(f"Python {ver.major}.{ver.minor} 太老，装个 3.9+ 再说")
        bad += 1

    for mod, label in (("websockets", "websockets（消息通道）"),
                       ("wechatauto", "wechatauto（读微信库 + 操作界面）"),
                       ("uiautomation", "uiautomation"),
                       ("win32api", "pywin32")):
        try:
            __import__(mod)
            _ok(f"依赖齐全：{label}")
        except Exception as exc:  # noqa: BLE001
            _bad(f"缺依赖：{label} —— 跑 scripts\\install.bat（{exc}）")
            bad += 1

    cfg = ROOT / "config.yaml"
    if not cfg.exists():
        import appconfig
        appconfig.ensure_config()
        _warn(f"已经帮你从 config.example.yaml 复制出 {cfg}，去填模型和密钥")
    else:
        _ok(f"配置文件在：{cfg}")
    import appconfig
    import brain as brain_mod
    import brain_store
    # 大脑实际用哪个端点：控制台里地址和模型名都填齐了就用它，否则退回 config.yaml。
    # 只看 config.yaml 会把「在网页里配好的人」报成没配，白吓人一跳。
    console = brain_store.load()
    if brain_store.configured(console):
        llm, src = console, "控制台里填的（brain.json）"
    else:
        llm, src = appconfig.llm_settings(), "config.yaml 的 llm 段"
    if llm.get("baseURL") and llm.get("model"):
        _ok(f"模型：{llm.get('model')} @ {llm.get('baseURL')}　来源：{src}")
    else:
        _bad("模型地址和模型名没填齐（config.yaml 的 llm 段，或控制台「设置」页 —— 不填大脑不会说话）")
        bad += 1
    local = brain_mod._is_local_server(str(llm.get("baseURL") or ""))
    if llm.get("apiKey"):
        _ok("密钥已配（文件或环境变量 WXREPLY_API_KEY）")
    elif local:
        _ok("没密钥 —— 本地 / 局域网的推理服务（Ollama、LM Studio 这类）本来就不用填")
    else:
        _warn("没找到密钥：只有本地免密钥模型（如 Ollama）能这么跑，云端服务都得填")
    if appconfig.persona_text():
        _ok("人设已写")
    else:
        _warn("人设 persona 是空的，回复会不像你")

    names = appconfig.seed_names()
    _ok(f"名单：config.yaml 里给了 {len(names)} 个种子名字（真正生效的看 allowlist.json）")

    if os.name != "nt":
        _bad("这套东西只能Windows 上的微信客户端（依赖 UIA 和微信本地数据库）")
        bad += 1
        return 1 if bad else 0

    import supervisor
    if supervisor.is_admin():
        _ok("管理员权限：有（写不进 Weixin.dll 的话 UIA 树起不来，只能发得笨一点）")
    else:
        _warn("不是管理员：发送会退化成「坐标 + OCR」，微信窗口一挪就废。"
              "建议用 scripts\\start_admin.bat（会弹 UAC）")

    exe = supervisor.WEIXIN_EXE
    if supervisor.wechat_pids():
        _ok("微信客户端在跑")
    elif exe.exists():
        _bad(f"微信没开：{exe}（先登录微信再启动服务）")
        bad += 1
    else:
        _bad(f"找不到微信客户端：{exe}\n"
             "       要 Windows 版微信 4.1.x（PC 版），装好路径不一样就改 supervisor.py 里的 WEIXIN_EXE")
        bad += 1

    for port, label in ((BRIDGE_PORT, "消息通道"), (DASHBOARD_PORT, "控制台"),
                        (CONTROL_PORT, "进程管家")):
        _ok(f"端口 {port}（{label}）：{'已占用（说明在跑）' if port_open(port) else '空闲'}")

    print("", file=_CONSOLE_OUT, flush=True)
    if bad:
        _bad(f"体检有 {bad} 项不通过，按上面的提示补一下再 start。")
    else:
        _ok("体检通过，可以 scripts\\start.bat 了。")
    return 1 if bad else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="微信 AI 回复助手 · 命令入口")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("start", help="拉起管家 + 桥接").set_defaults(func=cmd_start)
    sub.add_parser("stop", help="停桥接并退出管家").set_defaults(func=cmd_stop)
    sub.add_parser("restart", help="重启桥接（改了要重启的配置用这个）").set_defaults(func=cmd_restart)
    sub.add_parser("status", help="看现在谁在跑").set_defaults(func=cmd_status)
    sub.add_parser("console", help="用默认浏览器打开控制台").set_defaults(func=cmd_console)
    sub.add_parser("doctor", help="体检：环境 / 依赖 / 配置 / 权限").set_defaults(func=cmd_doctor)
    d = sub.add_parser("demo", help="不连微信，假消息跑一遍链路")
    d.add_argument("--port", type=int, default=9765, help="临时通道端口（默认 9765，避开正式服务）")
    d.set_defaults(func=cmd_demo)
    p = sub.add_parser("panic", help="紧急刹车：立刻停止一切界面操作")
    p.add_argument("--resume", action="store_true", help="松开刹车")
    p.set_defaults(func=cmd_panic)
    args = parser.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
