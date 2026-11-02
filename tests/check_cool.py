# -*- coding: utf-8 -*-
"""离线验证：把拟人节流的冷却压到固定 30 秒（不真发消息、不真睡）。"""
import os
import sys
import time

BRIDGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "wxbridge")
sys.path.insert(0, BRIDGE)

from wechatauto import rhythm  # noqa: E402
from backends import wechatauto as wxbackend  # noqa: E402

# 节流状态是落盘的（用户目录下的 rhythm.json），跨进程生效。测试里把状态文件
# 指到临时目录，免得把正在跑的桥接的突发计数覆盖掉。
TMP = os.path.join(os.environ.get("TEMP", "/tmp"), "rhythm-test-%d" % os.getpid())
os.makedirs(TMP, exist_ok=True)
rhythm.STATE_DIR = TMP
rhythm.STATE_FILE = os.path.join(TMP, "rhythm.json")

real_sleep = time.sleep
slept = []


def fake_sleep(seconds):
    slept.append(float(seconds))


time.sleep = fake_sleep
fails = []


def check(name, cond, got=""):
    print(("PASS " if cond else "FAIL ") + name + ("  -> " + str(got) if got != "" else ""))
    if not cond:
        fails.append(name)


# 1) 常量与入口存在
check("COOL_SECONDS = 30", wxbackend.COOL_SECONDS == 30.0, wxbackend.COOL_SECONDS)

# 2) 未调档前：库默认自然档冷却是 30~75 秒随机
base = rhythm.PROFILES["natural"].cooloff
check("库默认冷却是 30~75 随机", tuple(base) == (30.0, 75.0), base)

# 3) 调档后 cooloff 固定 30
wxbackend._tune_rhythm()
check("调档后 cooloff=(30,30)", tuple(rhythm.profile().cooloff) == (30.0, 30.0),
      rhythm.profile().cooloff)

# 4) 再调一次不该重复改（幂等，不刷屏日志）
wxbackend._tune_rhythm()
check("重复调档后仍是 30", tuple(rhythm.profile().cooloff) == (30.0, 30.0))

# 5) 撞上限时 gate 只睡 30 秒
now = time.time()
rhythm._stamps = [now - i for i in range(6)]     # 120 秒窗口内已写 6 次
rhythm._last_write = now                          # 最小间隔已够，只剩冷却
rhythm._loaded = True
slept.clear()
waited = rhythm.gate("send")
# gate 里有两个等待：写动作最小间隔（自然档 2.5~6 秒，随机）+ 撞上限后的冷却。
# 这里只把冷却固定成 30 秒；最小间隔那一段是库自带的防刷屏设计，不动。
cool = [s for s in slept if s >= 25.0]
check("撞上限后冷却正好 30 秒", len(cool) == 1 and abs(cool[0] - 30.0) < 0.01, slept)
check("总等待不超过 6+30 秒", waited <= 37.0, waited)
check("冷却后突发计数清零", len(rhythm._stamps) == 1, len(rhythm._stamps))

# 6) 没撞上限时不该有额外冷却（只按最小间隔）
rhythm._stamps = [time.time()]
rhythm._last_write = 0.0
slept.clear()
rhythm.gate("send")
check("未撞上限时没有 30 秒冷却", all(s < 10.0 for s in slept), slept)

# 7) 别的入口切档后，下一次调档要能压回来
rhythm.set_profile("natural")
check("切档把冷却打回 30~75", tuple(rhythm.profile().cooloff) != (30.0, 30.0),
      rhythm.profile().cooloff)
wxbackend._tune_rhythm()
check("重新调档又压回 30", tuple(rhythm.profile().cooloff) == (30.0, 30.0))

time.sleep = real_sleep
import shutil  # noqa: E402

shutil.rmtree(TMP, ignore_errors=True)
print("")
print("失败 %d 项" % len(fails) + ("：" + "、".join(fails) if fails else "（全过）"))
sys.exit(1 if fails else 0)
