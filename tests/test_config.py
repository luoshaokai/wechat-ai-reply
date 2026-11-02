# -*- coding: utf-8 -*-
"""离线验证 appconfig 的极小 YAML 解析器（不需要任何第三方包）。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "wxbridge"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import appconfig  # noqa: E402

fails = []


def check(name, got, want):
    ok = got == want
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "  -> got=%r want=%r" % (got, want)))
    if not ok:
        fails.append(name)


# 用仓库里那份样例当被解析对象：样例写得不对，测试就该红
SAMPLE = os.path.join(os.path.dirname(HERE), "config.example.yaml")
text = open(SAMPLE, encoding="utf-8-sig").read()
data = appconfig._parse(text)

check("llm.baseURL", (data.get("llm") or {}).get("baseURL"), "http://localhost:11434/v1")
check("llm.api", (data.get("llm") or {}).get("api"), "openai")
check("llm.model", (data.get("llm") or {}).get("model"), "qwen2.5:7b")
check("llm.apiKey 空", (data.get("llm") or {}).get("apiKey"), "")
check("limits.minIntervalSeconds 是整数", (data.get("limits") or {}).get("minIntervalSeconds"), 10)
check("limits.dailyMaxPerChat 0 = 不限", (data.get("limits") or {}).get("dailyMaxPerChat"), 0)
check("respond.allowChats 是列表", (data.get("respond") or {}).get("allowChats"), ["小明", "小红"])
check("respond.groupsNeedAt 是布尔", (data.get("respond") or {}).get("groupsNeedAt"), True)
check("send.delaySeconds 列表", (data.get("send") or {}).get("delaySeconds"), [3, 8])
check("send.maxAgeSeconds", (data.get("send") or {}).get("maxAgeSeconds"), 120)
persona = str(data.get("persona") or "")
check("persona 读成了整块文本", ("你不是助手" in persona and "20 个字" in persona), True)
check("persona 里的 - 项没被当成列表吃掉", persona.count("\n- ") >= 3, True)

# 合并默认值：缺项要补齐，不能 KeyError
merged = appconfig._merge(appconfig.DEFAULTS, data)
check("缺项补默认（send.dockRight）", (merged.get("send") or {}).get("dockRight"), True)
empty = appconfig._merge(appconfig.DEFAULTS, {})
check("空配置也能拿到默认限速", (empty.get("limits") or {}).get("minIntervalSeconds"), 10)
check("空配置名单是空列表", (empty.get("respond") or {}).get("allowChats"), [])

# 行尾注释、BOM、CRLF、GBK 之外的怪写法
odd = appconfig._parse(
    "limits:\r\n  minIntervalSeconds: 5   # 注释\r\n  dailyMaxTotal: 20\r\n"
    "respond:\r\n  allowChats:\r\n    - 张三\r\n    - 李四\r\n")
check("行尾注释被去掉", (odd.get("limits") or {}).get("minIntervalSeconds"), 5)
check("块列表解析", (odd.get("respond") or {}).get("allowChats"), ["张三", "李四"])
check("没写的段保持缺省", odd.get("send"), None)

print("")
print("失败 %d 项" % len(fails) + ("：" + "、".join(fails) if fails else "（全过）"))
sys.exit(1 if fails else 0)
