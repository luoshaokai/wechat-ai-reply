"""拆条规则的离线核对：整条默认发一条，出现逗号/句号/问号才切成两条。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "wxbridge"))

from brain import MAX_PARTS, SPLIT, split_at_punct  # noqa: E402

CASES = [
    # ---- 不拆的情况
    ("嗯嗯", ["嗯嗯"]),                                  # 啥标点没有
    ("这条没有任何标点所以整条发出去", ["这条没有任何标点所以整条发出去"]),
    ("嗯嗯，好", ["嗯嗯，好"]),                          # 逗号后半只剩 1 字 → 不切
    ("行，我知道了", ["行，我知道了"]),                  # 逗号前半只剩 1 字 → 不切
    ("好！马上到", ["好！马上到"]),                      # 感叹号不算
    ("花 1.5 块钱买个冰棍", ["花 1.5 块钱买个冰棍"]),    # 英文句点不算
    ("https://a.com/b?a=1 你看下", ["https://a.com/b?a=1 你看下"]),  # 英文问号不算
    ("，开头就是逗号", ["，开头就是逗号"]),              # 够不上门槛 → 不切
    # ---- 拆成两条
    ("在开会，等下跟你说", ["在开会", "等下跟你说"]),    # 逗号
    ("好的。收到哈", ["好的", "收到哈"]),                # 句号
    ("你吃了吗？我去买菜", ["你吃了吗", "我去买菜"]),    # 问号
    ("别急，慢慢来；没问题", ["别急", "慢慢来；没问题"]),  # 切在逗号，分号原样留着
    ("刚看完，觉得还行，那周末一起去", ["刚看完", "觉得还行，那周末一起去"]),
    ("English, keep going", ["English", "keep going"]),  # 英文逗号也算
    ("第一段" + SPLIT + "第二段", ["第一段", "第二段"]),
    ("", []),
]

bad = 0
for text, want in CASES:
    got = split_at_punct(text.replace(SPLIT, "，"))
    ok = got == want
    if not ok:
        bad += 1
    print("%s %r -> %r（期望 %r）" % ("OK  " if ok else "FAIL", text, got, want))

print("MAX_PARTS =", MAX_PARTS)
print("全部通过" if not bad else "有 %d 条不符" % bad)
sys.exit(1 if bad else 0)
