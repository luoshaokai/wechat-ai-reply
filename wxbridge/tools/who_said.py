"""查一条消息到底落在哪个会话里（防止「回复发错窗口」）。

用法：python -X utf8 tools/who_said.py "在呢在呢"
"""

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from wechatauto.db import WeChatDB  # noqa: E402


def main() -> None:
    needle = sys.argv[1] if len(sys.argv) > 1 else "在呢在呢"
    db = WeChatDB()
    for name in ["小王", "小明", "文件传输助手"]:
        try:
            username = db.username_by_nickname(name)
        except Exception as error:  # noqa: BLE001
            print(name, "认不出:", error)
            continue
        rows = db.get_messages(username, limit=30) or []
        hit = [r for r in rows if needle in str(r.get("content") or "")]
        print(f"{name} ({username}): 命中 {len(hit)} 条")
        for row in hit:
            print("   ", {k: row.get(k) for k in
                          ("sender_username", "create_time", "talker", "username",
                           "str_talker", "content") if k in row})


if __name__ == "__main__":
    main()
