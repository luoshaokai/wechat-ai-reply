"""待发送队列：没发出去的回复先存这里，等方便的时候再补发。

什么时候会进来：
- 紧急刹车踩着（PAUSE 文件在）；
- 微信无响应 / 锁屏 / 名字定位不到 / 发送超时；
- 发给对方的正文已经生成好了，只是没送出去。

存成 JSONL（一行一条），重启也不丢。补发成功后从文件里删掉。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger("wxbridge.pending")

HERE = Path(__file__).resolve().parent
QUEUE_FILE = HERE / "pending.jsonl"
#: 最多留这么多条，防止一直失败把文件撑爆。
MAX_ITEMS = 200


def _load_raw() -> List[Dict[str, Any]]:
    if not QUEUE_FILE.exists():
        return []
    items: List[Dict[str, Any]] = []
    try:
        for line in QUEUE_FILE.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(item, dict) and item.get("chat") and item.get("text"):
                items.append(item)
    except OSError:
        pass
    return items


def _save(items: List[Dict[str, Any]]) -> None:
    tmp = QUEUE_FILE.with_suffix(".jsonl.tmp")
    lines = [json.dumps(i, ensure_ascii=False) for i in items[-MAX_ITEMS:]]
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    tmp.replace(QUEUE_FILE)


def add(chat: str, text: str, reason: str = "") -> Dict[str, Any]:
    """进队列（同一个人同样的话不重复进）。"""
    chat = str(chat or "").strip()
    text = str(text or "").strip()
    if not chat or not text:
        return {}
    items = _load_raw()
    for item in items:
        if item.get("chat") == chat and item.get("text") == text:
            item["reason"] = reason or item.get("reason", "")
            item["at"] = item.get("at") or int(time.time())
            _save(items)
            return item
    entry = {"chat": chat, "text": text, "reason": str(reason)[:120], "at": int(time.time())}
    items.append(entry)
    _save(items)
    log.info("进待发送队列：%s（%s）", chat, reason or "未说明")
    return entry


def list_items() -> List[Dict[str, Any]]:
    return _load_raw()


def count() -> int:
    return len(_load_raw())


def remove(chat: str, text: str) -> List[Dict[str, Any]]:
    items = [i for i in _load_raw() if not (i.get("chat") == chat and i.get("text") == text)]
    _save(items)
    return items


def clear() -> None:
    _save([])
