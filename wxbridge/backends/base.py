"""后端接口：把「一个能收发微信消息的东西」抽象成同一套契约。

桥接层只认这个契约，不认具体是 wxautox 还是 wcferry。这样换后端时
WebSocket 协议、外部应答端都不用动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class InboundMessage:
    """一条收到的消息。

    Attributes:
        chat: 会话标识（好友昵称或群名），回发时用它定位聊天窗口。
        chat_type: ``private`` 或 ``group``。
        sender: 发送者昵称；私聊时等于 chat。
        text: 正文文本。
        mentioned: 群里是否 @ 了我。
        ts: 毫秒时间戳。
        raw: 后端原始对象，调试用。
    """

    chat: str
    chat_type: str
    sender: str
    text: str
    mentioned: bool = False
    ts: int = 0
    raw: object = field(default=None, repr=False)

    def to_json(self) -> dict:
        return {
            "op": "message",
            "chat": self.chat,
            "chatType": self.chat_type,
            "sender": self.sender,
            "text": self.text,
            "mentioned": self.mentioned,
            "ts": self.ts,
        }


MessageSink = Callable[[InboundMessage], None]


class Backend:
    """微信后端基类。

    子类需要实现 :meth:`start`、:meth:`stop`、:meth:`send`。
    """

    #: 后端名字，报给外部应答端做诊断用。
    name = "base"

    def start(self, sink: MessageSink) -> None:
        """启动后端，并把收到的消息交给 ``sink``。"""
        raise NotImplementedError

    def stop(self) -> None:
        """停止后端，释放窗口与线程。"""
        raise NotImplementedError

    def send(self, chat: str, text: str) -> bool:
        """给 ``chat`` 发一条文本，返回是否成功。"""
        raise NotImplementedError

    def account(self) -> str:
        """当前登录账号的昵称；拿不到就返回空串。"""
        return ""

    def can_send_now(self) -> bool:
        """现在能不能发（默认能）。

        给上层的「自动补发」用：界面类后端在暂停、锁屏、微信无响应的时候
        点下去只会卡死自己，所以先问一句再决定要不要试。
        不看界面的后端（mock）没有这种状态，保持默认 True。
        """
        return True

    def already_delivered(self, chat: str, text: str, since: float) -> bool:
        """这条是不是已经发出去了（默认不知道 = False）。

        自动补发前拿它查重，避免同一条发给真人两遍。会读消息库的后端覆盖这个方法；
        不看库的后端（mock）没这个概念，保持 False 就是「照常补」。
        """
        return False
