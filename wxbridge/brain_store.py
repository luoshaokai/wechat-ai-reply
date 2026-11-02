"""大脑配置：回话就用这一个模型。

以前分「云端 / 本地 / 自动」三档，还要跟别的程序的配置对齐——太绕了。
现在只有一个模型：这页填什么就用什么。不填就用 config.yaml 里 llm 段配好的模型
（这样装好就能用，不用先填一堆东西）。

    wxbridge/brain.json
    {
      "api": "openai" | "anthropic",
      "baseURL": "http://127.0.0.1:11434/v1",
      "model": "qwen2.5:7b",
      "apiKey": "",
      "updated": 1793123456
    }

常见本地服务（点模板自动填，baseURL 里带不带 /v1 都认）：
    Ollama      http://127.0.0.1:11434/v1      qwen2.5:7b / llama3.1:8b
    LM Studio   http://127.0.0.1:1234/v1       模型名以它界面里显示的为准
    llama.cpp   http://127.0.0.1:8080/v1
    vLLM        http://127.0.0.1:8000/v1
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger("wxbridge.brain_cfg")

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "brain.json"

#: 常见服务的默认值，点一下就填上，省得去翻文档。
TEMPLATES: List[Dict[str, str]] = [
    {"name": "Ollama", "baseURL": "http://127.0.0.1:11434/v1", "model": "qwen2.5:7b", "api": "openai"},
    {"name": "LM Studio", "baseURL": "http://127.0.0.1:1234/v1", "model": "local-model", "api": "openai"},
    {"name": "llama.cpp", "baseURL": "http://127.0.0.1:8080/v1", "model": "local-model", "api": "openai"},
    {"name": "vLLM", "baseURL": "http://127.0.0.1:8000/v1", "model": "local-model", "api": "openai"},
    {"name": "DeepSeek", "baseURL": "https://api.deepseek.com/v1", "model": "deepseek-chat", "api": "openai"},
    {"name": "OpenAI", "baseURL": "https://api.openai.com/v1", "model": "gpt-4o-mini", "api": "openai"},
    {"name": "Anthropic", "baseURL": "https://api.anthropic.com", "model": "claude-3-5-haiku-latest",
     "api": "anthropic"},
]

DEFAULT: Dict[str, Any] = {
    "api": "openai",
    "baseURL": "",
    "model": "",
    "apiKey": "",
    "updated": 0,
}


def load() -> Dict[str, Any]:
    """读配置（坏了回默认，不炸）。"""
    data = dict(DEFAULT)
    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
        if isinstance(raw, dict):
            api = str(raw.get("api") or "openai")
            data["api"] = api if api in ("openai", "anthropic") else "openai"
            data["baseURL"] = str(raw.get("baseURL") or "").strip()
            data["model"] = str(raw.get("model") or "").strip()
            data["apiKey"] = str(raw.get("apiKey") or "").strip()
            data["updated"] = int(raw.get("updated") or 0)
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        log.warning("读 brain.json 失败，用默认：%s", exc)
    return data


def save(api: str, baseURL: str, model: str, apiKey: str, clear_key: bool = False) -> Dict[str, Any]:
    """保存。密钥不打印到日志里。

    `apiKey` 留空表示**不改动**：控制台的密钥框平时是空的（占位写「已保存密钥（留空不改）」），
    用户只想换个模型名时也会提交空串，把已经存好的密钥擦掉 —— 之后云端服务一律 401，
    表现就是「连接测试失败」。所以空串一律沿用旧值，真要抹掉请显式 `clear_key=True`。
    """
    key = "" if clear_key else (str(apiKey or "").strip() or load()["apiKey"])
    data = {
        "api": api if api in ("openai", "anthropic") else "openai",
        "baseURL": str(baseURL or "").strip(),
        "model": str(model or "").strip(),
        "apiKey": key,
        "updated": int(time.time()),
    }
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_FILE)
    return data


def configured(cfg: Dict[str, Any] | None = None) -> bool:
    """地址和模型都填了，才算自己指定了模型。"""
    data = cfg or load()
    return bool(data["baseURL"]) and bool(data["model"])


def snapshot(cloud: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """给控制台的一整包：当前设置 + 模板 + 现在实际用的是哪个。"""
    data = load()
    cloud = cloud or {}
    if configured(data):
        active = f"{data['model']}　（{data['baseURL']}）"
        source = "自己填的"
    else:
        active = f"{cloud.get('model') or '（config.yaml 里也没填）'}　（{cloud.get('baseURL') or ''}）"
        source = "跟随 config.yaml"
    return {
        "api": data["api"],
        "baseURL": data["baseURL"],
        "model": data["model"],
        "hasKey": bool(data["apiKey"]),
        "updated": data["updated"],
        "templates": TEMPLATES,
        "active": active,
        "source": source,
        "cloud": {"model": cloud.get("model") or "", "baseURL": cloud.get("baseURL") or ""},
        "file": str(CONFIG_FILE),
    }
