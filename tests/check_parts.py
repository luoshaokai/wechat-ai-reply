"""reply_parts 端到端核对（模型输出打桩，只看拆条）。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "wxbridge"))

from brain import Brain  # noqa: E402

b = Brain()
b._persona = "你在用微信跟熟人聊天。"
b._llm = {"baseURL": "http://x", "model": "m"}
b._key = "k"
b._loaded = True
b._remember = lambda *a, **k: None

CASES = [
    ("好的收到", ["好的收到"]),
    ("在开会，等下跟你说", ["在开会", "等下跟你说"]),
    ("这句话挺长。后面还有一句。", ["这句话挺长", "后面还有一句。"]),
    ("你吃了吗？我去买菜", ["你吃了吗", "我去买菜"]),
    ("第一段|||第二段", ["第一段", "第二段"]),
]

bad = 0
for text, want in CASES:
    b._generate = lambda c, t, s=None, _x=text: _x
    got = b.reply_parts("张三", "喂")
    ok = got == want
    if not ok:
        bad += 1
    b._generate = lambda c, t, s=None, _x=text: _x
    one = b.reply("张三", "喂")
    print("%s 生成=%r -> 发%d条 %r（reply=%r）" % ("OK  " if ok else "FAIL", text, len(got), got, one))

print("全部通过" if not bad else "有 %d 条不符" % bad)
sys.exit(1 if bad else 0)
