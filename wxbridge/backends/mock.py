"""Mock 后端：不连微信，用来验证桥接协议和外部应答端的联动。

用法::

    python bridge.py --backend mock --demo
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from .base import Backend, InboundMessage, MessageSink

log = logging.getLogger("wxbridge.mock")


class MockBackend(Backend):
    """假装自己是微信：定时造一条消息进来，发出去的消息只记日志。"""

    name = "mock"

    def __init__(self, script: Optional[list] = None) -> None:
        #: [(chat, chat_type, sender, text, delay_seconds), ...]
        self._script = script or []
        self._sink: Optional[MessageSink] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self, sink: MessageSink) -> None:
        self._sink = sink
        self._thread = threading.Thread(target=self._run, name="mock", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def account(self) -> str:
        return "mock-bot"

    def send(self, chat: str, text: str) -> bool:
        log.info("[mock] 发送 -> %s：%s", chat, text)
        return True

    def _run(self) -> None:
        for chat, chat_type, sender, text, delay in self._script:
            if self._stop.wait(delay):
                return
            if self._sink is None:
                return
            self._sink(
                InboundMessage(
                    chat=chat,
                    chat_type=chat_type,
                    sender=sender,
                    text=text,
                    mentioned=text.startswith("@"),
                    ts=int(time.time() * 1000),
                )
            )
