"""查消息库：看看「真正发进微信」的正文长什么样（联调用）。

用法：python -X utf8 tools/check_sent.py "小明" "小王"
"""

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from wechatauto.db import WeChatDB  # noqa: E402

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from backends.wechatauto import _EMOJI_RE  # noqa: E402


def main() -> None:
    names = sys.argv[1:] or ["小明", "小王"]
    db = WeChatDB()
    self_ids = set()
    for attr in ("wxid", "self_wxid", "current_wxid", "user_name"):
        value = getattr(db, attr, None)
        if value:
            self_ids.add(str(value))
    for name in names:
        try:
            username = db.username_by_nickname(name)
        except Exception as error:  # noqa: BLE001
            print(name, "认不出会话:", error)
            continue
        print("===", name, "->", username)
        try:
            rows = db.get_messages(username, limit=10) or []
        except Exception as error:  # noqa: BLE001
            print("   读消息失败:", error)
            continue
        for row in rows:
            sender = str(row.get("sender_username") or "")
            who = "我" if sender in self_ids else sender
            content = str(row.get("content") or "")
            has_emoji = bool(_EMOJI_RE.search(content))
            print(f"   [{who}] {'⚠有表情' if has_emoji else '纯文字'} {content!r}")


if __name__ == "__main__":
    main()
