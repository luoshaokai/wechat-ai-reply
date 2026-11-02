# -*- coding: utf-8 -*-
"""wechatauto 后端的离线单测：不连微信，只喂真实形状的数据库记录。

样本按微信 Windows 4.1.15.55 解密库里的字段形状手工构造（不含任何真实账号数据）。
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "wxbridge"))

from backends.wechatauto import WechatAutoBackend   # noqa: E402

SELF = "wxid_self00000001"
NOW = int(time.time())

PASS = 0
FAIL = []


def check(name, got, want):
    global PASS
    if got == want:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL.append(name)
        print("  FAIL  %s -> got=%r want=%r" % (name, got, want))


class FakeDB:
    def get_nickname(self, u):
        return {"10000000001@chatroom": "演练群",
                "wxid_friend00003": "小张",
                "wxid_friend00004": "李工"}.get(u, u)

    def get_self_info(self):
        return {"username": SELF, "nick_name": "🍊", "remark": ""}


def make_backend():
    b = WechatAutoBackend()
    b._self_wxid = SELF
    b._account = "🍊"
    b._self_names = {"🍊"}
    b._db = FakeDB()
    b._names = {}
    b._user_by_name = {}
    return b


def capture(b, msg):
    got = []
    b._sink = got.append
    b._on_message(msg, None)
    return got[0] if got else None


print("=== 1. 过滤规则 ===")
b = make_backend()
check("自己发的不回",
      capture(b, {"username": "wxid_friend00006", "type": "文本",
                  "sender_username": SELF, "create_time": NOW,
                  "content": "wechatauto 链路自检 OK"}), None)
check("文件传输助手不回",
      capture(b, {"username": "filehelper", "type": "文本",
                  "sender_username": "other", "create_time": NOW,
                  "content": "在吗"}), None)
check("公众号不回",
      capture(b, {"username": "gh_000000000000", "type": "文本",
                  "sender_username": "gh_000000000000", "create_time": NOW,
                  "content": "推文"}), None)
check("图片不回",
      capture(b, {"username": "wxid_friend00005", "type": "图片",
                  "sender_username": "wxid_friend00005", "create_time": NOW,
                  "content": '<?xml version="1.0"?><msg><img aeskey="x"/>'}), None)
check("语音不回",
      capture(b, {"username": "wxid_friend00005", "type": "语音",
                  "sender_username": "wxid_friend00005", "create_time": NOW,
                  "content": "<msg><voicemsg/>"}), None)
check("太旧的不回（停机积压）",
      capture(b, {"username": "wxid_friend00005", "type": "文本",
                  "sender_username": "wxid_friend00005", "create_time": NOW - 3600,
                  "content": "昨天那句忘回了"}), None)
check("刚好在窗口内的回",
      (capture(b, {"username": "wxid_friend00005", "type": "文本",
                   "sender_username": "wxid_friend00005", "create_time": NOW - 5,
                   "content": "在吗"}) or {}).text, "在吗")

print("=== 2. 群消息解析（真实样本）===")
b = make_backend()
m = capture(b, {"username": "10000000001@chatroom", "type": "文本",
                "sender_username": "wxid_friend00003", "create_time": NOW,
                "content": "wxid_friend00003:\n收到"})
check("群前缀已剥离", (m or {}).text, "收到")
check("会话是群", (m or {}).chat_type, "group")
check("会话名用群名", (m or {}).chat, "演练群")
check("发送者名字已解析", (m or {}).sender, "小张")
check("没 @ 我 -> mentioned False", (m or {}).mentioned, False)

b = make_backend()
m = capture(b, {"username": "10000000001@chatroom", "type": "文本",
                "sender_username": "wxid_friend00004", "create_time": NOW,
                "content": "wxid_friend00004:\n@\U0001f34a\u2005@小王\u2005会议改到周四下午四点，你们这边可以不"})
check("@我(emoji昵称+U+2005) -> mentioned True", (m or {}).mentioned, True)
check("@我的正文不含前缀", (m or {}).text.startswith("@🍊"), True)

b = make_backend()
m = capture(b, {"username": "10000000001@chatroom", "type": "文本",
                "sender_username": "wxid_friend00004", "create_time": NOW,
                "content": "wxid_friend00004:\n@小王\u2005你看一下你负责的那部分"})
check("@别人 -> mentioned False", (m or {}).mentioned, False)

# 昵称是汉字时不能被长名字骗到
b = make_backend()
b._self_names = {"明"}
m = capture(b, {"username": "10000000001@chatroom", "type": "文本",
                "sender_username": "wxid_friend00004", "create_time": NOW,
                "content": "wxid_friend00004:\n@明天见\u2005这个页面打不开"})
check("@明天见 不算 @明", (m or {}).mentioned, False)

print("=== 3. 私聊正文不被误剥 ===")
b = make_backend()
m = capture(b, {"username": "wxid_friend00002", "type": "文本",
                "sender_username": "wxid_friend00002", "create_time": NOW,
                "content": "备注:这个文件的说明"})
check("私聊带冒号开头不剥", (m or {}).text, "备注:这个文件的说明")
check("私聊 chat_type", (m or {}).chat_type, "private")

print("=== 4. 发送定位：只能用显示名 ===")
# 实测：who="filehelper" 重试 142s 报「会话没打开」，who="文件传输助手" 2.3s 成功。
b = make_backend()
b._names = {"wxid_friend00002": "小刚",
            "10000000001@chatroom": "演练群",
            "filehelper": "文件传输助手"}
check("显示名原样返回", b._target_of("小刚"), "小刚")
check("wxid 翻成显示名", b._target_of("wxid_friend00002"), "小刚")
check("群号翻成群名", b._target_of("10000000001@chatroom"), "演练群")
check("系统号翻成显示名", b._target_of("filehelper"), "文件传输助手")
check("空串返回空", b._target_of("  "), "")
b = make_backend()
check("查不到显示名时原样兜底", b._target_of("wxid_不在通讯录"), "wxid_不在通讯录")

print("=== 4b. 回复前的随机延迟 ===")
check("默认区间 3~8s", WechatAutoBackend()._send_delay, (3.0, 8.0))
check("传 0 关掉", WechatAutoBackend(send_delay=(0, 0))._send_delay, (0.0, 0.0))
b = WechatAutoBackend(send_delay=None)
b._wx = object()          # 骗过「窗口没连接」那道判断
b._user_by_name = {}
check("None 也当不延迟", b._send_delay, (0.0, 0.0))

print("=== 5. 发送前文本净化 ===")
THINK = "<" + "think>先想一下再说</" + "think>"          # 运行时才拼出标签，避免写文件被吞
THINKML = "<" + "antml:thinking>规划</" + "antml:thinking>"
check("去 think 块",
      WechatAutoBackend._tidy(THINK + "\n我看看"), "我看看")
check("think 块在中间也去",
      WechatAutoBackend._tidy("前面" + THINK + "后面"), "前面后面")
check("去 antml:thinking 块",
      WechatAutoBackend._tidy(THINKML + "\n结论"), "结论")
check("去粗体星号、留单星号",
      WechatAutoBackend._tidy("这是**重点**，2*3=6"), "这是重点，2*3=6")
check("压连续空行",
      WechatAutoBackend._tidy("一行\n\n\n\n二行"), "一行\n\n二行")
check("去行尾空白",
      WechatAutoBackend._tidy("行尾有点空格   \n下一行"), "行尾有点空格\n下一行")
check("只有 think -> 空", WechatAutoBackend._tidy(THINK), "")
check("保留正常换行",
      WechatAutoBackend._tidy("第一行\n第二行"), "第一行\n第二行")

print("=== 6. 发送结果判定 ===")
b = make_backend()


class R(dict):
    @property
    def is_success(self):
        return self["status"] == "成功"


check("WxResponse 成功", b._ok(R(status="成功", message="x", data={})), True)
check("WxResponse 失败", b._ok(R(status="失败", message="x", data={})), False)
check("普通字典", b._ok({"status": "成功"}), True)
check("None", b._ok(None), False)

print("=== 7. 「结果未确认」要回库里确认 ===")
check("未确认措辞命中",
      WechatAutoBackend._attempted_unconfirmed(
          R(status="失败", message="已尝试发送但结果未确认，为避免重复发送不再自动重试", data={})),
      True)
check("会话没打开不算未确认",
      WechatAutoBackend._attempted_unconfirmed(
          R(status="失败", message="发送失败：多次重试未完成（会话没打开）", data={})),
      False)
check("成功不算未确认",
      WechatAutoBackend._attempted_unconfirmed(R(status="成功", message="消息已发送", data={})),
      False)
check("None 不炸", WechatAutoBackend._attempted_unconfirmed(None), False)

print("=== 8. 输入框里还是不是我们那条（新判据，替代数像素）===")


class FakePattern:
    def __init__(self, value):
        self.Value = value


class FakeCtrl:
    def __init__(self, value=None, no_pattern=False, boom=False):
        self.value, self.no_pattern, self.boom = value, no_pattern, boom

    def GetValuePattern(self):
        if self.boom:
            raise RuntimeError("控件没了")
        if self.no_pattern:
            return None
        return FakePattern(self.value)


class FakeUia:
    def __init__(self, ctrl=None, boom=False):
        self.ctrl, self.boom = ctrl, boom

    def _chat_input(self, win=None):
        if self.boom:
            raise RuntimeError("UIA 断了")
        return self.ctrl


class FakeInput:
    def __init__(self):
        self.press = 0

    def key(self, vk):
        self.press += 1


class FakeGui:
    """只喂 _ours_in_box / _rescue_enter 用到的那几个接口。"""

    def __init__(self, uia, pixel=True, values=None):
        self._uia, self._pixel, self._values = uia, pixel, values
        self._input = FakeInput()
        self.step = 0

    def _get_uia(self):
        if self._values is not None:
            idx = min(self.step, len(self._values) - 1)
            self.step += 1          # 每读一次走一帧，模拟补按后文字消失
            ctrl = FakeCtrl(self._values[idx])
            return FakeUia(ctrl)
        return self._uia

    def _input_box_has_text(self):
        return self._pixel


b8 = make_backend()
b8._focus_wechat = lambda gui: None
OURS = "刚在忙，没盯着手机"

check("框里就是我们那条",
      b8._ours_in_box(FakeGui(FakeUia(FakeCtrl(OURS))), OURS), True)
check("框里是号主自己的草稿",
      b8._ours_in_box(FakeGui(FakeUia(FakeCtrl("晚上出来吃火锅不"))), OURS), False)
check("框里已经空了（发走了）",
      b8._ours_in_box(FakeGui(FakeUia(FakeCtrl(""))), OURS), False)
check("框里是会话名（读错控件）不算我们那条",
      b8._ours_in_box(FakeGui(FakeUia(FakeCtrl("文件传输助手"))), OURS), False)
check("说了会话名后读到会话名 = 没把握（不能当已发送）",
      b8._ours_in_box(FakeGui(FakeUia(FakeCtrl("文件传输助手"))), OURS, "文件传输助手"), None)
check("正文为空不猜", b8._ours_in_box(FakeGui(FakeUia(FakeCtrl(OURS))), ""), None)
check("UIA 不可用 = 没把握", b8._ours_in_box(FakeGui(None), OURS), None)
check("控件拿不到 = 没把握（不能当已发送）",
      b8._ours_in_box(FakeGui(FakeUia(None)), OURS), None)
check("读值抛异常 = 没把握",
      b8._ours_in_box(FakeGui(FakeUia(FakeCtrl(OURS, boom=True))), OURS), None)
check("没有 ValuePattern = 没把握",
      b8._ours_in_box(FakeGui(FakeUia(FakeCtrl(None, no_pattern=True))), OURS), None)
check("UIA 自己抛异常 = 没把握",
      b8._ours_in_box(FakeGui(FakeUia(boom=True)), OURS), None)
check("前后空格也算同一条",
      b8._ours_in_box(FakeGui(FakeUia(FakeCtrl("  " + OURS + " "))), " " + OURS), True)

print("--- 补按回车的安全线 ---")
# 判据不是 True（别人的草稿 / 读不到）时，一个回车都不许按。
gui_draft = FakeGui(FakeUia(FakeCtrl("晚上出来吃火锅不")))
check("框里是草稿时不补按", b8._rescue_enter(gui_draft, "define", OURS), False)
check("  一次回车都没按", gui_draft._input.press, 0)

gui_blind = FakeGui(None)
check("读不到时不补按", b8._rescue_enter(gui_blind, "define", OURS), False)
check("  一次回车都没按", gui_blind._input.press, 0)

# 确实是我们那条卡在框里：按到消失为止。
gui_stuck_then_gone = FakeGui(None, values=[OURS, OURS, ""])
check("我们那条卡住了就补按", b8._rescue_enter(gui_stuck_then_gone, "define", OURS), True)
check("  按过回车", gui_stuck_then_gone._input.press >= 1, True)

gui_never_goes = FakeGui(None, values=[OURS, OURS, OURS, OURS, OURS, OURS, OURS, OURS])
check("补按到底没发走就如实返回失败",
      b8._rescue_enter(gui_never_goes, "define", OURS), False)
check("  最多按 2 次不乱按", gui_never_goes._input.press, 2)

print("=== 9. 补发前查重（避免同一条发两遍）===")


class RowsDB(FakeDB):
    def __init__(self, rows):
        self.rows = rows

    def username_by_nickname(self, name):
        return {"阿伟": "wxid_friend00007", "define": "wxid_friend00008"}.get(name, "")

    def get_messages(self, username, limit=8):
        return self.rows[:limit]


T = NOW - 60          # 这条进待发送队列的时间
ROW = {"sender_username": SELF, "content": "大概就这意思", "create_time": T + 4}
OLD = {"sender_username": SELF, "content": "大概就这意思", "create_time": T - 600}
THEIRS = {"sender_username": "wxid_other", "content": "大概就这意思", "create_time": T + 4}


def rows_backend(rows):
    b9 = make_backend()
    b9._db = RowsDB(rows)
    return b9


check("库里已有同内容的新行 = 已经发过",
      rows_backend([ROW]).already_delivered("阿伟", "大概就这意思", T), True)
check("同内容但那行比进队列还早 = 不算（是上一条同话）",
      rows_backend([OLD]).already_delivered("阿伟", "大概就这意思", T), False)
check("内容不一样不算",
      rows_backend([{"sender_username": SELF, "content": "懂了就好", "create_time": T + 4}])
      .already_delivered("阿伟", "大概就这意思", T), False)
check("对方发的同内容不算",
      rows_backend([THEIRS]).already_delivered("阿伟", "大概就这意思", T), False)
check("引用/拼接格式（换行后接我们那条）算",
      rows_backend([{"sender_username": SELF, "content": "阿伟：xxx\n大概就这意思",
                     "create_time": T + 4}]).already_delivered("阿伟", "大概就这意思", T), True)
b_bad = make_backend()          # FakeDB 没有 get_messages，不该炸
check("库查不动就返回 False 不抛", b_bad.already_delivered("阿伟", "大概就这意思", T), False)
b_none = make_backend()
b_none._db = None
check("没有库也不抛", b_none.already_delivered("阿伟", "大概就这意思", T), False)

print("=== 表情 / 回复引用（这两类也要回）===")
b = make_backend()
EMOJI = ('<msg><emoji fromusername = "wxid_friend00001" '
         'tousername = "%s" type="2" idbuffer="media:0_0" '
         'md5="7f06da993452781e42f0854a0bd8e748" len = "337108" '
         'cdnurl = "http://vweixinf.tc.qq.com/110/20401/stodownload?m=7f06" /></msg>'
         % SELF)
check("动画表情 → 翻成[表情]并回",
      (capture(b, {"username": "wxid_friend00001", "type": "动画表情",
                   "sender_username": "wxid_friend00001", "create_time": NOW,
                   "content": EMOJI}) or {}).text, "[表情]")

QUOTE = ('<?xml version="1.0"?>\n<msg>\n\t<appmsg>\n\t\t<title>1</title>\n'
         '\t\t<action>view</action>\n\t\t<type>57</type>\n\t\t<refermsg>\n'
         '\t\t\t<type>1</type>\n\t\t\t<displayname>🍊</displayname>\n'
         '\t\t\t<content>唠嗑</content>\n\t\t</refermsg>\n\t</appmsg>\n'
         '\t<fromusername>wxid_friend00001</fromusername>\n</msg>')
m = capture(b, {"username": "wxid_friend00001", "type": "文件/链接/卡片",
                "sender_username": "wxid_friend00001", "create_time": NOW,
                "content": QUOTE})
check("回复引用（type=57）→ 正文是他说的话", (m or {}).text, "1（他引用了「唠嗑」）")
check("回复引用会话名对", (m or {}).chat, "wxid_friend00001")

LINK = ('<?xml version="1.0"?><msg><appmsg><title>拼多多百亿补贴</title>'
        '<type>5</type><url>https://x.cn/a</url></appmsg></msg>')
check("链接卡片不回（没真人话）",
      capture(b, {"username": "wxid_friend00001", "type": "文件/链接/卡片",
                  "sender_username": "wxid_friend00001", "create_time": NOW,
                  "content": LINK}), None)
check("卡片正文坏了也不炸",
      capture(b, {"username": "wxid_friend00001", "type": "文件/链接/卡片",
                  "sender_username": "wxid_friend00001", "create_time": NOW,
                  "content": '<?xml version="1.0"?><msg><appmsg><title>一半'}), None)
check("红包不回",
      capture(b, {"username": "wxid_friend00001", "type": "红包",
                  "sender_username": "wxid_friend00001", "create_time": NOW,
                  "content": "<msg><appmsg><title>恭喜发财</title></appmsg></msg>"}), None)

b = make_backend()
m = capture(b, {"username": "wxid_friend00001", "type": "文件/链接/卡片",
                "sender_username": "wxid_friend00001", "create_time": NOW,
                "content": '<?xml version="1.0"?><msg><appmsg><type>57</type>'
                           '<title><font color="#1f1f1f">在吗</font></title></appmsg></msg>'})
check("回复引用没带被引内容时只留正文（标签剥掉）", (m or {}).text, "在吗")

b = make_backend()
m = capture(b, {"username": "10000000001@chatroom", "type": "动画表情",
                "sender_username": "wxid_friend00001", "create_time": NOW,
                "content": EMOJI})
check("群里发【表情】没 @ 我 → 不算 @ 我", (m or {}).mentioned, False)
check("群里发【表情】会话识别成群", (m or {}).chat_type, "group")

print("\n通过 %d 项，失败 %d 项%s" % (PASS, len(FAIL), ("：" + "、".join(FAIL)) if FAIL else ""))
sys.exit(1 if FAIL else 0)
