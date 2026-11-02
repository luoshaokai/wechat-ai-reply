"""名单：谁能让它自动回复。带开关、带时间、可彻底删除。

光有一份 `allowlist.txt`（一行一个名字）不够用：想临时关掉某个人只能把他删
掉，下次要用又得重新输入。所以主状态改成一份带开关的文件：

    wxbridge/allowlist.json
    [{"name": "小明", "enabled": true, "added": 1793123456}]

同时继续维护 `allowlist.txt`（只写 enabled 的名字）——它是给外部应答端读的简
单镜像，那份文件一行都不用改格式；改完下一条消息就生效，不需要重启。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List

import appconfig

log = logging.getLogger("wxbridge.allowlist")

HERE = Path(__file__).resolve().parent
ENTRIES_FILE = HERE / "allowlist.json"
TXT_FILE = HERE / "allowlist.txt"

TXT_HEADER = (
    "# 私聊白名单（热改）：每行一个「界面里显示的名字」，改完下一条消息就生效。\n"
    "# 这份文件由控制台自动生成，别手改——要开关人请到「名单」页点开关。\n"
    "#\n"
    "# 文件传输助手只对桥接 --demo 联调有用：真人从自己发过去的消息不算「收到消息」。\n"
)


def _seed_from_legacy() -> List[Dict[str, Any]]:
    """第一次运行：把 config.yaml 里的名单（和手写的 allowlist.txt）搬过来。"""
    names: List[str] = []
    for name in appconfig.seed_names():
        if name and name not in names:
            names.append(name)
    try:
        for line in TXT_FILE.read_text(encoding="utf-8-sig").splitlines():
            name = line.strip()
            if not name or name.startswith("#") or name.startswith("-"):
                continue
            if name not in names:
                names.append(name)
    except OSError:
        pass
    return [{"name": n, "enabled": True, "added": int(time.time())} for n in names]


def load() -> List[Dict[str, Any]]:
    """读名单（文件坏了就从老 txt 重建，不炸）。"""
    try:
        raw = ENTRIES_FILE.read_text(encoding="utf-8-sig")
        data = json.loads(raw)
        if isinstance(data, list):
            entries: List[Dict[str, Any]] = []
            for item in data:
                if isinstance(item, str):
                    item = {"name": item, "enabled": True}
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                entries.append({
                    "name": name,
                    "enabled": bool(item.get("enabled", True)),
                    "added": int(item.get("added") or 0),
                })
            return entries
    except FileNotFoundError:
        entries = _seed_from_legacy()
        if entries:
            save(entries)
        return entries
    except Exception as exc:  # noqa: BLE001
        log.warning("读 allowlist.json 失败，改从 txt 重建：%s", exc)
        return _seed_from_legacy()
    return []


def save(entries: List[Dict[str, Any]]) -> None:
    """写 JSON，并把「已开启」的名字同步进 allowlist.txt（外部应答端读的就是它）。"""
    clean: List[Dict[str, Any]] = []
    for item in entries:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        clean.append({"name": name, "enabled": bool(item.get("enabled", True)),
                      "added": int(item.get("added") or time.time())})
    tmp = ENTRIES_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(ENTRIES_FILE)

    lines = [TXT_HEADER]
    for item in clean:
        if item["enabled"]:
            lines.append(item["name"])
    TXT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def enabled_names() -> List[str]:
    """真正生效的名单（外部应答端读 allowlist.txt，两边保持一致）。"""
    return [e["name"] for e in load() if e["enabled"]]


def add(name: str) -> List[Dict[str, Any]]:
    """加一个人；已经有就把开关打开（不重复添加）。"""
    name = str(name or "").strip()
    entries = load()
    if not name:
        return entries
    for item in entries:
        if item["name"] == name:
            item["enabled"] = True
            save(entries)
            return entries
    entries.append({"name": name, "enabled": True, "added": int(time.time())})
    save(entries)
    return entries


def remove(name: str) -> List[Dict[str, Any]]:
    """彻底删掉（连记录一起没）。"""
    entries = [e for e in load() if e["name"] != str(name or "").strip()]
    save(entries)
    return entries


def set_enabled(name: str, enabled: bool) -> List[Dict[str, Any]]:
    """开 / 关。关掉只是不回他，名字留着，下次点一下就能开。"""
    entries = load()
    for item in entries:
        if item["name"] == str(name or "").strip():
            item["enabled"] = bool(enabled)
    save(entries)
    return entries
