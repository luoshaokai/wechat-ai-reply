"""手动往某个会话发一条消息（补一句、救急用）。

用法：python -X utf8 tools/send_once.py "小明" "刚在忙，语音发不了，打字说吧"

做法：临时当一个「不报身份」的外部应答端连上桥接，只发 send 帧。
桥接优先把入站消息喂给正式接入的应答端（报过身份的那个），所以这个临时客户端
收不到对方消息，不会跟自动回复抢活儿。
"""

import asyncio
import json
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import websockets  # noqa: E402

URL = "ws://127.0.0.1:8765"


async def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        return
    chat, text = sys.argv[1], sys.argv[2]
    async with websockets.connect(URL, max_size=8 * 1024 * 1024) as ws:
        # 先读掉握手帧（hello），别把它当成发送结果。
        try:
            await asyncio.wait_for(ws.recv(), timeout=10)
        except asyncio.TimeoutError:
            pass
        await ws.send(json.dumps({"op": "send", "chat": chat, "text": text},
                                 ensure_ascii=False))
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=60)
        except asyncio.TimeoutError:
            print("等发送结果超时（桥接可能正忙）")
            return
        print(raw)


if __name__ == "__main__":
    asyncio.run(main())
