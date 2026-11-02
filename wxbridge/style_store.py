"""说话风格：一段可以随便改的文字 + 几个现成模板。

设计上只认真实文本（`text`）：点下面的模板只是**把文字替换进主文本框**，
然后可以继续自己加、叠加、保存。文件里同时写 `effective`（就是 text 本身），
外部应答端读的就是它——所以改完下一条消息就生效，不用重启。

    wxbridge/style.json
    {"text": "...", "updated": 1793..., "effective": "...", "effectiveName": "..."}
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger("wxbridge.style")

HERE = Path(__file__).resolve().parent
STYLE_FILE = HERE / "style.json"

MAX_TEXT = 4000

#: 模板。点一下会替换主文本框里的内容（不是叠加），再自己改。
PRESETS: List[Dict[str, str]] = [
    {
        "id": "default",
        "name": "默认：像本人",
        "desc": "短句、口语、有语气词，一条最多 20 字。最稳，不容易露馅。",
        "text": "像平时发微信那样：短句、口语、带点语气词（行、咋了、哈哈），一条最多 20 字。"
                "被问什么就随口答，别解释、别总结、别说教。",
    },
    {
        "id": "cool",
        "name": "高冷：能一个字不写两个字",
        "desc": "话少、不主动找话题。适合不想多聊的时候。",
        "text": "话少一点：能一个字就一个字（嗯、行、不去、没空），不主动找话题，"
                "对方问三句你回半句。不解释理由。",
    },
    {
        "id": "warm",
        "name": "温和：会接住情绪",
        "desc": "语气软、会顺着对方说，适合家人和关系近的人。",
        "text": "语气软一点：会接住对方的情绪，多说半句，可以用「嗯嗯」「好呀」「别急」这种。"
                "对方难受就顺着说，不分析、不给建议。",
    },
    {
        "id": "roast",
        "name": "损友：爱抬杠嘴贫",
        "desc": "损两句再给台阶，适合常开玩笑的朋友。",
        "text": "说话像损友：爱抬杠、爱拆台、爱复读对方的梗，损两句就给个台阶。"
                "不下狠嘴、不人身攻击、不聊过界的话题。",
    },
    {
        "id": "formal",
        "name": "正经：对同事长辈",
        "desc": "完整句子、礼貌但不客套，不用网络用语。",
        "text": "说话正经一点：用完整句子，礼貌但不客套，不用网络用语和语气词，"
                "不主动开玩笑；对方开玩笑就简单接一句。",
    },
]


def _default_text() -> str:
    return PRESETS[0]["text"]


def load() -> Dict[str, Any]:
    """读当前设置；兼容老格式（preset + custom）。"""
    data: Dict[str, Any] = {"text": _default_text(), "updated": 0, "legacy": False}
    try:
        raw = STYLE_FILE.read_text(encoding="utf-8-sig")
        loaded = json.loads(raw)
        if isinstance(loaded, dict):
            text = loaded.get("text")
            if not isinstance(text, str) or not text.strip():
                # 老格式迁移：模板正文 + custom 补充
                pid = str(loaded.get("preset") or "default")
                preset = next((p for p in PRESETS if p["id"] == pid), PRESETS[0])
                custom = str(loaded.get("custom") or "").strip()
                text = (preset["text"] + ("\n补充要求：" + custom if custom else "")).strip()
                data["legacy"] = bool(pid != "default" or custom)
            data["text"] = str(text)[:MAX_TEXT]
            data["updated"] = loaded.get("updated") or 0
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        log.warning("读 style.json 失败，用默认：%s", exc)
    return data


def save(text: str) -> Dict[str, Any]:
    """保存风格正文，同时写好外部应答端要读的 effective 字段。"""
    value = str(text or "").strip()[:MAX_TEXT] or _default_text()
    data = {
        "text": value,
        "updated": int(time.time()),
        # 下面两个是给外部应答端用的：它只认 effective，不用自己维护模板表
        "effective": value,
        "effectiveName": "自定义",
    }
    tmp = STYLE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STYLE_FILE)
    return data


def effective() -> str:
    """当前生效的风格正文（本地大脑拼进系统提示词用）。"""
    return load()["text"]


def snapshot() -> Dict[str, Any]:
    """给控制台的一整包。"""
    data = load()
    return {
        "text": data["text"],
        "updated": data["updated"],
        "presets": [{"id": p["id"], "name": p["name"], "desc": p["desc"], "text": p["text"]}
                    for p in PRESETS],
        "file": str(STYLE_FILE),
    }
