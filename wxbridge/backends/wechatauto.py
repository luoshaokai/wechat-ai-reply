"""wechatauto 后端：驱动 Windows 微信 4.1（本机 4.1.15.55）。

这个后端的做法是**读写分家**：

* 读：不碰界面，直接解密本机消息库（SQLCipher 4，密钥从 Weixin.exe 内存里
  抠出来缓存到 ``db.workdir``）。所以微信更新、界面改版都不影响收消息，
  也不需要 UIA 树。
* 发：仍然要点界面（搜索框找到会话 → 粘贴 → 回车）。微信 4.x 默认不把
  mmui 控件树交给 UI Automation，库会在需要时往 ``Weixin.dll`` 里那个
  QAccessible「激活字节」写 1（热激活，不注入不重启）。**这一步要
  OpenProcess(PROCESS_VM_WRITE) 写别的进程的内存，本机微信跑在 High
  Mandatory Level，桥接进程必须以管理员身份运行**；不提权时库会退回
  「坐标 + OCR」发，能发但脆，窗口一挪就废。

监听用的是 ``wechatauto.db.Listener``：增量轮询消息库，水位落盘，重启不重复
推送。也正因为水位会落盘，停机期间攒下的旧消息在下次启动会被一次性倒进来——
这里用 ``MAX_MESSAGE_AGE_SECONDS`` 再拦一道，只回新鲜消息，不然重启一次就
把几天前的话全回一遍。
"""

from __future__ import annotations

import ctypes
import logging
import os
import random
import re
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .base import Backend, InboundMessage, MessageSink

log = logging.getLogger("wxbridge.wechatauto")

#: 联调时合成消息的目标会话：文件传输助手是「自己给自己发」，不打扰任何第三方。
DEMO_CHAT = "文件传输助手"

#: 回复前随机延迟的区间（秒）。秒回是最不像人的特征，也是风控最容易认的模式；
#: 收到 → 想几秒 → 发出去，才像人在打字。下限 0 时等于不延迟（联调用）。
SEND_DELAY_SECONDS = (3.0, 8.0)

#: Unicode 表情的大致范围：国旗、符号与图形、杂项符号、箭头、星星。
#: 范围比「真正的 emoji」宽一点没关系——这些格子里没有常用汉字，
#: 删掉不会把正常聊天删成残句。
_EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"   # 国旗区域
    "\U0001F300-\U0001FAFF"   # 符号与图形（含 ☺🙌😂🎉）
    "\U00002600-\U000027BF"   # 杂项符号、装饰符号（✨✌❤）
    "\U00002B00-\U00002BFF"   # 箭头、星星（⭐⬅）
    "\U00002190-\U000021FF"   # 箭头
    "\U0000FE0F"              # 变体选择符（emoji 尾巴上那个 ️）
    "\U0000200D"              # 零宽连接符（组合 emoji 用）
    "]+"
)

#: 微信自带的方括号表情码。模型有时候会直接写「[裂开]」「[呲牙]」这种，
#: 那是微信客户端自带的表情；默认一并去掉，想保留就改这里。
_WX_FACE_CODES = (
    "微笑", "撇嘴", "色", "发呆", "得意", "流泪", "害羞", "闭嘴", "睡", "大哭",
    "尴尬", "发怒", "调皮", "呲牙", "惊讶", "难过", "抓狂", "吐舌", "偷笑", "愉快",
    "白眼", "委屈", "困", "流汗", "憨笑", "悠闲", "奋斗", "咒骂", "疑问", "嘘",
    "晕", "衰", "骷髅", "敲打", "再见", "擦汗", "抠鼻", "鼓掌", "坏笑", "左哼哼",
    "右哼哼", "哈欠", "强", "弱", "握手", "抱拳", "勾引", "拳头", "OK", "点赞",
    "耶", "吃瓜", "嘿哈", "裂开", "叹气", "捂脸", "机智", "好的", "酸Q", "苦涩",
    "咦", "拜托", "啊", "敲开心", "让我看看", "得意",
)
_WX_FACE_RE = re.compile(
    r"\[(?:%s)\]" % "|".join(re.escape(code) for code in sorted(set(_WX_FACE_CODES)))
)


def _strip_emojis(text: str) -> str:
    """去掉 Unicode 表情和微信方括号表情码。

    :param text: 已经去掉 markdown 的纯文本。
    :returns: 不带任何表情的文本。
    """
    if not text:
        return text
    out = _EMOJI_RE.sub("", text)
    out = _WX_FACE_RE.sub("", out)
    # 表情常紧贴文字，删完会留下多余空格和悬空的括号。
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"^[ \t]+", "", out)
    return out

#: 库报「已尝试发送但结果未确认」时，回读本地消息库确认的总超时与间隔（秒）。
#: 实测回车确实按下去了、消息也落库了（23:07:06 那条），库只是不敢替自己担保，
#: 直接当失败会让应答端误报「发送失败」，白烧一轮模型额度。
#: 微信写库实测有 9~15 秒延迟，窗口给到 25 秒，宁可慢，不要重复发。
CONFIRM_TIMEOUT_SECONDS = 25.0
CONFIRM_POLL_SECONDS = 1.5

#: 库自己回读确认的等待上限（秒）。默认 8 秒太短：微信写库实测有 11~15 秒延迟，
#: 窗口一过库就判「没发出去」，我们再重试一次——同一条消息就在微信里出现两遍。
#: 给足 30 秒，宁可慢，不要重发。
DB_VERIFY_TIMEOUT_SECONDS = 30.0
DB_VERIFY_INTERVAL_SECONDS = 2.0

#: 一次发送最多允许花多久（秒）。点界面走 UIA 自动化，微信卡住的时候库会一直
#: 等下去——实测卡过 4 分半（00:44:44 那次），后面所有回复全堵在发送锁里，
#: 好友那边看着就是「已读不回」。超时就硬退进程，让进程管家把桥接拉起来：
#: 宁可丢一条消息，也不能让整条通道哑掉。
SEND_TIMEOUT_SECONDS = 120.0

#: 超过这个年龄的消息只推进水位、不回复。防止长时间停机后重启把积压旧消息群发一遍。
MAX_MESSAGE_AGE_SECONDS = 120.0

#: 只有这些消息类型直接进上层。
TEXT_TYPES = ("文本",)

#: 这两种没有「正文」可言，要先翻译成人话再进上层（表情和引用也照样回）：
#: - 动画表情：库里只有 cdnurl/md5，认不出是哪个表情，只能回「对方发了个表情」这个事实；
#: - 文件/链接/卡片：local_type=49 是个大杂烩，其中 appmsg/type=57 其实是**引用消息**
#:   （对方真实说的话在 <title> 里，被引用的那条在 <refermsg><content>），不解析就是「已读不回」。
#: 剩下的（图片/语音/视频/红包/链接/小程序/名片）继续不回：库里只有一串密钥和下载地址，
#: 喂给模型只能瞎编，之前就编过「你说的这个链接我打不开」那种鬼话。
RICH_TYPES = ("动画表情", "文件/链接/卡片")

#: 表情给大脑的说法。
EMOJI_TEXT = "[表情]"


def _xml_text(node: Any) -> str:
    """取节点里的纯文本（引用消息的 title 可能带 <font> 之类标签）。"""
    if node is None:
        return ""
    return "".join(node.itertext()).strip()


def describe_rich(mtype: str, content: str) -> str:
    """把非文本消息翻成一句能发给大脑的话。返回空串 = 这条不回。"""
    raw = str(content or "")
    if mtype == "动画表情":
        return EMOJI_TEXT
    if mtype != "文件/链接/卡片" or "<appmsg" not in raw:
        return ""
    try:
        import xml.etree.ElementTree as ET
        appmsg = ET.fromstring(raw).find("appmsg")
    except Exception:  # noqa: BLE001 —— 微信的 XML 经常缺转义，解析不动就不回
        return ""
    if appmsg is None:
        return ""
    if _xml_text(appmsg.find("type")) != "57":
        return ""            # 链接/小程序/商品卡那类，没有真人话，不回
    title = _xml_text(appmsg.find("title"))
    if not title:
        return ""
    refer = appmsg.find("refermsg")
    quoted = re.sub(r"<[^>]+>", "", _xml_text(refer.find("content")) if refer is not None else "").strip()
    return f"{title}（他引用了「{quoted}」）" if quoted else title


#: 系统/机器会话，形状和好友私聊一模一样但不是人，回了就是对着空气说话。
#: ``@openim`` 是订阅号助手那类（本机实测有个「光合小助手」走这个后缀）。
SYSTEM_USERNAMES = ("filehelper", "weixin", "fmessage", "medianote",
                    "brandsessionholder", "brandservicesessionholder")
SYSTEM_PREFIXES = ("gh_",)                 # 公众号
SYSTEM_SUFFIXES = ("@chatroom", "@openim", "@weclaw", "@im.wechat", "@chatbot")

#: 群消息正文的前缀是 ``"<成员wxid>:\\n"``，要剥掉。
GROUP_PREFIX_RE = re.compile(r"^[A-Za-z0-9_@.\-]{6,}:\n")


#: 发送前是否把微信摆到右半屏（固定形状，坐标才稳）。
DOCK_RIGHT = True


def _nap(seconds: float) -> None:
    """两个操作之间喘口气——用库自带的节奏器（它会按人的打字速度排队）。"""
    try:
        from wechatauto import rhythm
        rhythm.nap(seconds)
        return
    except Exception:  # noqa: BLE001
        pass
    time.sleep(seconds)


#: 撞到「120 秒内已写 6 次」之后的冷却秒数（config.yaml 里 send.cooloffSeconds 可改）。
#: 库的自然档是 30~75 秒随机；默认压到 30 秒，是因为冷却太久会把一次发送拖到超时
#: （发送保险丝 120 秒），对方那边看着就是「已读不回」，比多等一会儿更糟。
#: 想更像真人就把这个值调大，或者写 0 用库自带的随机冷却。
COOL_SECONDS = 30.0


def _cool_seconds() -> float:
    """读配置里的冷却秒数；读不到就用 COOL_SECONDS。"""
    try:
        import appconfig
        return max(0.0, float(appconfig.send_options().get("cooloffSeconds")))
    except (TypeError, ValueError):
        return COOL_SECONDS
    except Exception:  # noqa: BLE001
        return COOL_SECONDS


def _tune_rhythm() -> None:
    """把库的拟人节流冷却按配置改（其余节奏参数保持库默认）。

    为什么在这儿改而不是改库源码：库是 pip 装的，升级/重装就没了；在桥接进程
    里改档位参数，升级不受影响，也随时能改回来。写 0 就是不改，用库自己的。
    """
    cool = _cool_seconds()
    if cool <= 0:
        return
    try:
        from wechatauto import rhythm
    except Exception:  # noqa: BLE001
        return
    p = rhythm.profile()
    if tuple(p.cooloff) == (cool, cool):
        return
    try:
        rhythm.configure(cooloff=(cool, cool))
        log.info("拟人节流冷却已设为 %.0f 秒（库默认 %s，档位 %s）", cool, p.cooloff, p.name)
    except Exception as exc:  # noqa: BLE001
        log.warning("改节流冷却失败（仍按库默认 %s）：%s", p.cooloff, exc)


#: 紧急刹车开关文件：存在 = 立刻停止一切界面操作（按下热键时写它）。
PAUSE_FILE = Path(__file__).resolve().parent.parent / "PAUSE"


def paused() -> bool:
    """紧急刹车是不是踩着。"""
    try:
        return PAUSE_FILE.exists()
    except Exception:  # noqa: BLE001
        return False


def screen_locked() -> bool:
    """电脑是不是锁屏了（或远程桌面断开）。

    为什么要判这个：锁屏之后 UIA 点界面会一直挂住，最后被 75 秒保险丝硬退进程，
    那条消息就丢了。宁可先不点，也不要把进程作掉。
    """
    try:
        user32 = ctypes.windll.user32
        # 能打开输入桌面 = 没锁屏；打不开 = 在锁屏/安全桌面
        handle = user32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
        if handle:
            user32.CloseDesktop(handle)
            return False
        return True
    except Exception:  # noqa: BLE001
        return False


def _foreground_hwnd() -> int:
    """当前前台窗口句柄（拿不到就是 0）。"""
    try:
        return int(ctypes.windll.user32.GetForegroundWindow() or 0)
    except Exception:  # noqa: BLE001
        return 0


def _refocus(hwnd: int, tries: int = 1, delay: float = 0.0) -> bool:
    """把前台还给这个窗口。

    发消息必须先抢前台（点界面才能落到微信上），但抢完不还，就是在打断号主
    干活：打字会断、视频会停、游戏会掉出来。这里用 AttachThreadInput 绕过
    前台锁，把焦点还给原来那个窗口——效果像「微信闪了一下又退回去」。

    为什么还要重试几次：微信自己会把窗口重新顶上来（收到消息、发完消息都可能），
    一次还完过 0.3 秒又被打断。所以按 150/400/900 毫秒再确认几下。
    @param hwnd - 发送前记下的前台窗口句柄。
    @param tries - 还几次。
    @param delay - 两次之间等多久。
    @returns 最后一次检查时前台是不是已经回到目标窗口。
    """
    if not hwnd:
        return False
    for _ in range(max(1, tries)):
        try:
            u = ctypes.windll.user32
            if not u.IsWindow(hwnd):
                return False
            if u.GetForegroundWindow() != hwnd:
                fg = u.GetForegroundWindow()
                tid_fg = u.GetWindowThreadProcessId(fg, None) if fg else 0
                tid_t = u.GetWindowThreadProcessId(hwnd, None)
                if tid_fg:
                    u.AttachThreadInput(tid_fg, tid_t, True)
                try:
                    u.SetForegroundWindow(hwnd)
                finally:
                    if tid_fg:
                        u.AttachThreadInput(tid_fg, tid_t, False)
        except Exception:  # noqa: BLE001 - 还不了焦点也别影响发送
            return False
        if delay > 0:
            time.sleep(delay)
    try:
        return ctypes.windll.user32.GetForegroundWindow() == hwnd
    except Exception:  # noqa: BLE001
        return False


def is_machine_chat(username: str) -> bool:
    """这个 username 背后不是真人吗？"""
    if not username:
        return True
    if username in SYSTEM_USERNAMES:
        return True
    if username.startswith(SYSTEM_PREFIXES):
        return True
    # @chatroom 是群，群要过（有 @ 我才回），不是"机器会话"，这里单独放行。
    if username.endswith("@chatroom"):
        return False
    return username.endswith(SYSTEM_SUFFIXES)


def _age_seconds(create_time: Any) -> float:
    try:
        return max(0.0, time.time() - float(create_time))
    except (TypeError, ValueError):
        return 0.0


class WechatAutoBackend(Backend):
    """微信 4.1 后端（wechatauto-replica）。"""

    name = "wechatauto"

    def __init__(
        self,
        interval: float = 1.0,
        max_age: float = MAX_MESSAGE_AGE_SECONDS,
        send_delay: Tuple[float, float] = SEND_DELAY_SECONDS,
        demo_script: Optional[Sequence[Tuple[float, str]]] = None,
    ) -> None:
        self._interval = interval
        self._max_age = max_age
        self._send_delay = tuple(send_delay) if send_delay else (0.0, 0.0)
        self._sink: Optional[MessageSink] = None
        self._db: Any = None
        self._wx: Any = None
        self._listener: Any = None
        self._self_wxid = ""
        self._account = ""
        self._self_names: set = set()          # 自己的备注/昵称，群 @ 判定用
        self._names: Dict[str, str] = {}      # username -> 显示名（备注/昵称/群名）
        self._user_by_name: Dict[str, str] = {}   # 显示名 -> username，发送时用
        self._names_lock = threading.Lock()
        self._send_lock = threading.Lock()    # 发送串行：UI 一次只能服务一个会话
        # 「还焦点」线程的世代号：新一次发送开始就 ++，旧的还焦点线程立刻退场，
        # 免得它在下一条消息打字/回车的时候把焦点抢走（那会让文字留在输入框）。
        self._focus_epoch = 0
        #: 最近一次发送失败的原因（给桥接写「未回」说明用）
        self._last_error = ""
        #: 联调用的合成入站消息 [(延迟秒, 正文), ...]。
        #: 收的是假消息，发出去是真的——回复会真的落到「文件传输助手」，
        #: 用来在没有真人发消息时验证「模型→桥接→微信」整条链路。
        self._demo_script = list(demo_script or [])
        self._demo_stop = threading.Event()
        #: 通用退出信号：等待发送延迟时用它能做到「一停就醒」，不是干等满 8 秒。
        self._stop = threading.Event()

    # ------------------------------------------------------------------ 生命周期

    def start(self, sink: MessageSink) -> None:
        self._sink = sink
        threading.Thread(target=self._run, name="wechatauto", daemon=True).start()

    def stop(self) -> None:
        self._demo_stop.set()
        self._stop.set()
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:  # noqa: BLE001 - 退出路径不纠结
                pass
            self._listener = None

    def account(self) -> str:
        return self._account

    # ------------------------------------------------------------------ 主流程

    def _run(self) -> None:
        try:
            from wechatauto.db import WeChatDB, Listener
        except Exception as exc:  # noqa: BLE001
            log.error("导入 wechatauto 失败（pip install wechatauto-replica）：%s", exc)
            return

        try:
            # 首次构造要从微信进程里抠库密钥，慢，放在线程里。
            self._db = WeChatDB()
            info = self._db.get_self_info() or {}
            self._self_wxid = str(info.get("username") or "")
            self._account = str(info.get("remark") or info.get("nick_name") or "")
            self._self_names = {
                str(info.get("remark") or "").strip(),
                str(info.get("nick_name") or "").strip(),
                self._account,
            } - {""}
            log.info("消息库已解密，当前账号：%s（%s）", self._account or "(未知)", self._self_wxid)
        except Exception as exc:  # noqa: BLE001
            log.error("打开本地消息库失败：%s", exc)
            return

        # 发送侧（UIA）。连不上也不致命：收消息照跑，发的时候再重试。
        self._connect_ui()

        # 会话名先缓存一遍，消息进来时不用每条查库。
        try:
            for s in self._db.get_sessions(limit=500):
                u = s.get("username")
                if u:
                    self._resolve_name(u)
        except Exception as exc:  # noqa: BLE001
            log.warning("预载会话名失败：%s", exc)

        try:
            self._listener = Listener(self._db, interval=self._interval)
            self._listener.add_all(self._on_message, discover=True)
            self._listener.start()
            log.info("已开始监听全部会话（增量轮询消息库）")
        except Exception as exc:  # noqa: BLE001
            log.error("启动监听失败：%s", exc)

        if self._demo_script:
            threading.Thread(target=self._run_demo, name="wechatauto-demo", daemon=True).start()

    def _run_demo(self) -> None:
        """合成入站消息：联调用。回复走真实微信链路。"""
        for delay, text in self._demo_script:
            if self._demo_stop.wait(delay):
                return
            if self._sink is None:
                return
            log.info("[联调] 合成一条入站消息：%s", text)
            self._sink(
                InboundMessage(
                    chat=DEMO_CHAT,
                    chat_type="private",
                    sender=DEMO_CHAT,
                    text=text,
                    mentioned=False,
                    ts=int(time.time() * 1000),
                )
            )

    def _connect_ui(self) -> None:
        """建 UIA 会话对象。需要写 gate 字节，非管理员时库会退回 OCR 发送。"""
        if self._wx is not None:
            return
        try:
            from wechatauto import WeChat
            from wechatauto.param import WxParam

            _tune_rhythm()
            # 库默认只等 8 秒就判「没发出去」，微信写库实测要 11~15 秒。
            # 窗口不够 → 误判 → 我们重发 → 同一条在微信里出现两遍。
            WxParam.DB_VERIFY_TIMEOUT = DB_VERIFY_TIMEOUT_SECONDS
            WxParam.DB_VERIFY_INTERVAL = DB_VERIFY_INTERVAL_SECONDS

            self._wx = WeChat()
            log.info("已连上微信窗口：%s", getattr(self._wx, "nickname", "") or "(昵称未知)")
            self._share_db_with_ui()
        except Exception as exc:  # noqa: BLE001
            self._wx = None
            log.warning("连接微信窗口失败（发送会不可用，稍后自动重试）：%s", exc)

    def _share_db_with_ui(self) -> None:
        """让发送侧的 GUI 也用监听侧那一个数据库实例。

        库自己会在发送校验时**再建一个** WeChatDB 实例。两个实例指向同一个
        解密缓存文件（%TEMP%\\wechatauto_db\\...\\message__message_0.db），刷新时
        要 os.replace 覆盖它——Windows 上只要另一个实例还开着这个文件，替换
        就报 PermissionError（WinError 5），缓存停在旧数据上，于是「我这条发
        出去没有」永远确认不了（实测 23:38 那条）。

        所以这里直接把 GUI 的取库方法换掉，两边共用一个实例、一个连接。
        """
        gui = getattr(self._wx, "_gui", None)
        if gui is None or self._db is None:
            return
        shared = self._db

        def _get_db():  # noqa: ANN202 - 替换库的方法，签名照原样
            return shared

        try:
            gui._get_db = _get_db
            log.info("发送侧与监听侧共用同一个数据库实例（避免缓存文件抢锁）")
        except Exception as exc:  # noqa: BLE001
            log.warning("共用数据库实例失败：%s", exc)

    # ------------------------------------------------------------------ 名字

    def _resolve_name(self, username: str) -> str:
        """username -> 界面显示名，同时记下反查表（显示名 -> username）。"""
        with self._names_lock:
            hit = self._names.get(username)
            if hit:
                self._user_by_name.setdefault(hit, username)
                return hit
        try:
            name = self._db.get_nickname(username) or username
        except Exception:  # noqa: BLE001
            name = username
        with self._names_lock:
            self._names[username] = name
            self._user_by_name.setdefault(name, username)
        return name

    def _target_of(self, chat: str) -> str:
        """上层回传的会话标识 -> ``SendMsg`` 能认出的目标。

        和读库正好相反，**发送必须用显示名**：wechatauto 的发送是点界面
        （在搜索框里敲 ``who`` 等结果），实测 ``who="filehelper"`` 重试 142 秒
        报「会话没打开」，``who="文件传输助手"`` 2.3 秒就发出去了。wxid、群号
        搜不出来，所以只有上层传来的是 username 时才翻成显示名。
        """
        text = (chat or "").strip()
        if not text:
            return ""
        is_username = (
            text.endswith("@chatroom")
            or text.startswith(("wxid_", "gh_"))
            or text in SYSTEM_USERNAMES
        )
        if not is_username:
            return text                     # 本来就是显示名，原样用
        name = self._resolve_name(text)
        return name or text

    # ------------------------------------------------------------------ 收消息

    def _on_message(self, msg: dict, _listener: Any = None) -> None:
        """Listener 回调。抛出去的消息才会进桥接；不合适的直接吃掉（水位照样推进）。"""
        username = str(msg.get("username") or "")
        sender_wxid = str(msg.get("sender_username") or "")
        mtype = str(msg.get("type") or "")

        # 1) 自己发的不回。机器人回自己 = 无限循环，这条最要命。
        if sender_wxid and sender_wxid == self._self_wxid:
            return
        # 2) 系统号、公众号、订阅号助手那类机器会话，以及文件传输助手。
        if is_machine_chat(username):
            return
        # 3) 文本直接用；表情/引用先翻成人话；其余类型（图片语音红包链接）不吃。
        if mtype in TEXT_TYPES:
            body = str(msg.get("content") or "")
        elif mtype in RICH_TYPES:
            body = describe_rich(mtype, str(msg.get("content") or ""))
            if not body:
                return
        else:
            return
        # 4) 太旧的不回（停机期间攒的、或水位文件里落后的）。
        if _age_seconds(msg.get("create_time")) > self._max_age:
            return

        chat_type = "group" if username.endswith("@chatroom") else "private"
        text = body.strip()
        if chat_type == "group" and sender_wxid:
            # 群消息正文实测带 "<成员wxid>:\n" 前缀。先精确剥这一条，剥不掉再退
            # 回正则——私聊正文没有这个前缀，而且像「备注:xxx」这种开头会被一刀切
            # 的正则误伤，所以只在群里动刀。
            prefix = sender_wxid + ":\n"
            if text.startswith(prefix):
                text = text[len(prefix):]
            else:
                text = GROUP_PREFIX_RE.sub("", text, count=1)
        text = text.strip()
        if not text:
            return

        chat = self._resolve_name(username)
        sender = self._resolve_name(sender_wxid) if sender_wxid else chat

        mentioned = self._mentioned(text) if chat_type == "group" else False

        message = InboundMessage(
            chat=chat,
            chat_type=chat_type,
            sender=sender,
            text=text,
            mentioned=mentioned,
            ts=int(float(msg.get("create_time") or 0) * 1000),
            raw=msg,
        )
        if self._sink is not None:
            try:
                self._sink(message)
            except Exception:  # noqa: BLE001
                log.exception("投递消息失败")

    def _mentioned(self, text: str) -> bool:
        """群里是否 @ 了我。

        不能靠列举分隔符：微信在 @昵称 后面塞的是 U+2005 这类不可见空白，版本间还不一
        样，白名单会漏。反过来用「后向否定」更稳 —— @昵称 之后不能再跟字母数字或汉字，
        跟上说明匹配到的是别人的长名字（我叫「登」却命中「@登录」。昵称本身是 emoji
        时这条规则也成立，因为 emoji 不在 \\w 里，边界天然是「非文字」。
        """
        for name in self._self_names:
            pattern = re.escape("@" + name) + r"(?![0-9A-Za-z_\u4e00-\u9fff])"
            if re.search(pattern, text):
                return True
        return False

    # ------------------------------------------------------------------ 焦点/遮挡

    #: 这些窗口不用收：系统外壳、输入法、UWP 容器
    _SKIP_CLASSES = (
        "Progman", "WorkerW", "Shell_TrayWnd", "MSCTFIME UI", "IME",
        "ApplicationFrameWindow", "Shell_SecondaryTrayWnd",
    )

    @staticmethod
    def _window_pid(hwnd: int) -> int:
        try:
            pid = wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return int(pid.value)
        except Exception:  # noqa: BLE001
            return 0

    def _focus_wechat(self, gui: Any) -> List[int]:
        """把微信真正摆到最前面，挡在它上面的窗口先收起来（发完还原）。

        为什么自己来一遍：库自带的 ensure_visible() 只收「有标题或 Chrome 类」的
        顶层窗口，有些程序的界面（Electron 那类）会漏掉。于是鼠标点击按 Z 序落到
        别人头上——日志里的「落点窗口不属于微信（进程 6352 != 45096）」就是
        这么来的，用户那边的现象就是「鼠标瞎点、消息没发出去」。

        这里按「跟微信窗口重叠 + 可见 + 不是微信」自己枚举一遍，先最小化，
        再把微信置顶，发完再把收起来的窗口还原回去。
        """
        minimized: List[int] = []
        try:
            user32 = ctypes.windll.user32
            main_hwnd = getattr(gui, "main_hwnd", None)
            if not main_hwnd:
                return minimized
            rect = wintypes.RECT()
            user32.GetWindowRect(main_hwnd, ctypes.byref(rect))
            wx_rect = (rect.left, rect.top, rect.right, rect.bottom)
            if wx_rect[2] - wx_rect[0] < 10 or wx_rect[3] - wx_rect[1] < 10:
                return minimized
            wx_pid = self._window_pid(main_hwnd)
            handles: List[int] = []

            @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            def _cb(h: int, _lp: int) -> bool:
                handles.append(int(h))
                return True

            user32.EnumWindows(_cb, 0)
            for h in handles:
                if h == main_hwnd:
                    continue
                if not user32.IsWindowVisible(h) or user32.IsIconic(h):
                    continue
                cls_buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(h, cls_buf, 256)
                cls = cls_buf.value
                if cls in self._SKIP_CLASSES or cls.startswith("Windows.UI.Core"):
                    continue
                if wx_pid and self._window_pid(h) == wx_pid:
                    continue
                r2 = wintypes.RECT()
                user32.GetWindowRect(h, ctypes.byref(r2))
                if not (r2.right > wx_rect[0] and r2.left < wx_rect[2]
                        and r2.bottom > wx_rect[1] and r2.top < wx_rect[3]):
                    continue
                user32.ShowWindow(h, 6)  # SW_MINIMIZE
                minimized.append(h)
            if minimized:
                log.info("收起了 %d 个挡着微信的窗口（发完还原）", len(minimized))
            try:
                gui.bring_to_front(keep_topmost=True)
            except Exception:  # noqa: BLE001 - 置顶失败也照样发
                pass
        except Exception as exc:  # noqa: BLE001
            log.debug("整理窗口时出错：%s", exc)
        return minimized

    @staticmethod
    def _restore_windows(handles: List[int]) -> None:
        """把 _focus_wechat 收起来的窗口还原（不抢前台）。"""
        if not handles:
            return
        try:
            user32 = ctypes.windll.user32
            for h in handles:
                if user32.IsWindow(h) and user32.IsIconic(h):
                    user32.ShowWindow(h, 4)  # SW_SHOWNOACTIVATE
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ 发消息

    def _hand_back(self, prev_fg: int, gui: Any, blocked: List[int], epoch: int = 0) -> List[int]:
        """发完立刻把屏幕还回去：撤置顶、还原被最小化的窗口、把前台还给原来的窗口。

        @param prev_fg - 发送前的前台窗口句柄。
        @param gui - 微信 GUI 对象（可能为 None）。
        @param blocked - 这次收起来的窗口。
        @returns 已经还原，返回空列表（调用方别再重复还原）。
        """
        if gui is not None:
            try:
                gui.restore_zorder()
            except Exception:  # noqa: BLE001
                pass
        self._restore_windows(blocked)
        # 还焦点这件小事放到后台做：按 0/0.15/0.4/1/2/3.5 秒确认几次。
        # 为什么要拉这么长：这个库自己有后台线程会在校准时把微信顶到前台
        # （guia.py 的校准探针里就有一句 bring_to_front），一次还完过两秒又被顶上来，
        # 号主那边还是被打断。多确认几次，等它的后台线程干完就稳了。
        if prev_fg:
            def _keeper() -> None:
                fought = 0
                try:
                    for i, wait in enumerate((0.0, 0.15, 0.25, 0.6, 1.0, 1.5)):
                        if wait:
                            time.sleep(wait)
                        # 又有新的发送开始了：立刻收手，别把焦点从微信抢走。
                        if epoch and self._focus_epoch != epoch:
                            log.debug("有新消息在发，跳过还焦点")
                            return
                        if not _refocus(prev_fg, tries=1):
                            fought += 1
                    if fought:
                        log.debug("还焦点时被打断 %d 次（微信自己顶上来），已拉回", fought)
                except Exception:  # noqa: BLE001
                    pass

            threading.Thread(target=_keeper, daemon=True).start()
        return []

    def _wechat_hung(self) -> bool:
        """微信窗口是不是「无响应」（原生判断，毫秒级）。

        这是最根上的原因：微信 4.x 的 UI 线程一卡，任何 UIA/COM 调用都会
        一起阻塞——看起来就是「打开会话卡住」「发送卡住」，最后被保险丝
        硬退。这里用 IsHungAppWindow 直接问系统，不用去戳微信。
        """
        try:
            gui = getattr(getattr(self, "_wx", None), "_gui", None)
            hwnd = int(getattr(gui, "main_hwnd", 0) or 0)
            if not hwnd:
                return False
            return bool(ctypes.windll.user32.IsHungAppWindow(hwnd))
        except Exception:  # noqa: BLE001
            return False

    def _restart_wechat(self) -> bool:
        """微信卡死时把它重启（同一时间只做一次，冷却 10 分钟）。"""
        now = time.time()
        if now - getattr(self, "_last_wechat_restart", 0.0) < 600:
            log.warning("微信又卡了，但 10 分钟内已经重启过一次，这次只记录不重启")
            return False
        self._last_wechat_restart = now
        exe = r"C:\Program Files\Tencent\Weixin\Weixin.exe"
        log.warning("微信无响应，自动重启它（只重启微信，不动聊天记录）")
        try:
            import subprocess
            subprocess.run(["taskkill", "/IM", "Weixin.exe", "/F"],
                           capture_output=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as exc:  # noqa: BLE001
            log.warning("结束微信失败：%s", exc)
        time.sleep(3.0)
        try:
            import subprocess
            subprocess.Popen([exe], close_fds=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as exc:  # noqa: BLE001
            log.warning("启动微信失败：%s", exc)
            return False
        for _ in range(40):
            time.sleep(1.0)
            try:
                import winproc
                if winproc.pids_by_name(["Weixin.exe"]):
                    time.sleep(6.0)   # 等它把窗口和消息库准备好
                    self._wx = None
                    self._connect_ui()
                    return True
            except Exception:  # noqa: BLE001
                continue
        log.warning("等微信起来超时")
        return False

    def _close_standalone_chat_windows(self) -> int:
        """关掉微信的「独立聊天窗口」（mmui::ChatSingleWindow）。

        独立窗是微信 4.x 把某个会话单独弹出来的小窗。一旦它开着，
        主窗口的搜索框就不在界面里，Ctrl+F 没反应、库的 open_chat 也
        只能干等——这就是「定位不到会话、一直卡住」的真正原因。
        """
        try:
            u = ctypes.windll.user32
            gui = getattr(getattr(self, "_wx", None), "_gui", None)
            want_pid = int(getattr(gui, "pid", 0) or 0)
            closed = 0

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def cb(hwnd, _lparam):
                nonlocal closed
                buf = ctypes.create_unicode_buffer(256)
                u.GetClassNameW(hwnd, buf, 256)
                if buf.value == "mmui::ChatSingleWindow":
                    pid = wintypes.DWORD()
                    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if not want_pid or pid.value == want_pid:
                        u.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE
                        closed += 1
                return True

            u.EnumWindows(cb, 0)
            if closed:
                time.sleep(0.5)
                log.info("关掉了 %d 个独立聊天窗口（它们会挡住搜索框）", closed)
            return closed
        except Exception as exc:  # noqa: BLE001
            log.debug("关独立窗失败：%s", exc)
            return 0

    def _timed(self, label: str, fn: Any, limit: float) -> "tuple[Any, bool]":
        """限时跑一步并记录耗时——卡住时能立刻知道是哪一步卡的。"""
        box: dict = {"value": None}

        def work() -> None:
            try:
                box["value"] = fn()
            except Exception as exc:  # noqa: BLE001
                log.warning("%s 抛错：%s", label, exc)

        t0 = time.time()
        log.info("▶ %s", label)
        th = threading.Thread(target=work, daemon=True)
        th.start()
        th.join(limit)
        dt = time.time() - t0
        if th.is_alive():
            log.warning("✗ %s 超过 %.0f 秒没返回（卡在这一步），放弃", label, limit)
            return None, True
        log.info("✔ %s 用时 %.1fs", label, dt)
        return box["value"], False

    def _current_chat_name(self, gui: Any) -> str:
        name, _ = self._timed("读当前会话名",
                              lambda: self._read_current_chat_name(gui), 6.0)
        return str(name or "").strip()

    def _read_current_chat_name(self, gui: Any) -> str:
        try:
            uia = gui._get_uia() if hasattr(gui, "_get_uia") else None
            if uia is not None and getattr(uia, "current_chat", None):
                return str(uia.current_chat() or "").strip()
        except Exception:  # noqa: BLE001
            pass
        return str(getattr(gui, "_current_chat", "") or "").strip()

    def _open_chat_by_uia(self, gui: Any, target: str) -> bool:
        """用 UIA 打开会话：Ctrl+F 让搜索框现身 → 粘贴名字 → 直接点结果项。

        为什么不用库自带的 open_chat：右半屏的竖长布局里，微信的搜索框默认是
        **折叠**的，库靠坐标点放大镜去展开，窄窗口里那一点击偏了，搜索框就
        永远不出现在控件树里（_search_box 返回 None），接着 _collect_results
        就会空转到超时——表现就是「一直卡在搜索、不点对」。
        键盘 Ctrl+F 是确定可靠的，出来之后搜索框和结果列表都能被 UIA 认出来，
        点结果项也不需要任何坐标。
        """
        try:
            uia = gui._get_uia()
        except Exception:  # noqa: BLE001
            uia = None
        if uia is None:
            return False
        if self._current_chat_name(gui) == target:
            return True
        try:
            self._close_standalone_chat_windows()
            self._focus_wechat(gui)
            time.sleep(0.25)
            # 窗口可能被重建过（微信重启、窗口重开），旧句柄取不到搜索框——
            # 先刷新，再用 Ctrl+F 把折叠的搜索框唤出来（多试几次，它进树有点慢）。
            try:
                uia.ensure_window()
            except Exception:  # noqa: BLE001
                pass
            # 先给 win 一个初值：下面「取搜索框」的 lambda 会读它，
            # 万一循环里第一次 ensure_window() 抛异常也不会有未定义变量。
            win = getattr(uia, "_win", None)
            box = None
            for _ in range(4):
                # 每轮都重新刷新窗口句柄：微信重启、独立聊天窗关掉之后，
                # 旧 win 一直在树外面，_search_box 只会「立刻返回 None」——
                # 表现为连着四次「✔ 取搜索框 用时 0.0s」但其实一次都没取到。
                try:
                    uia.ensure_window()
                    win = uia._win
                except Exception:  # noqa: BLE001
                    pass
                gui._input.key(0x46, ctrl=True)    # Ctrl+F：展开搜索框
                time.sleep(0.5)
                _nap(0.4)
                found, _ = self._timed("取搜索框",
                                       lambda: uia._search_box(win, expand=False), 5.0)
                if found is not None:
                    box = found
                    break
            if box is None:
                # 这里**不能**再往「当前焦点」粘名字。以前的兜底是
                # Ctrl+A + Delete + Ctrl+V：焦点要是落在聊天输入框上，
                # 就把号主没发出去的草稿整段删掉，再把「文件传输助手」
                # 这种会话名留在框里，下一次回车直接发给真人。
                # 拿不到搜索框控件 = 无法确认字进的是哪个框，宁可放弃这条
                # （上层会进待发送队列，界面好了自动补），也不瞎打。
                log.warning("取不到搜索框（Ctrl+F 唤不出来），这条不进界面，交给待发送队列")
                try:
                    gui._input.key(0x1B)   # ESC：把半开的搜索面收掉，别影响下一条
                    time.sleep(0.2)
                except Exception:  # noqa: BLE001
                    pass
                return False
            # 搜索框用 UIA 直接设内容：比剪贴板+焦点稳，
            # 上一轮残留的搜索文字也不会串到聊天输入框里。
            try:
                uia._set_text(box, target)
            except Exception as exc:  # noqa: BLE001
                log.warning("设置搜索内容失败：%s", exc)
            _nap(0.5)
            time.sleep(0.5)
            results, _ = self._timed("搜索结果", lambda: uia._collect_results(target, settle=3.0), 8.0)
            results = results or []
            if not results:
                log.warning("搜索「%s」没有结果项", target)
                return False
            exact = [r for r in results if r.get("name") == target]
            contacts = [r for r in exact if r.get("section") == "联系人"]
            pick = (contacts or exact or results)[0]
            uia._click_ctrl(pick["cell"])
            _nap(0.6)
            time.sleep(0.6)
            name = self._current_chat_name(gui)
            ok = (name == target)
            log.info("UIA 定位「%s」→ 当前会话「%s」（%s）", target, name,
                     "已切换" if ok else "没切过去")
            return ok
        except Exception as exc:  # noqa: BLE001
            log.warning("UIA 定位失败：%s", exc)
            return False

    def _send_via_uia(self, gui: Any, target: str, text: str) -> bool:
        """UIA 发送：定位会话 → 往输入框粘贴正文（回读校验）→ 回车。"""
        if not self._open_chat_by_uia(gui, target):
            return False
        try:
            uia = gui._get_uia()
            ok = bool(uia.send_text(text))

            if ok:
                log.info("UIA 发送成功（%s）", target)
            else:
                log.warning("UIA 发送没成功（%s），内容可能没进输入框", target)
            return ok
        except Exception as exc:  # noqa: BLE001
            log.warning("UIA 发送异常：%s", exc)
            return False
    def _dock_wechat_right(self, gui: Any) -> bool:
        """把微信窗口摆到屏幕右半边（固定形状），再开始点。

        为什么要这么做：库的坐标/侧栏比例是按窗口尺寸算的，窗口一旦被拖动、
        贴边、最大化，坐标就全偏了——「定位不准」的根源。与其每次去猜，
        不如每次发送前把窗口摆成同一个形状：右半屏、固定宽高。
        @returns 是否真的挪动过（挪过就需要重新校准）。
        """
        if not DOCK_RIGHT:
            return False
        try:
            hwnd = int(getattr(gui, "main_hwnd", 0) or 0)
            if not hwnd:
                return False
            u = ctypes.windll.user32
            if u.IsZoomed(hwnd):
                u.ShowWindow(hwnd, 9)      # SW_RESTORE：先退出最大化
                time.sleep(0.2)
            work = wintypes.RECT()
            # SPI_GETWORKAREA = 0x0030：拿到去掉任务栏的可用桌面
            if not u.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0):
                return False
            width = (work.right - work.left) // 2
            height = work.bottom - work.top
            x = work.left + width
            y = work.top
            cur = wintypes.RECT()
            u.GetWindowRect(hwnd, ctypes.byref(cur))
            same = (abs(cur.left - x) <= 8 and abs(cur.top - y) <= 8
                    and abs((cur.right - cur.left) - width) <= 12
                    and abs((cur.bottom - cur.top) - height) <= 12)
            if same:
                return False
            u.SetWindowPos(hwnd, 0, x, y, width, height, 0x0004 | 0x0040)  # NOZORDER|SHOWWINDOW
            time.sleep(0.6)
            log.info("把微信摆到右半屏：%dx%d @ (%d,%d)", width, height, x, y)
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("摆窗口失败（不影响发送）：%s", exc)
            return False

    def _preflight_ui(self, gui: Any) -> None:
        """点会话之前先把界面「归位」：前台、关掉搜索/弹窗、回到聊天页。

        微信被搜过、弹过窗、停在朋友圈页之后，open_chat 会一直卡住不返回
        （实测超过 15 秒），最后被保险丝硬退。先按两下 Esc、回聊天页，
        能省掉绝大多数卡死。
        """
        try:
            self._focus_wechat(gui)
        except Exception:  # noqa: BLE001
            pass
        try:
            uia = gui._get_uia() if hasattr(gui, "_get_uia") else None
            for _ in range(2):
                gui._input.key(0x1B)   # ESC
                time.sleep(0.15)
            if uia is not None and hasattr(uia, "back_to_chat_tab"):
                uia.back_to_chat_tab()
            time.sleep(0.2)
        except Exception as exc:  # noqa: BLE001
            log.debug("界面归位失败（不影响继续）：%s", exc)

    def _call_with_timeout(self, fn: Any, limit: float) -> "tuple[bool, bool]":
        """限时执行一个可能卡住的库调用。返回 (结果, 是否超时)。"""
        box = {"ok": False}

        def work() -> None:
            try:
                box["ok"] = bool(fn())
            except Exception as exc:  # noqa: BLE001
                log.debug("限时调用抛错：%s", exc)
                box["ok"] = False

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        thread.join(limit)
        if thread.is_alive():
            return False, True
        return bool(box["ok"]), False

    def _open_with_timeout(self, gui: Any, target: str, exact: bool,
                           limit: float = 14.0) -> bool:
        """打开会话最多等 limit 秒。卡住就放弃这一轮，绝不拖到 75 秒被硬退。"""
        result = {"ok": False}

        def work() -> None:
            try:
                result["ok"] = bool(gui.open_chat(target, exact=exact))
            except Exception as exc:  # noqa: BLE001
                log.debug("open_chat(exact=%s) 抛错：%s", exact, exc)
                result["ok"] = False

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        thread.join(limit)
        if thread.is_alive():
            log.warning("打开会话「%s」超过 %.0f 秒没返回（界面卡住），这一轮放弃", target, limit)
            return False
        return bool(result["ok"])

    def _chat_open_verified(self, gui: Any, target: str) -> bool:
        """会话是不是真打开了（不信 open_chat 的返回值）。

        三种验证，能确认一种就算开：
        1. 库自己的 _chat_open_confirmed（点了并确认渲染）；
        2. UIA 的当前会话名 == 目标；
        3. 库记录的 _current_chat == 目标。
        """
        try:
            confirm = getattr(gui, "_chat_open_confirmed", None)
            if callable(confirm) and confirm(target):
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            uia = gui._get_uia() if hasattr(gui, "_get_uia") else None
            if uia is not None and getattr(uia, "current_chat", None):
                if str(uia.current_chat() or "").strip() == target:
                    return True
        except Exception:  # noqa: BLE001
            pass
        return str(getattr(gui, "_current_chat", "") or "") == target

    def _chat_exists(self, target: str) -> bool:
        """微信里有没有这个会话（按界面显示名查消息库）。查不到就返回 False。

        注意：查不到不等于一定没有（库可能还没拿到最新昵称），所以调用方
        只在明确拿不到任何对应微信号时才拦。
        """
        try:
            if target in getattr(self, "_user_by_name", {}):
                return True
            resolved = self._db.username_by_nickname(target) if self._db is not None else None
            return bool(resolved)
        except Exception:  # noqa: BLE001 - 查不了就放行，交给后面的流程
            return True

    def _ui_stale(self) -> bool:
        """绑定的微信窗口是不是已经过期了（微信重启/升级之后就会这样）。

        表现：日志里一直刷「热激活 UIA 失败：PID xxx 未找到 Weixin.dll」，
        点击落点不属于微信（进程对不上），发送全部失效、消息不回。
        """
        wx = self._wx
        if wx is None:
            return True
        gui = getattr(wx, "_gui", None)
        if gui is None:
            return True
        try:
            if not gui.is_alive():
                return True
            pid = int(getattr(gui, "pid", 0) or 0)
        except Exception:  # noqa: BLE001
            return True
        if not pid:
            return True
        try:
            import winproc
            alive = winproc.pids_by_name(["Weixin.exe"])
        except Exception:  # noqa: BLE001
            return False
        if alive and pid not in alive:
            log.warning("微信窗口换了（绑定 pid=%s，现在的微信 pid=%s），重新连接",
                        pid, ",".join(str(p) for p in alive))
            return True
        return False

    def _ours_in_box(self, gui: Any, text: str, chat: str = "") -> Optional[bool]:
        """输入框里那串字，是不是「我们刚发的那条」还没出去。三态：

        - True  框里还留着这条 = 真没发出去（可以放心补按回车）
        - False 框里没这条了 = 已经发出去了
        - None  读不到（UIA 不可用/控件拿不到）= 没把握，谁也别乱猜

        以前这里用的是库自带的 ``_input_box_has_text()``——它不读文字，是数输入框
        区域的深色像素，超过 20 个就算「还有字」。于是光标、输入法候选、框坐标漂到
        聊天区（那里全是黑字）都会误判成没发出去。实测 03:38~03:41 对着 define 连中
        11 次，可消息库里 10 次都查到了：字早发出去了，纯粹是判据读错。

        误判本身只是慢几秒，真正的问题是它后面那两下补按回车：万一那一刻框里是号主
        自己打了一半的草稿，我们就替他发出去了。所以现在改成读编辑控件的**真实文本**
        跟我们发的那条比对——对得上才认「真没发出去」。
        @param gui - 微信 GUI 对象。
        @param text - 刚发出去的那串正文。
        @param chat - 会话名。读回来正好等于会话名时说明抓错控件（实测抓到过
            '文件传输助手'），这种情况报 None，不能当成「已经发走了」。
        @returns 上面三种情况之一。
        """
        want = (text or "").strip()
        if not want:
            return None
        try:
            uia = gui._get_uia()
            ctrl = uia._chat_input() if uia is not None else None
            # 控件都拿不到（会话没打开/编辑框还没建好）不等于「我们那条发走了」，
            # 必须报成没把握，否则调用方会当成已发送，真漏发就查不出来了。
            if ctrl is None:
                return None
            pattern = ctrl.GetValuePattern()
            if pattern is None:
                return None
            got = str(getattr(pattern, "Value", None) or "").strip()
        except Exception as exc:  # noqa: BLE001 - 读不到就返回 None，让调用方兜底
            log.debug("读输入框文本失败：%s", type(exc).__name__)
            return None
        if chat and got == str(chat).strip() and want != got:
            log.debug("读回来的是会话名「%s」，说明抓到的不是输入框，当没把握", got)
            return None
        return got == want or want in got

    def _rescue_enter(self, gui: Any, target: str, text: str) -> bool:
        """文字还留在输入框里（回车没生效）时，补按一次回车。

        判断依据换成 ``_ours_in_box``：框里那串必须就是我们刚发的这条，
        补按才是安全的（不会替号主把他写到一半的话发出去）。读不到也不补按，
        交给消息库确认和待发送队列。
        不重新输入文字——重输会把两句拼在一起，还可能重复发送。
        @param gui - 微信 GUI 对象。
        @param target - 会话名（只用于日志）。
        @param text - 刚发的那串正文。
        @returns 补按之后我们那条是否从框里消失了（= 发出去了）。
        """
        try:
            if self._ours_in_box(gui, text, target) is not True:
                return False
            log.warning("回车没生效，刚发的那条还留在输入框里（%s），重新聚焦后再按一次", target)
            self._focus_wechat(gui)
            time.sleep(0.35)
            for _ in range(2):
                gui._input.key(0x0D)          # VK_RETURN
                for _ in range(4):
                    time.sleep(0.45)
                    if self._ours_in_box(gui, text, target) is False:
                        log.info("补按回车成功（%s）", target)
                        return True
            if self._ours_in_box(gui, text, target) is None:
                log.warning("补按回车后读不到输入框（%s），交给消息库确认", target)
            else:
                log.warning("补按回车也没把我们那条从输入框里发走（%s）", target)
            return False
        except Exception as exc:  # noqa: BLE001
            log.debug("补按回车出错：%s", exc)
            return False

    @staticmethod
    def _watchdog(seconds: float, tag: str) -> threading.Event:
        """发送卡死时的保险丝：到点直接退进程，让进程管家把桥接拉起来。

        不能"放弃这一条继续发下一条"：UIA 的点击已经在微信窗口里排着队，
        半途放弃等于让后面的点击落到别的窗口上去——就是「鼠标瞎点」那回事。
        整个进程一起退，界面状态由微信自己收拾，干净。
        @param {number} seconds - 允许的最长秒数。
        @param {string} tag - 出事时写日志用的会话名。
        @returns {threading.Event} 发送结束时 set 一下就取消保险丝。
        """
        done = threading.Event()

        def _kill() -> None:
            if done.wait(seconds):
                return
            log.error("发送卡住超过 %.0fs，硬退进程（%s），等进程管家重启", seconds, tag)
            os._exit(3)

        threading.Thread(target=_kill, daemon=True).start()
        return done

    def can_send_now(self) -> bool:
        """现在适不适合点界面（给待发送队列的自动补发用，纯探测不发）。

        只做 send 里那几项「快速安全门」：紧急刹车 / 锁屏 / 微信无响应。
        这三个成立时，补发也一样会卡死或乱点，所以直接判「现在别补」。
        """
        if paused():
            return False
        if screen_locked():
            return False
        if self._wechat_hung():
            return False
        return True

    def send(self, chat: str, text: str) -> bool:
        _tune_rhythm()      # 每条都对一遍节流档位，别让它被别的入口改回去
        text = self._tidy(text)
        if not text:
            log.info("净化后正文为空，跳过发送：%s", chat)
            return False
        # 把真正要打进微信输入框的那行字记下来：应答端日志记的是模型原文，
        # 净化发生在这一步，只看应答端日志会以为表情漏出去了。
        log.info("发送正文：%s", text)
        target = self._target_of(chat)
        if paused():
            log.info("紧急刹车生效中（PAUSE 文件在），这条不进界面：%s", chat)
            self._last_error = "paused"
            return False
        if self._wechat_hung():
            # 微信卡住时**绝不能自动杀掉它**：号主可能正在用，杀了就是「人工动不了」。
            # 记下来、这条进待发送队列，等他手动处理。
            log.warning("微信无响应，这条先不发（已进待发送队列）：%s", chat)
            self._last_error = "wechat-hung"
            return False
        # 名字先对一遍：微信里根本没有这个会话时，库会反复搜索/切列表 75 秒，
        # 你看到的就是「聊天列表被拉来拉去」，最后进程被保险丝硬退。
        if self._db is not None and not self._chat_exists(target):
            log.warning("微信里找不到会话「%s」（名字要跟界面显示的一模一样），这次不发", target)
            self._last_error = "no-such-chat"
            return False
        if self._wx is not None:
            gui_for_dock = getattr(self._wx, "_gui", None)
            if gui_for_dock is not None and self._dock_wechat_right(gui_for_dock):
                # 尺寸变了，侧栏比例得重算，否则坐标还是偏的
                try:
                    if hasattr(gui_for_dock, "calibrate"):
                        gui_for_dock.calibrate()
                        log.info("窗口变了，已重新校准布局")
                    else:
                        gui_for_dock._update_render_rect()
                except Exception as exc:  # noqa: BLE001
                    log.debug("重新校准失败：%s", exc)
        self._close_standalone_chat_windows()
        if screen_locked():
            log.warning("电脑锁屏了，这次不点界面（点了会挂住），等解锁再回：%s", chat)
            self._last_error = "screen-locked"
            return False
        if self._wx is None or self._ui_stale():
            self._connect_ui()
        if self._wx is None:
            log.warning("微信窗口未连接，无法发送")
            return False
        # 像人一样先"想几秒"。秒回是机器最明显的特征，风控看一眼就能挑出来。
        lo, hi = self._send_delay
        lo, hi = max(0.0, float(lo)), max(float(lo), float(hi))
        if hi > 0:
            delay = round(random.uniform(lo, hi), 2)
            log.debug("回复 %s 前等 %.2fs", target, delay)
            if self._stop.wait(delay):
                return False
        # 点界面是全局资源，并发发会串窗。锁必须带超时：上一条卡死时，
        # 后面的消息不能无限排队等下去（那会让整条链路看着像全挂了）。
        if not self._send_lock.acquire(timeout=150):
            log.warning("上一条发送还占着锁，这条等不到就放弃（会进待发送队列）")
            self._last_error = "send-busy"
            return False
        try:
            self._focus_epoch += 1
            epoch = self._focus_epoch
            watchdog = self._watchdog(SEND_TIMEOUT_SECONDS, target)
            response = None
            confirmed = False
            gui = getattr(self._wx, "_gui", None)
            # 点界面之前先把微信摆到最前面：别的程序（浏览器、开发工具）盖在
            # 微信上时，点击按 Z 序会落到别人头上——就是「鼠标瞎点」那回事。
            # 但抢前台会打断号主正在干的事（打字、看视频），所以先记住原来的
            # 前台窗口，回车一按完立刻还回去（见 _hand_back）。
            prev_fg = _foreground_hwnd()
            blocked: List[int] = []
            if gui is not None:
                focused, _ = self._timed("聚焦微信窗口",
                                         lambda: self._focus_wechat(gui), 12.0)
                blocked = focused if isinstance(focused, list) else []
            try:
                for attempt in (1, 2):
                    started = time.time()
                    try:
                        # 发送要走界面，两件事必须自己做：
                        #   1) 先把目标会话切到前台——不切的话，文字会粘到当前开着
                        #      的那个窗口里去（实测第一条回复就发错窗口了）；
                        #   2) 回车之后自己回读消息库确认落地。库自己的校验在这个
                        #      版本的库上是坏的（它只认 sender_id==2，而本机库里自己
                        #      发的消息 sender_id 是 3），所以 verify=True 永远判失败；
                        #      反过来 verify=False 时库又会乐观返回"成功"——其实根本没
                        #      发出去（实测 23:32 那条）。两条都不行，只能自己确认。
                        if gui is None:
                            response = self._wx.SendMsg(text, who=target)
                        else:
                            # 已经在这个会话里就别再切了：每次切会话都会把聊天列表
                            # 翻来翻去（搜索、滚动），看起来就是「反复切换聊天列表」。
                            # 连发多条时尤其明显——第一条切过去，后面几条应该原地发。
                            # 库内部用 _current_chat 记「上次成功发到哪个会话」
                            current = getattr(gui, "_current_chat", None)
                            sent_by_uia = False
                            # 首选纯键盘（Ctrl+F 找会话 + 粘贴 + 回车）：
                            # 不受窗口形状/坐标影响，也不会去点侧栏把列表拉来拉去。
                            sent_keys, timed_out = self._timed(
                                "UIA 打开会话并发送",
                                lambda: self._send_via_uia(gui, target, text), 75.0)
                            if timed_out:
                                self._last_error = "send-timeout"
                                log.warning("键盘发送超时，这轮放弃（已进待发送队列）")
                                break
                            if sent_keys:
                                sent_by_uia = True
                            if not sent_by_uia:
                                # 不再回落到库那套「搜索 + 翻列表」：它在这个窄窗口里
                                # 会空转 75 秒，还会把聊天列表拉来拉去（看着就像卡死）。
                                # 这条交给待发送队列，等窗口状态好的时候再补。
                                log.warning("UIA 这条没发出去「%s」（进待发送队列，不硬来）", target)
                                self._last_error = "open-chat-failed"
                                return False
                    except Exception as exc:  # noqa: BLE001
                        log.warning("发送失败 %s（第 %d 次）：%s", target, attempt, exc)
                        response = None
                        break
                    # 回车按了但输入框里还有字 → 多半是那一瞬间焦点被抢走了，
                    # 补按一次回车（不重输文字，避免重复/拼接）。
                    rescued = False
                    if (gui is not None and not self._ok(response)
                            and getattr(gui, "_send_attempted", False)):
                        rescued = self._rescue_enter(gui, target, text)
                        if rescued:
                            response = {"message": "补按回车后已发送", "ok": True}
                    # 回车已经按完了，屏幕立刻还给号主：后面等消息库确认的十几秒
                    # 完全不需要占着前台。以前要等整个流程结束才还原，等于每回一条
                    # 就抢走你十几秒的窗口——就是「弹窗影响操作」。
                    blocked = self._hand_back(prev_fg, gui, blocked, epoch)
                    if not self._ok(response) and self._attempted_unconfirmed(response):
                        log.info("库说没把握，交给消息库确认 %s", target)
                    # 无论库怎么说，都以消息库为准：库里落了才算真发出去。
                    # 库乐观返回"成功"但消息库里没有的，是真没发出去（实测 23:32）。
                    # 先看界面：回车之后输入框应该清空（这是最快的成功信号）。
                    # 判「清空了」优先读文字、不数像素：像素会把光标、输入法候选、
                    # 坐标漂到聊天区（全是黑字）都当成「还有字」，实测误判之后会把
                    # 已经发出去的那条判成没发出去、再重发一遍——重复消息就是这么来的。
                    # 只有读不到文本时（UIA 挂了）才退回像素判据。
                    box_cleared = False
                    if gui is not None:
                        try:
                            ours = self._ours_in_box(gui, text, target)
                            if ours is False:
                                box_cleared = True
                            elif ours is None:
                                box_cleared = not bool(gui._input_box_has_text())
                        except Exception:  # noqa: BLE001
                            box_cleared = False
                    confirmed = self._confirmed_by_db(target, text, since=started)
                    if confirmed:
                        log.info("已确认送达 %s（库里有记录）", target)
                    elif box_cleared:
                        # 输入框空了、库里还没写进去（微信写库有 9~15 秒延迟）：
                        # 算「很可能已发」，不再重发，避免同一条出现两遍。
                        log.info("%s 输入框已清空，按已发送处理（库还没写入）", target)
                        confirmed = True
                        self._last_error = "unconfirmed-but-cleared"
                    else:
                        log.info("没发出去 %s：输入框还有字 / 库里也没有", target)
                    if confirmed:
                        break
                    if rescued:
                        log.info("补按回车后就别重发了（%s），交给消息库确认", target)
                        break
                    if attempt == 1:
                        log.info("第 1 次没发出去（%s），重试一次", target)
                        # 可能是窗口又被别的程序盖住了，重试前再摆一次前台
                        if gui is not None:
                            self._restore_windows(blocked)
                            refocused, _ = self._timed(
                                "重新聚焦窗口", lambda: self._focus_wechat(gui), 10.0)
                            blocked = refocused if isinstance(refocused, list) else []
                        if self._stop.wait(3.0):
                            return False
            finally:
                watchdog.set()
                if gui is not None:
                    try:
                        gui.restore_zorder()
                    except Exception:  # noqa: BLE001
                        pass
                    self._restore_windows(blocked)
                    _refocus(prev_fg)
        finally:
            self._send_lock.release()
        if not confirmed:
            log.warning("发送被拒绝 %s：%s", target, response)
        return confirmed

    @staticmethod
    def _attempted_unconfirmed(response: Any) -> bool:
        """库说「已尝试发送但结果未确认」——按了回车但没验到，不代表没发出去。"""
        try:
            text = str(response.get("message") or "")      # WxResponse 是 dict 子类
        except AttributeError:
            text = str(response or "")
        return "未确认" in text or "已尝试发送" in text

    def _confirmed_by_db(self, target: str, text: str, since: float = 0.0) -> bool:
        """回读消息库，确认自己刚发的那条真的落库了。

        只在发送结果不确定的时候用。库是本地解密读，不点界面，等几秒没有副作用。

        ``since`` 是这次发送开始的时间：只认那之后落库的行，免得把上一条
        内容相同的老消息当成本次成功。
        """
        username = self._user_by_name.get(target) or ""
        if not username:
            try:
                username = str(self._db.username_by_nickname(target) or "")
            except Exception:  # noqa: BLE001 - 认不出是谁就放弃确认，按失败处理
                username = ""
        username = username or target
        if not username or self._db is None:
            return False
        deadline = time.time() + CONFIRM_TIMEOUT_SECONDS
        while time.time() < deadline:
            try:
                rows = self._db.get_messages(username, limit=8) or []
            except Exception:  # noqa: BLE001 - 确认失败就按失败处理
                return False
            for row in rows:
                if str(row.get("sender_username") or "") != self._self_wxid:
                    continue
                content = str(row.get("content") or "").strip()
                if content != text and not content.endswith("\n" + text):
                    continue
                if since and float(row.get("create_time") or 0) < since - 5.0:
                    continue   # 比这次发送还早的行，是上一条同内容的旧消息
                return True
            if self._stop.wait(CONFIRM_POLL_SECONDS):
                return False
        return False

    def already_delivered(self, chat: str, text: str, since: float) -> bool:
        """消息库里是不是**已经**有这条了（只看一眼，不等）。

        给待发送队列的自动补发把门：被步骤超时判成「放弃」的那条，线程其实还会
        跑完并落库——补发前不问一句就会同一条发两遍。跟 ``_confirmed_by_db`` 的
        区别是这里绝不循环等待，一眼没有就返回 False（等会让补发那一轮白耗 25 秒）。
        @param chat - 会话名（界面显示的那个名字）。
        @param text - 准备补发的正文。
        @param since - 这条进待发送队列的时间，比它早的同内容行不算。
        @returns 库里已经有这条时 True。
        """
        try:
            username = self._user_by_name.get(chat) or ""
            if not username and self._db is not None:
                username = str(self._db.username_by_nickname(chat) or "")
            username = username or chat
            if not username or self._db is None:
                return False
            rows = self._db.get_messages(username, limit=12) or []
        except Exception as exc:  # noqa: BLE001 - 查不动就当没有，交给后面正常补发
            log.debug("补发前查库失败：%s", type(exc).__name__)
            return False
        for row in rows:
            if str(row.get("sender_username") or "") != self._self_wxid:
                continue
            content = str(row.get("content") or "").strip()
            if content != text and not content.endswith("\n" + text):
                continue
            if float(row.get("create_time") or 0) >= float(since or 0) - 5.0:
                return True
        return False

    @staticmethod
    def _tidy(text: str) -> str:
        """模型输出 → 微信里像人话的纯文本。

        微信输入框不渲染 markdown，留着 ``**`` 就是一串星号，加上一堆 ``\\n\\n`` 会
        占满屏幕； reasoning 模型还会把 `<think>` 块一起吐出来。

        表情也一并去掉：号主要的是「像真人打字」，但真人发微信不一定带 emoji，
        而且模型很爱在每句话尾巴挂一个 😂。Unicode emoji 和微信那套方括号表情
        （[裂开]、[呲牙]…）都在这里清掉，清完顺手把空出来的空格收干净。
        """
        out = re.sub(r"<think\b[^>]*>.*?</think>", "", text or "", flags=re.S | re.I)
        out = re.sub(r"<(?:antml:)?(thinking|thought)\b[^>]*>.*?</(?:antml:)?\1>",
                     "", out, flags=re.S | re.I)
        out = out.replace("**", "")                       # 粗体/斜体星号
        out = _strip_emojis(out)
        out = re.sub(r"[ \t]+\n", "\n", out)              # 行尾空白
        out = re.sub(r"\n{3,}", "\n\n", out)              # 连续空行压到一行
        out = re.sub(r"[ \t]{2,}", " ", out)              # 表情删完留下的连续空格
        return out.strip()

    @staticmethod
    def _ok(response: Any) -> bool:
        """WxResponse 是 dict 子类（``is_success`` 是属性），但也可能拿到普通字典或 None。"""
        if response is None:
            return False
        flag = getattr(response, "is_success", None)
        if isinstance(flag, bool):
            return flag
        if isinstance(response, dict):
            return response.get("status") == "成功"
        return bool(response)
