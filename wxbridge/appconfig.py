"""自带配置：读项目根目录的 ``config.yaml``，不依赖任何外部程序。

为什么要有这一层：整套代聊原来把「人设 / 模型 / 密钥 / 限速 / 名单」放在别的
App 的配置文件里，别人拿到代码就没处读。现在全部收到一个 ``config.yaml``：

- ``config.example.yaml`` 进仓库（进 git）；
- ``config.yaml`` 是你自己的（.gitignore 已排除，不会被提交）；
- 第一次运行如果 ``config.yaml`` 不在，会自动从 example 复制一份，改完就能跑。

限速、发送延迟这些是「热配置」：改完保存，下一条消息就生效，不用重启。
人设和模型这两块是「启动时读」，改了要重启桥接（控制台里改是即时生效的，
因为它写的是运行时存档 ``style.json`` / ``brain.json``，优先级更高）。

只支持这个 schema 用得上的 YAML 子集，所以不需要装 pyyaml（装了也照样能用
pyyaml 解析，写法不变）。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("wxbridge.config")

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE.parent / "config.yaml"
EXAMPLE_FILE = HERE.parent / "config.example.yaml"

#: 名单以外的默认值。缺项就用这些，绝不因为配置不全而崩。
DEFAULTS: Dict[str, Any] = {
    "llm": {"baseURL": "", "api": "openai", "model": "", "apiKey": ""},
    "persona": "",
    "limits": {"minIntervalSeconds": 10, "dailyMaxPerChat": 0, "dailyMaxTotal": 0},
    "respond": {"allowChats": [], "allowGroups": True, "groupsNeedAt": True},
    "send": {"delaySeconds": (3.0, 8.0), "maxAgeSeconds": 120, "dockRight": True,
             "cooloffSeconds": 30},
}

_loaded: Optional[Dict[str, Any]] = None
_stamp: Tuple[float, float] = (-1.0, -1.0)

_KEY_RE = re.compile(r"^([A-Za-z0-9_.\-]+)\s*:\s*(.*)$")


def _scalar(raw: str) -> Any:
    """把 ``1`` / ``\"abc\"`` / ``true`` / ``[a, b]`` 这种值转成 Python 对象。"""
    text = raw.strip()
    if not text or text.startswith("#"):
        return ""
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_scalar(x) for x in inner.split(",")]
    if len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
        return text[1:-1]
    low = text.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d*\.\d+", text):
        return float(text)
    return text


def _strip_comment(line: str) -> str:
    """去掉行尾注释，但不动引号里的 #。"""
    out, quote = [], ""
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out).rstrip()


def _parse(text: str) -> Dict[str, Any]:
    """极小 YAML 子集：两段缩进的 `key: value`、块列表、`|` 多行文本。"""
    data: Dict[str, Any] = {}
    section: Optional[str] = None
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    index = 0
    while index < len(lines):
        raw = lines[index]
        index += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        line = _strip_comment(raw)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        body = line.strip()
        found = _KEY_RE.match(body)
        if indent == 0:
            section = None
            if not found:
                continue
            key, rest = found.group(1), found.group(2)
            if rest.strip() in ("|", ">", "|-", ">-"):
                block: List[str] = []
                while index < len(lines):
                    nxt = lines[index]
                    if nxt.strip() and len(nxt) - len(nxt.lstrip()) == 0:
                        break
                    block.append(nxt[2:] if nxt.startswith("  ") else nxt)
                    index += 1
                join = "\n" if rest.strip().startswith("|") else " "
                data[key] = join.join(block).strip("\n")
                if join == " ":
                    data[key] = re.sub(r"\s*\n\s*", " ", str(data[key])).strip()
            elif rest.strip() in ("", "[]", "{}"):
                if rest.strip() == "{}":
                    data[key] = {}
                    section = None
                elif rest.strip() == "[]":
                    data[key] = []
                    section = None
                else:
                    # 空的顶层键一定是「段」，后面的缩进键都往这个 dict 里放
                    data[key] = {}
                    section = key
            else:
                data[key] = _scalar(rest)
            continue
        if not found:
            continue
        key, rest = found.group(1), found.group(2)
        target = data.setdefault(section or "_", {})
        if not isinstance(target, dict):
            continue
        if rest.strip() in ("|", ">", "|-", ">-"):
            block = []
            base = indent
            while index < len(lines):
                nxt = lines[index]
                if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= base:
                    break
                block.append(nxt[base + 2:] if len(nxt) > base + 1 else nxt.strip())
                index += 1
            target[key] = ("\n" if rest.strip().startswith("|") else " ").join(block).strip("\n")
            continue
        if rest.strip() == "":
            target[key] = []
            # 后面可能跟块列表（- 项）或更深一层键
            items: List[Any] = []
            sub: Dict[str, Any] = {}
            while index < len(lines):
                nxt = _strip_comment(lines[index])
                if not nxt.strip():
                    index += 1
                    continue
                nind = len(nxt) - len(nxt.lstrip())
                if nind <= indent:
                    break
                content = nxt.strip()
                if content.startswith("- "):
                    items.append(_scalar(content[2:]))
                    index += 1
                    continue
                mf = _KEY_RE.match(content)
                if mf:
                    sub[mf.group(1)] = _scalar(mf.group(2))
                    index += 1
                    continue
                break
            target[key] = sub if (sub and not items) else items
            continue
        target[key] = _scalar(rest)
    return data


def _load_text(path: Path) -> Optional[str]:
    """读文件，兼容记事本/PowerShell 存出来的 BOM。"""
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="gbk")
        except Exception:  # noqa: BLE001
            return None
    except OSError:
        return None


def ensure_config() -> Path:
    """没有 config.yaml 就从 example 复制一份，并提示用户去填。"""
    if CONFIG_FILE.exists():
        return CONFIG_FILE
    if EXAMPLE_FILE.exists():
        try:
            shutil.copyfile(EXAMPLE_FILE, CONFIG_FILE)
            log.info("已生成 config.yaml（从 config.example.yaml 复制），请填模型和密钥")
        except OSError as exc:  # noqa: BLE001
            log.warning("复制 config.yaml 失败：%s", exc)
    else:
        log.warning("没有 config.example.yaml，也没有 config.yaml，全部用默认值")
    return CONFIG_FILE


def _merge(base: Dict[str, Any], got: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in base.items():
        raw = got.get(key)
        if isinstance(value, dict) and isinstance(raw, dict):
            merged = dict(value)
            for k2, v2 in raw.items():
                merged[k2] = v2
            out[key] = merged
        elif raw is None or raw == "" or isinstance(raw, list) and not raw:
            out[key] = value
        else:
            out[key] = raw
    for key, value in got.items():
        out.setdefault(key, value)
    return out


def load(force: bool = False) -> Dict[str, Any]:
    """读配置（按文件修改时间热重载）。永远返回一个可用的 dict。"""
    global _loaded, _stamp
    try:
        mtime = CONFIG_FILE.stat().st_mtime if CONFIG_FILE.exists() else -1.0
    except OSError:
        mtime = -1.0
    if not force and _loaded is not None and _stamp[0] == mtime:
        return _loaded
    text = _load_text(CONFIG_FILE) if mtime > 0 else None
    parsed = _parse(text) if text else {}
    _loaded = _merge(DEFAULTS, parsed or {})
    _stamp = (mtime, 0.0)
    return _loaded


def persona_text() -> str:
    return str(load().get("persona") or "").strip()


def llm_settings() -> Dict[str, Any]:
    got = dict(load().get("llm") or {})
    # 环境变量优先：不想把密钥写进文件的人 export WXREPLY_API_KEY 就行
    env_key = os.environ.get("WXREPLY_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if env_key:
        got["apiKey"] = env_key
    return got


def limits() -> Dict[str, Any]:
    return dict(load().get("limits") or {})


def respond() -> Dict[str, Any]:
    return dict(load().get("respond") or {})


def send_options() -> Dict[str, Any]:
    return dict(load().get("send") or {})


def seed_names() -> List[str]:
    out = []
    for name in (respond().get("allowChats") or []):
        text = str(name).strip()
        if text and text not in out:
            out.append(text)
    return out
