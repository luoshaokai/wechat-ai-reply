"""桥接自带的网页控制台。

桥接进程（8765 的 WebSocket）旁边再开一个 HTTP 小站（默认 8766），
浏览器打开 http://127.0.0.1:8766/ 就能看状态、改名单、查最近消息、手动补一句。

它跟桥接同一个进程，所以状态最准；进程管家（8767）再把同一个页面发一遍，
桥接死了也还能点「启动」。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import appconfig
import allowlist_store
import brain_store
import pending_store
import style_store

HERE = Path(__file__).resolve().parent
WEB_DIR = HERE / "web"
ALLOWLIST_FILE = HERE / "allowlist.txt"
STYLE_FILE = HERE / "style.json"
BRIDGE_LOG = HERE / "bridge.log"
OUT_LOG = HERE / "bridge.out.log"


# ---------------------------------------------------------------- 读文件

def tail_text(path: Path, limit: int = 100) -> List[str]:
    """读文件尾巴（最多看最后 200KB，别把大日志整个读进内存）。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > 200_000:
                fh.seek(size - 200_000)
            data = fh.read()
    except OSError:
        return []
    text = data.decode("utf-8", "replace")
    lines = text.splitlines()
    return lines[-limit:] if limit > 0 else lines


def read_allowlist_file() -> "tuple[List[str], List[str]]":
    """读 txt 名单，返回 (加进名单的, 要移除的)。行首 ``-`` 表示移除。"""
    names: List[str] = []
    removes: List[str] = []
    try:
        for line in ALLOWLIST_FILE.read_text(encoding="utf-8-sig").splitlines():
            item = line.strip()
            if not item or item.startswith("#"):
                continue
            if item.startswith("-"):
                removes.append(item[1:].strip())
            else:
                names.append(item)
    except OSError:
        pass
    return names, removes


def write_allowlist_file(names: List[str]) -> None:
    """把名单写回 txt（保留头部说明，去重）。"""
    seen: List[str] = []
    for item in names:
        item = str(item).strip()
        if item and item not in seen:
            seen.append(item)
    body = allowlist_store.TXT_HEADER + "\n".join(seen) + ("\n" if seen else "")
    ALLOWLIST_FILE.write_text(body, encoding="utf-8")


def read_app_config() -> Dict[str, Any]:
    """要显示、要生效的「热配置」，全部来自 config.yaml（改了不用重启）。"""
    limits = appconfig.limits()
    respond = appconfig.respond()
    return {
        "allowChats": appconfig.seed_names(),
        "minIntervalSeconds": _as_int(limits.get("minIntervalSeconds"), 10),
        "dailyMaxPerChat": _as_int(limits.get("dailyMaxPerChat"), 0),
        "dailyMaxTotal": _as_int(limits.get("dailyMaxTotal"), 0),
        "requireMention": bool(respond.get("groupsNeedAt", True)),
        "allowGroups": bool(respond.get("allowGroups", True)),
    }


def _as_int(value: Any, fallback: int) -> int:
    """配置里写歪了也不炸：能转整数就转，不能就用默认值。"""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return fallback


def effective_allowlist() -> List[str]:
    """真正生效的名单：以 allowlist.json 里「已开启」的人为准。

    config.yaml 里的 allowChats 只在第一次运行时当种子（在那儿写名字关不掉人），
    之后由 allowlist_store 从 JSON 生成 allowlist.txt，所以只有这一个来源。
    """
    try:
        return allowlist_store.enabled_names()
    except Exception:  # noqa: BLE001 - 读不到就退回 config 里的种子
        return list(read_app_config().get("allowChats") or [])


def recent_problems(limit: int = 30) -> List[Dict[str, Any]]:
    """最近的异常/未回事件，给「首页 → 最近异常」用。

    直接读桥接日志里带 WARNING / ERROR 的行，不改桥接代码，也不编数据。
    「应答端没报身份」这类是应答端重连时的正常噪音，滤掉。
    """
    noise = ("没报身份", "只有", "检测到", "旧版本", "不在本地配置里")
    keys = ("WARNING", "ERROR", "未回", "没发出去", "接手")
    out: List[Dict[str, Any]] = []
    seen = set()
    for path, tag in ((OUT_LOG, "通道"),):
        for line in tail_text(path, 400):
            text = line.strip()
            if not text or any(n in text for n in noise):
                continue
            if not any(k in text for k in keys):
                continue
            if text in seen:
                continue
            seen.add(text)
            out.append({"src": tag, "line": text})
    return out[-limit:][::-1]


def _wechat_running() -> bool:
    """微信客户端在不在跑（原生枚举，不起子进程、不闪黑框）。"""
    try:
        import winproc
        return bool(winproc.pids_by_name(["Weixin.exe"]))
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- HTTP

class DashboardHandler(BaseHTTPRequestHandler):
    """控制台的接口。"""

    server_version = "ddchat/1.0"
    bridge: Any = None
    on_send: Optional[Callable[[str, str], bool]] = None

    _ALLOWED_ORIGINS = (
        "http://127.0.0.1:8766", "http://localhost:8766",
        "http://127.0.0.1:8767", "http://localhost:8767",
        # 本机页面的端口每次启动都可能变，所以按本地回环整段放行
    )

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        pass

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True  # 命令行工具没有 Origin，放行
        if origin in self._ALLOWED_ORIGINS:
            return True
        # 本机页面的端口不固定，来自本地回环就放行
        return origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:")

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin and self._origin_ok():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Vary", "Origin")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self._send_json_inner(data, status)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # 长轮询的时候用户关了页面/切走了，连接早断了，这是正常现象，
            # 别在日志里刷一屏 traceback。
            pass

    def _send_json_inner(self, data: bytes, status: int) -> None:
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

    def _read_json(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    # ---------------------------------------------------------------- GET

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._origin_ok():
            self._send_json({"error": "这个来源不让访问"}, 403)
            return
        if path in ("/", "/index.html"):
            try:
                self._send_bytes((WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self._send_json({"error": "页面文件不在了"}, 500)
            return
        if path == "/api/status":
            # 长轮询：带上 since=<上次的 rev>&wait=20，没变化就先挂住，
            # 一有新消息（或「正在想」状态变化）立刻返回——控制台就不用等
            # 4 秒一轮了，体感是「实时」。
            q = parse_qs(parsed.query)
            try:
                since = int(q.get("since", ["-1"])[0])
            except ValueError:
                since = -1
            try:
                wait = float(q.get("wait", ["0"])[0])
            except ValueError:
                wait = 0.0
            bridge = self.bridge
            if wait > 0 and since >= 0 and bridge is not None and hasattr(bridge, "rev_cond"):
                with bridge.rev_cond:
                    if bridge.rev == since:
                        bridge.rev_cond.wait(timeout=min(wait, 25.0))
            self._send_json(self._status())
            return
        if path == "/api/health":
            self._send_json(self._health())
            return
        if path == "/api/allowlist":
            self._send_json({"entries": allowlist_store.load(),
                             "names": allowlist_store.enabled_names()})
            return
        if path == "/api/style":
            self._send_json(style_store.snapshot())
            return
        if path == "/api/brain":
            brain = getattr(self.bridge, "brain", None) if self.bridge is not None else None
            cloud = {}
            if brain is not None:
                try:
                    brain._load()
                    cloud = dict(getattr(brain, "_llm", {}) or {})
                except Exception:  # noqa: BLE001
                    cloud = {}
            self._send_json(brain_store.snapshot(cloud))
            return
        if path == "/api/pending":
            self._send_json({"items": pending_store.list_items(), "count": pending_store.count(),
                             "paused": (HERE / "PAUSE").exists()})
            return
        if path == "/api/events":
            self._send_json({"events": recent_problems()})
            return
        if path == "/api/logs":
            q = parse_qs(parsed.query)
            try:
                limit = max(10, min(500, int(q.get("tail", ["80"])[0])))
            except ValueError:
                limit = 80
            self._send_json({"lines": [f"[通道] {l}" for l in tail_text(OUT_LOG, limit)]})
            return
        self._send_json({"error": "没有这个接口"}, 404)

    # ---------------------------------------------------------------- POST

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not self._origin_ok():
            self._send_json({"error": "这个来源不让访问"}, 403)
            return
        body = self._read_json()
        if path == "/api/allowlist":
            toggle = body.get("toggle")
            if isinstance(toggle, dict):
                allowlist_store.set_enabled(str(toggle.get("name", "")), bool(toggle.get("enabled")))
            for name in body.get("add", []) or []:
                allowlist_store.add(str(name))
            for name in body.get("remove", []) or []:
                allowlist_store.remove(str(name))
            self._send_json({"ok": True,
                             "entries": allowlist_store.load(),
                             "names": allowlist_store.enabled_names()})
            return
        if path == "/api/style":
            text = body.get("text")
            if text is None:
                pid = str(body.get("preset", ""))
                preset = next((p for p in style_store.PRESETS if p["id"] == pid),
                              style_store.PRESETS[0])
                custom = str(body.get("custom", "")).strip()
                text = preset["text"] + ("\n补充要求：" + custom if custom else "")
            style_store.save(str(text))
            self._send_json({"ok": True, "style": style_store.snapshot()})
            return
        if path == "/api/style/preview":
            self._send_json(self._preview(str(body.get("message", "") or "在吗？晚上吃啥"),
                                          body.get("text")))
            return
        if path == "/api/brain":
            try:
                brain_store.save(str(body.get("api", "openai")),
                                 str(body.get("baseURL", "")),
                                 str(body.get("model", "")),
                                 str(body.get("apiKey", "")))
            except Exception as exc:  # noqa: BLE001 - 保存失败也要说清原因
                self._send_json({"ok": False, "error": f"保存失败：{exc}"}, 500)
                return
            self._send_json({"ok": True, "brain": self._brain_snapshot()})
            return
        if path == "/api/brain/test":
            self._send_json(self._test_brain(body))
            return
        if path == "/api/pause":
            on = bool(body.get("on", True))
            flag = HERE / "PAUSE"
            try:
                if on:
                    flag.write_text(str(int(time.time())), encoding="utf-8")
                elif flag.exists():
                    flag.unlink()
            except Exception as exc:  # noqa: BLE001
                self._send_json({"ok": False, "error": str(exc)}, 500)
                return
            self._send_json({"ok": True, "paused": on})
            return
        if path == "/api/pending/flush":
            self._send_json(self._flush_pending())
            return
        if path == "/api/pending/clear":
            pending_store.clear()
            self._send_json({"ok": True, "count": 0})
            return
        if path == "/api/reply":
            self._send_json(self._reply_now(str(body.get("chat", "")).strip(),
                                            str(body.get("text", "") or "").strip()))
            return
        if path == "/api/send":
            chat = str(body.get("chat", "")).strip()
            text = str(body.get("text", "")).strip()
            if not chat or not text:
                self._send_json({"ok": False, "error": "会话和内容都要填"}, 400)
                return
            try:
                ok = bool(self.on_send(chat, text)) if self.on_send else False
            except Exception as exc:  # noqa: BLE001
                self._send_json({"ok": False, "error": str(exc)}, 500)
                return
            self._send_json({"ok": ok})
            return
        self._send_json({"error": "没有这个接口"}, 404)

    # ---------------------------------------------------------------- 数据

    def _flush_pending(self) -> Dict[str, Any]:
        """把待发送队列里的东西逐条补发（暂停中不发）。"""
        if (HERE / "PAUSE").exists():
            return {"ok": False, "error": "现在处于暂停状态，先点「恢复」再补发"}
        bridge = self.bridge
        if bridge is None:
            return {"ok": False, "error": "桥接没起"}
        sent, failed = [], []
        for item in pending_store.list_items():
            chat = str(item.get("chat") or "")
            text = str(item.get("text") or "")
            if not chat or not text:
                pending_store.remove(chat, text)
                continue
            try:
                ok = bool(bridge.backend.send(chat, text))
            except Exception as exc:  # noqa: BLE001
                failed.append({"chat": chat, "text": text, "error": str(exc)[:80]})
                continue
            if ok:
                bridge._record("out", chat, text)
                pending_store.remove(chat, text)
                sent.append({"chat": chat, "text": text})
            else:
                failed.append({"chat": chat, "text": text, "error": "没发出去"})
        return {"ok": not failed, "sent": sent, "failed": failed,
                "count": pending_store.count()}

    def _reply_now(self, chat: str, text: str = "") -> Dict[str, Any]:
        """补回一条：拿这个会话最后一条「收到」的话，让大脑生成并立刻发出去。

        用途：桥接崩过、锁过屏、那条回复丢了的时候，不用你手打，点一下就补上。
        """
        bridge = self.bridge
        if bridge is None:
            return {"ok": False, "error": "桥接没起"}
        if not chat:
            return {"ok": False, "error": "要指定会话"}
        if not text:
            last = None
            for entry in reversed(getattr(bridge, "history", [])):
                if entry.get("chat") == chat and entry.get("dir") == "in":
                    last = entry
                    break
            if last is None:
                return {"ok": False, "error": "这个会话没有收到过消息"}
            text = str(last.get("text") or "")
        brain = getattr(bridge, "brain", None)
        if brain is None or not hasattr(brain, "reply_parts"):
            return {"ok": False, "error": "大脑不可用"}
        try:
            parts = brain.reply_parts(chat, text)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"生成失败：{exc}"}
        if not parts:
            err = str(getattr(brain, "last_error", "") or "")[:120]
            return {"ok": False, "error": f"模型没给出答案 {err}".strip()}
        sent = []
        for part in parts[:2]:
            try:
                ok = bool(bridge.backend.send(chat, part))
            except Exception as exc:  # noqa: BLE001
                sent.append({"text": part, "ok": False, "error": str(exc)[:80]})
                break
            bridge._record("out", chat, part)
            sent.append({"text": part, "ok": ok})
            if not ok:
                break
        return {"ok": all(item.get("ok") for item in sent), "sent": sent, "prompt": text[:40]}

    def _brain_snapshot(self) -> Dict[str, Any]:
        """大脑配置 + 云端实际用的是哪个。"""
        brain = getattr(self.bridge, "brain", None) if self.bridge is not None else None
        cloud: Dict[str, Any] = {}
        if brain is not None:
            try:
                brain._load()
                cloud = dict(getattr(brain, "_llm", {}) or {})
            except Exception:  # noqa: BLE001
                cloud = {}
        return brain_store.snapshot(cloud)

    def _test_brain(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """按页面上这组（还没保存的）参数真调一次模型，试通不通。

        以前只测「已经保存过的那份」：用户填完地址点测试，测的却是上一条配置，
        于是一个跟填写内容毫无关系的失败 —— 这正是「连接测试失败」最常见的来源。
        """
        brain = getattr(self.bridge, "brain", None) if self.bridge is not None else None
        if brain is None:
            # 桥接以 --brain off 起的话没有常驻大脑，但测试照样得能做。
            try:
                import brain as brain_mod
                brain = brain_mod.Brain()
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"大脑模块起不来：{exc}"}
        started = time.time()
        try:
            out = brain.probe(api=str(body.get("api", "") or ""),
                              baseURL=str(body.get("baseURL", "") or ""),
                              model=str(body.get("model", "") or ""),
                              apiKey=str(body.get("apiKey", "") or ""))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"调用失败：{exc}"}
        out["elapsed"] = round(time.time() - started, 1)
        return out

    def _preview(self, message: str, text: Optional[str] = None) -> Dict[str, Any]:
        """拿当前风格（或传进来的未保存文本）试回一句。不发出去、不进记录。"""
        bridge = self.bridge
        brain = getattr(bridge, "brain", None) if bridge is not None else None
        if brain is None:
            return {"ok": False, "error": "没开本地大脑，预览不了（外部应答端的模型照样按这个风格回）"}
        try:
            if not brain.available():
                return {"ok": False, "error": "本地大脑缺配置（人设 / 端点 / 密钥）"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"本地大脑起不来：{exc}"}
        sample = str(message or "").strip()[:200] or "在吗？晚上吃啥"
        history = getattr(brain, "_history", None)
        try:
            if isinstance(history, dict):
                history.pop("__preview__", None)
            reply = brain.reply("__preview__", sample, style=str(text) if text else None)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"生成失败：{exc}"}
        finally:
            if isinstance(history, dict):
                history.pop("__preview__", None)
        if not reply:
            return {"ok": False, "error": "模型没给答案（可能没网、或者额度用完了）"}
        return {"ok": True, "reply": reply, "style": "当前风格"}

    def _status(self) -> Dict[str, Any]:
        bridge = self.bridge
        cfg = read_app_config()
        plugin: Dict[str, Any] = {"connected": False, "version": "", "pid": None}
        account = ""
        backend = ""
        recent: List[Dict[str, Any]] = []
        if bridge is not None:
            try:
                account = bridge.backend.account() or ""
                backend = bridge.backend.name
            except Exception:  # noqa: BLE001
                pass
            recent = list(getattr(bridge, "history", []))
            identified = getattr(bridge, "_identified", {}) or {}
            if identified:
                newest = list(identified.values())[-1]
                plugin = {"connected": True,
                          "version": str(newest.get("version", "")),
                          "pid": newest.get("pid")}
        brain = getattr(bridge, "brain", None) if bridge is not None else None
        ep_model = ""
        ep_local = False
        try:
            if brain is not None:
                _ep = brain._endpoint()
                ep_model = str(_ep.get("model") or "")
                ep_local = bool(_ep.get("local"))
        except Exception:  # noqa: BLE001
            pass
        thinking: List[Dict[str, Any]] = []
        skips: List[Dict[str, Any]] = []
        if bridge is not None:
            now = time.time()
            for chat, since in (getattr(bridge, "_thinking", {}) or {}).items():
                if now - float(since) < 180:
                    thinking.append({"chat": chat, "since": since})
            skips = list(getattr(bridge, "_skips", []))[-20:]
        return {
            "account": account,
            "backend": backend,
            "wechat": bool(account) or _wechat_running(),
            "bridge": True,
            "plugin": plugin,
            "brain": {"ready": brain is not None, "model": ep_model, "local": ep_local},
            "thinking": thinking,
            "skips": skips,
            "allowlist": effective_allowlist(),
            "allowlistEntries": allowlist_store.load(),
            "allowlistFile": str(ALLOWLIST_FILE),
            "limits": {
                "minIntervalSeconds": cfg["minIntervalSeconds"],
                "dailyMaxPerChat": cfg["dailyMaxPerChat"],
                "dailyMaxTotal": cfg["dailyMaxTotal"],
                "requireMention": cfg["requireMention"],
                "allowGroups": cfg["allowGroups"],
            },
            "recent": recent[-120:],
            "rev": int(getattr(bridge, "rev", 0)) if bridge is not None else 0,
            "now": time.time(),
        }

    def _health(self) -> Dict[str, Any]:
        """体检：一条一条告诉你「现在能不能回消息」，不能的话差在哪。"""
        status = self._status()
        checks: List[Dict[str, Any]] = []

        def add(name: str, ok: bool, detail: str, hint: str = "", level: str = "fail") -> None:
            checks.append({"name": name, "ok": ok,
                           "level": "ok" if ok else level,
                           "detail": detail, "hint": hint})

        wechat = _wechat_running()
        add("微信", wechat, "在跑" if wechat else "没开",
            "先登录电脑微信，再双击 scripts\\start.bat；或直接点「打开微信」")
        account = status["account"]
        add("读消息", bool(account), f"已连上 {account}" if account else "还没解密出来",
            "等十几秒；一直不行就重启（要管理员）")
        names = status["allowlist"]
        try:
            off = [e["name"] for e in allowlist_store.load() if not e["enabled"]]
        except Exception:  # noqa: BLE001
            off = []
        detail = f"{len(names)} 人开启"
        if off:
            detail += f"，{len(off)} 人关着（{'、'.join(off[:3])}{'…' if len(off) > 3 else ''}）"
        add("白名单", bool(names), detail if names else "空名单，谁都不回",
            "在「名单」页加人 / 打开开关，名字要跟微信里显示的一模一样",
            level="warn" if off else "fail")
        brain = getattr(self.bridge, "brain", None) if self.bridge is not None else None
        ep: Dict[str, Any] = {}
        raw: Dict[str, Any] = {}
        try:
            if brain is not None:
                brain._load()
                raw = dict(brain._endpoint())
                if brain.available():
                    ep = raw
        except Exception:  # noqa: BLE001
            ep = {}
            raw = {}
        if ep:
            where = "本地" if ep.get("local") else "云端"
            add("回复大脑", True, f"{ep.get('model')}　{where}",
                "在「设置」页换模型，改完下一条消息生效")
        else:
            miss = []
            if not raw.get("baseURL"):
                miss.append("地址（baseURL）")
            if not raw.get("model"):
                miss.append("模型名")
            if not (brain is not None and getattr(brain, "_persona", "")):
                miss.append("人设 / 风格")
            if raw.get("baseURL") and raw.get("model") \
                    and not raw.get("key") and not raw.get("local"):
                miss.append("密钥（云端地址必须填）")
            add("回复大脑", False,
                ("还没填：" + "、".join(miss)) if miss else "模型没配好",
                "去「设置」页填好再点「测试连接」；Ollama / LM Studio 这类本地服务不用填密钥")
        backend = self.bridge.backend if self.bridge is not None else None
        gui_ok = bool(getattr(backend, "_wx", None))
        stale = False
        try:
            if backend is not None and hasattr(backend, "_ui_stale"):
                stale = bool(backend._ui_stale())
        except Exception:  # noqa: BLE001
            stale = False
        if gui_ok and stale:
            add("发消息", False, "绑定的微信窗口已经失效（微信重启过）",
                "发下一条消息时会自动重连；一直不行就点右上「停止」再「启动」", level="warn")
        else:
            add("发消息", gui_ok, "微信界面已挂上" if gui_ok else "还没挂上微信窗口",
                "微信窗口别最小化、别关；要管理员权限", level="warn")
        pending = len(status["thinking"])
        add("正在回复", True, f"{pending} 个会话正在等答案" if pending else "没有在等的", "")
        failed = [c for c in checks if not c["ok"]]
        return {"ok": not failed, "checks": checks, "account": status["account"],
                "at": time.time()}


def start_dashboard(bridge: Any, port: int = 8766, host: str = "127.0.0.1",
                    on_send: Optional[Callable[[str, str], bool]] = None) -> Optional[ThreadingHTTPServer]:
    """起控制台。端口被占就返回 None（不挡桥接启动）。"""
    if not port:
        return None
    handler = type("BoundDashboardHandler", (DashboardHandler,), {"bridge": bridge, "on_send": on_send})
    try:
        httpd = ThreadingHTTPServer((host, port), handler)
    except OSError as exc:
        import logging
        logging.getLogger("wxbridge").warning("控制台起不来（%s）：%s", port, exc)
        return None
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd
