"""微信自动回复的通道服务（桥接）。

一边接本机微信客户端（后端），一边把消息推给「回复大脑」。默认回复大脑就是
本目录里的 brain.py（直连你在 config.yaml 里填的模型），整套东西不依赖任何外
部程序。另外还在本地开一个 WebSocket，你可以挂一个自己的应答端上来（见
docs/开发说明.md）：

    微信客户端 (后端)  ──消息──▶  bridge.py  ──WebSocket──▶  外部应答端（可选）
                                          ◀──发消息──

协议（JSON，一行一条）：

    服务端 → 客户端
        {"op":"hello","account":"昵称","backend":"wechatauto","protocol":1}
        {"op":"message","chat":"群名/好友名","chatType":"group","sender":"张三",
         "text":"在吗","mentioned":true,"ts":1730000000000}
        {"op":"sent","chat":"群名/好友名","ok":true}
        {"op":"error","message":"发送失败"}

    客户端 → 服务端
        {"op":"send","chat":"群名/好友名","text":"回复内容"}
        {"op":"ping"}

运行::

    python bridge.py --backend wechatauto       # 微信 4.1：读本地消息库 + UIA 发送（发送要管理员）
    python bridge.py --backend mock --demo      # 不连微信，用来联调协议
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import signal
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import websockets

from backends.base import Backend, InboundMessage
from backends.wechatauto import WechatAutoBackend
from backends.mock import MockBackend
from web_dashboard import effective_allowlist, read_app_config, start_dashboard
import pending_store

log = logging.getLogger("wxbridge")

PROTOCOL_VERSION = 1
DEFAULT_URL = "ws://127.0.0.1:8765"

#: 最近消息落盘的位置。桥接一重启内存里的记录就没了，控制台左边会话列表
#: 会整个变空——看着像坏了。落到文件里，重启后还能看到最近在聊什么。
RECENT_FILE = Path(__file__).resolve().parent / "recent.jsonl"
#: 落盘保留多少条 / 内存里留多少条。
RECENT_KEEP = 300
RECENT_MEMORY = 300

#: 连上之后最多等多久要收到应答端的 identify 帧。应答端刚起来时可能正忙，
#: 太短会把能干活的连接误杀；太长则掉线的客户端占着坑。
IDENTIFY_TIMEOUT = 15.0

#: 没有外部应答端接入时，等多久再让本地大脑顶上。应答端重连只要几秒，
#: 等一小会儿可以避免「它回一遍、本地大脑又回一遍」。
BRAIN_WAIT_SECONDS = 12.0
#: 应答端早就掉线（那边程序关着）时的等待：不用每条都等那么久。
BRAIN_FAST_WAIT_SECONDS = 3.0
#: 谁来想回复：
#:   brain  = 桥接自己调 config.yaml 里配的那个模型（默认）——不依赖任何外部程序，最稳；
#:   client = 交给连上来的外部应答端（工具多、有长记忆，但它一掉线就没人回）。
RESPONDER = "brain"

#: 交给外部应答端之后多久还没答案，就由本地大脑接手。
#: 为什么需要这道保险：应答端跑在别的进程里，它一重启/卡死，消息已经投出去了，
#: 对面却永远不会回——界面上一直挂「正在想…」，好友那边什么都收不到。
#: 正常一轮只要几秒，75 秒足够宽松，不会误抢。
THINK_RESCUE_SECONDS = 75.0

#: 待发送队列自动补发。微信无响应 / 锁屏 / 发送超时没发出去的回复先进
#: pending_store 队列。以前只有控制台那个「补发」按钮会排空它——用户不点，
#: 旧回复就一直堆着。这里加一个后台定时器：界面缓过来之后自动补。
PENDING_DRAIN_SECONDS = 90.0
#: 一轮最多补几条，免得一次性把一堆旧回复连着发出去（像刷屏）。
PENDING_DRAIN_BATCH = 2
#: 超过这个年纪（秒）的回复不再自动补，留给控制台手动决定。
#: 自动补几小时前的旧回复很容易对不上话，宁可不补。
PENDING_MAX_AGE_SECONDS = 30 * 60.0


class Bridge:
    """桥接服务本体。

    微信后端是阻塞式的，跑在自己的线程里；WebSocket 服务跑在 asyncio
    循环里。两者之间用一个线程安全的队列传递消息。
    """

    def __init__(self, backend: Backend, host: str = "127.0.0.1", port: int = 8765,
                 http_port: int = 8766, brain: Any = None) -> None:
        self.backend = backend
        self.host = host
        self.port = port
        self.http_port = http_port
        # 本地大脑：没有应答端在跑、或应答端没回时，桥接自己顶上（见 brain.py）。
        self.brain = brain
        # 连着的外部应答端。用 dict 而不是 set：要按连接先后排序，还要能在
        # 只剩一个活跃收件人的时候把其它连接只当备胎。
        self._clients: "Dict[Any, float]" = {}
        # 通过握手的客户端（报过身份的）。消息优先发给他们。
        self._identified: "Dict[Any, Dict[str, Any]]" = {}
        # 没报身份的旧应答端。只在 _identified 空的时候兜底用，不参与抢消息。
        self._legacy: "Dict[Any, float]" = {}
        # 哑巴连接连上的时间：60 秒还没报身份、又有正经客户端在，就关掉。
        self._legacy_since: "Dict[Any, float]" = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._outbox: "asyncio.Queue[Dict]" = asyncio.Queue()
        self._client_task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        # 最后一次有应答端在的时间，用来判断「它刚掉线」还是「早没了」。
        self._last_client_at = time.time()
        # 本地大脑自己的守护栏：冷却、每日上限。
        self._brain_last_at: Dict[str, float] = {}
        self._brain_counts: Dict[str, int] = {}
        self._brain_day = ""
        self._persist_count = 0
        # 最近的消息（入站/出站），给网页控制台看的。
        self.history: "List[Dict[str, Any]]" = []
        self._history_lock = threading.Lock()
        # 已交给模型、还在等答案的会话（控制台显示「正在想…」）。
        self._thinking: "Dict[str, float]" = {}
        # 应答端主动报上来的「这条没回，因为什么」，最近 40 条。
        self._skips: "List[Dict[str, Any]]" = []
        # 已经投给应答端、还在等答案的消息：{chat: {at, payload}}。超时用它救场。
        self._pending: "Dict[str, Dict[str, Any]]" = {}
        # 每个会话的待回消息（合并用）和后台工人。
        self._chat_pending: "Dict[str, List[Dict[str, Any]]]" = {}
        self._chat_workers: "Dict[str, Any]" = {}
        # 「有变化」的版本号：控制台的长轮询靠它，比每 4 秒轮一次快得多。
        self.rev = 0
        self.rev_cond = threading.Condition()
        # 历史要在 history 建好之后再读回来。
        self._load_recent()
        self._mark_lost_on_start()

    def _mark_lost_on_start(self) -> None:
        """启动时找一下「上次崩之前有人说话、但没回」的记录，标成未回。

        桥接被保险丝硬退（发送卡死）重启后，那条回复就没了。以前界面上
        完全看不出发生了什么，现在会写进「最近异常」。
        """
        try:
            cutoff = time.time() - 600
            history = list(self.history)
            try:
                names = effective_allowlist()
            except Exception:  # noqa: BLE001
                names = set()
            for index, entry in enumerate(history):
                if entry.get("dir") != "in" or float(entry.get("ts") or 0) < cutoff:
                    continue
                chat = entry.get("chat")
                # 名单外的人本来就不回，别报成「漏回」——以前这里一口气刷出
                # 九条「桥接刚才重启了」，全是名单外的人，看得人以为通路坏了。
                if names and chat not in names:
                    continue
                ts = float(entry.get("ts") or 0)
                answered = any(
                    h.get("dir") == "out" and h.get("chat") == chat and float(h.get("ts") or 0) >= ts
                    for h in history[index + 1:]
                )
                if not answered:
                    self._skips.append({"ts": ts, "chat": chat, "text": entry.get("text", ""),
                                        "reason": "桥接刚才重启了，这条可能没回出去"})
            if len(self._skips) > 40:
                del self._skips[:-40]
        except Exception:  # noqa: BLE001
            pass

    def bump(self) -> None:
        """告诉控制台「有变化了」，长轮询的请求会立刻返回。"""
        with self.rev_cond:
            self.rev += 1
            self.rev_cond.notify_all()

    def _record(self, direction: str, chat: str, text: str, sender: str = "") -> None:
        """记一条消息，供网页控制台显示。"""
        entry = {"ts": time.time(), "dir": direction, "chat": chat,
                 "text": text, "sender": sender}
        with self._history_lock:
            self.history.append(entry)
            if len(self.history) > RECENT_MEMORY:
                del self.history[:-RECENT_MEMORY]
            self._persist(entry)
        self.bump()

    def _persist(self, entry: Dict[str, Any]) -> None:
        """往最近消息文件里追加一条；攒多了就整体重写，免得无限长。"""
        try:
            # 上一行没有换行结尾（手工改过文件、或者被别的程序写的）时，
            # 直接接上去会把两行粘成一行，读的时候两条一起坏掉。
            need_nl = False
            if RECENT_FILE.exists() and RECENT_FILE.stat().st_size > 0:
                with RECENT_FILE.open("rb") as fh:
                    fh.seek(-1, 2)
                    need_nl = fh.read(1) != b"\n"
            with RECENT_FILE.open("a", encoding="utf-8") as fh:
                if need_nl:
                    fh.write("\n")
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._persist_count += 1
            if self._persist_count < 50:
                return
            self._persist_count = 0
            self._rewrite_recent()
        except Exception as exc:  # noqa: BLE001 - 写不了文件也不该影响收发
            log.debug("消息落盘失败：%s", exc)

    def _rewrite_recent(self) -> None:
        """把文件重写成「文件里的 + 内存里的」，最近 RECENT_KEEP 条。

        为什么要合并而不是直接覆盖：桥接重启时新旧两个实例可能有一两秒重叠，
        新实例刚追加的消息会被老实例的整篇重写冲掉（实测丢过几条出站记录）。
        合并 + 按时间排序就撞不掉了。
        """
        merged: Dict[tuple, Dict[str, Any]] = {}
        try:
            raw = RECENT_FILE.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            raw = ""
        decoder = json.JSONDecoder()
        i = 0
        while i < len(raw):
            while i < len(raw) and raw[i] in " \r\n\t":
                i += 1
            if i >= len(raw):
                break
            try:
                entry, i = decoder.raw_decode(raw, i)
            except ValueError:
                nl = raw.find("\n", i)
                if nl < 0:
                    break
                i = nl + 1
                continue
            if isinstance(entry, dict) and entry.get("chat"):
                merged[(entry.get("ts"), entry.get("chat"), entry.get("text"))] = entry
        for entry in self.history:
            if entry.get("chat"):
                merged[(entry.get("ts"), entry.get("chat"), entry.get("text"))] = entry
        ordered = sorted(merged.values(), key=lambda e: e.get("ts") or 0)[-RECENT_KEEP:]
        lines = [json.dumps(e, ensure_ascii=False) for e in ordered]
        tmp = RECENT_FILE.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        tmp.replace(RECENT_FILE)

    def _load_recent(self) -> None:
        """启动时把上次的最近消息读回内存。"""
        if not RECENT_FILE.exists():
            return
        try:
            text = RECENT_FILE.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            return
        # 可能有一行被写坏（两行粘在一起、手工编辑过），逐条尽量救：
        # 用 raw_decode 从当前位置往后啃，能解一个算一个。
        decoder = json.JSONDecoder()
        loaded: List[Dict[str, Any]] = []
        i = 0
        while i < len(text):
            while i < len(text) and text[i] in " \r\n\t":
                i += 1
            if i >= len(text):
                break
            try:
                entry, end = decoder.raw_decode(text, i)
                i = end
            except ValueError:
                # 这一行坏了：跳到下一行继续
                nl = text.find("\n", i)
                if nl < 0:
                    break
                i = nl + 1
                continue
            if isinstance(entry, dict) and entry.get("chat"):
                loaded.append(entry)
        if loaded:
            self.history.extend(loaded[-RECENT_KEEP:])
            log.info("读回 %d 条历史消息（控制台能看到重启前的对话）", len(loaded[-RECENT_KEEP:]))
        # 顺手把文件整理干净（去掉 BOM 和粘连），免得下次再踩。
        try:
            self._rewrite_recent()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ 消息流转

    def _on_inbound(self, message: InboundMessage) -> None:
        """后端线程调用这里，把微信消息推进 asyncio 队列。"""
        payload = message.to_json()
        log.info("收到消息 [%s/%s] %s：%s", message.chat_type, message.chat, message.sender, message.text)
        self._record("in", message.chat, message.text, message.sender)
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._outbox.put(payload), self._loop)

    async def _pump(self) -> None:
        """把队列里的消息交给客户端。

        只发给一个收件人。以前是广播：应答端被挂了两份（或者旧连接没退干净）
        的时候，同一条微信消息会有两个实例各自跑一轮，好友收到两遍一模一样的
        回答，还多烧一次模型额度。这里按连接时间取最新的那个，发失败就摘掉
        换下一个。

        收件人优先取报过身份的客户端；一个都没有时退回老客户端（v0.1.3 之前
        的旧应答端不会发 identify 帧，硬要求会把整条链路踢下线）。老客户端上下文
        可能已经失效，所以只在没有新版应答端可发的时候才用它们。
        """
        while True:
            payload = await self._outbox.get()
            if RESPONDER == "brain":
                # 一条消息一条通路：有应答端就交给它，没有就桥接自己调模型回。
                # 这里只排队，不在这里等——发一条要十几秒，等在这儿会把后面
                # 所有会话都堵住（表现就是「漏掉、后面不回了」）。
                self._enqueue(payload)
                continue
            recipients = self._identified or self._legacy
            if not recipients:
                # 没有应答端接入：多半是那边程序关着了，暂时只能靠本地大脑。
                # 等一小会儿看它会不会连回来；实在没人，就本地大脑自己回，
                # 别把好友的消息直接丢掉——以前就是丢在这里的。
                recipients = await self._wait_for_client()
            if not recipients:
                await self._brain_reply(payload)
                continue
            if not self._identified:
                log.warning("只有 %d 个未报身份的旧应答端在跑，升级应答端后可消除该警告",
                            len(recipients))
            if len(recipients) > 1:
                log.warning(
                    "检测到 %d 个外部应答端接入，只用最新的一个（同时挂两份会双倍烧额度）",
                    len(recipients),
                )
            data = json.dumps(payload, ensure_ascii=False)
            # dict 保持插入顺序，最新的连接排在最后。
            delivered = False
            for client in reversed(list(recipients)):
                try:
                    await client.send(data)
                    delivered = True
                    break
                except Exception:  # noqa: BLE001 - 掉线的客户端直接丢
                    recipients.pop(client, None)
                    self._clients.pop(client, None)
            if not delivered:
                # 一条都没投出去（客户端正好在断开）——以前这里直接丢弃，好友就
                # 永远收不到回复。先放回队列，让下一轮（新连接/本地大脑）接手。
                log.warning("所有客户端都发不出去，放回队列重试：%s",
                            str(payload.get("text", ""))[:40])
                await asyncio.sleep(1.0)
                await self._outbox.put(payload)
                continue
            # 记下「这条已经交给应答端了、正在等答案」，超时没人回就本地大脑接手。
            chat = str(payload.get("chat") or "")
            if chat:
                # 同一个会话只记「第一条还没回的消息」的时间：对方连发五条时，
                # 计时不能被后面的消息一次次重置，否则兜底永远等不到。
                if chat not in self._pending:
                    self._pending[chat] = {"at": time.time(), "payload": payload}
                else:
                    self._pending[chat]["payload"] = payload

    async def _wait_for_client(self) -> "Dict[Any, Any]":
        """等外部应答端连上来。

        那边程序关着的时候应答端一定不在，但
        窗口重开只要几秒，所以先等一小会儿再走本地大脑，免得两边都答一遍。
        """
        # 应答端刚掉线：多等一会儿（它正在重连）。
        # 应答端早就没了：少等一会儿，不然每条消息都白白慢十几秒。
        idle = time.time() - self._last_client_at
        timeout = BRAIN_WAIT_SECONDS if idle < 30 else BRAIN_FAST_WAIT_SECONDS
        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            recipients = self._identified or self._legacy
            if recipients:
                return recipients
        log.warning("等了 %.0fs 也没有外部应答端接入，用本地大脑自己回", timeout)
        return {}

    @staticmethod
    def _daily_cap(cfg: Dict[str, Any], key: str) -> int:
        """每日上限读出来的数：<=0 就是不限（默认就是不限）。

        config.yaml 里默认写的是 0（= 不限制），0 就是不限；
        这边要是还按 ``count >= 0`` 判，就变成「谁都不回」，所以先过一道。
        """
        try:
            value = int(cfg.get(key, 0) or 0)
        except (TypeError, ValueError):
            value = 0
        return value if value > 0 else 0

    def _brain_rate_ok(self, chat: str) -> bool:
        """本地大脑也要守护栏：别连着发。每日条数默认不限（配 0 = 不限）。"""
        cfg = read_app_config()
        now = time.time()
        today = time.strftime("%Y-%m-%d")
        if self._brain_day != today:
            self._brain_day = today
            self._brain_counts.clear()
        last = self._brain_last_at.get(chat, 0.0)
        if now - last < float(cfg.get("minIntervalSeconds", 10)):
            return False
        per_chat = self._daily_cap(cfg, "dailyMaxPerChat")
        if per_chat and self._brain_counts.get(chat, 0) >= per_chat:
            return False
        total = self._daily_cap(cfg, "dailyMaxTotal")
        if total and sum(self._brain_counts.values()) >= total:
            return False
        return True

    def _brain_allowed(self, payload: Dict[str, Any]) -> bool:
        """本地大脑能不能回这条：名单里的人 + 群里得艾特。"""
        chat = str(payload.get("chat") or "")
        chat_type = str(payload.get("chatType") or "private")
        try:
            names = effective_allowlist()
        except Exception:  # noqa: BLE001 - 读不到名单就不回，宁可漏回不乱回
            log.warning("读不到白名单，本地大脑这次不回")
            return False
        if names and chat not in names:
            return False
        if chat_type == "group":
            cfg = read_app_config()
            if not cfg.get("allowGroups", True):
                return False
            if cfg.get("requireMention", True) and not payload.get("mentioned"):
                return False
        return True

    # ---------------------------------------------------------------- 排队与作答

    def _enqueue(self, payload: Dict[str, Any]) -> None:
        """把消息按会话排队，每个会话一个后台工人。

        为什么要排队而不是就地回：
        - 发一条要走界面，15~25 秒；就地回会把**所有**会话都堵在这，
          表现就是「这几条漏了、后面也不回了」；
        - 同一个人连着发好几条时，合并成一次回才像人，也省时间。

        为什么要「等冷却」而不是「跳过」：冷却期内直接丢，用户看到的就是
        漏消息。现在改成等到冷却结束再回（同一批合并）。
        """
        chat = str(payload.get("chat") or "")
        if not chat:
            return
        self._chat_pending.setdefault(chat, []).append(payload)
        worker = self._chat_workers.get(chat)
        if worker is None or worker.done():
            self._chat_workers[chat] = asyncio.create_task(self._chat_worker(chat))

    async def _chat_worker(self, chat: str) -> None:
        """一个会话一个工人：合并这一段的消息 → 等冷却 → 调模型 → 发出去。"""
        try:
            while True:
                batch = self._chat_pending.get(chat) or []
                if not batch:
                    break
                self._chat_pending[chat] = []
                first = batch[0]
                texts = [str(p.get("text") or "").strip() for p in batch]
                texts = [t for t in texts if t]
                if not texts:
                    continue
                text = "\n".join(texts)

                if not self._brain_allowed(first):
                    self._note_skip(chat, text, self._skip_reason(first))
                    continue

                # 冷却：等够间隔（最多等 60 秒），不是丢掉
                wait = await self._wait_for_slot(chat, limit=60.0)
                if wait is None:
                    self._note_skip(chat, text, "这条冷却太久（等满 60 秒还没轮到），先搁着")
                    continue
                if wait > 0:
                    log.info("%s 冷却中，%.0f 秒后回（这段的消息合并成一条）", chat, wait)

                if not self._count_ok(chat):
                    self._note_skip(chat, text, "撞到配置里的每日上限（配置改成 0 就是不限）")
                    continue
                if self.brain is None:
                    self._note_skip(chat, text,
                                    "模型还没配：编辑 config.yaml 的 llm 段（baseURL / model / apiKey），"
                                    "或在控制台「设置」页填，填完点重启")
                    continue
                self._brain_last_at[chat] = time.time()
                self._brain_counts[chat] = self._brain_counts.get(chat, 0) + 1
                self._thinking[chat] = time.time()
                self.bump()
                parts: List[str] = []
                try:
                    if hasattr(self.brain, "reply_parts"):
                        parts = await asyncio.to_thread(self.brain.reply_parts, chat, text)
                    else:
                        one = await asyncio.to_thread(self.brain.reply, chat, text)
                        parts = [one] if one else []
                except Exception as exc:  # noqa: BLE001
                    log.warning("大脑没答上来（%s）：%s", chat, exc)
                    self._note_skip(chat, text, f"模型调用出错：{exc}"[:120])
                    parts = []
                finally:
                    self._thinking.pop(chat, None)
                    self.bump()
                if not parts:
                    err = str(getattr(self.brain, "last_error", "") or "")[:80]
                    log.warning("大脑没给出答案（%s）：%s", chat, err or "无返回")
                    self._note_skip(chat, text, "模型没给出答案" + (f"（{err}）" if err else ""))
                    continue
                # 默认就一条；正文里有逗号/句号/问号才会拆成两条，两条之间隔一两秒，像真人分两下说。
                for index, part in enumerate(parts[:2]):
                    if index:
                        await asyncio.sleep(random.uniform(0.8, 2.2))
                    log.info("回复 %s（%d/%d）：%s", chat, index + 1, len(parts), part)
                    ok = await asyncio.to_thread(self.backend.send, chat, part)
                    self._record("out", chat, part)
                    if not ok:
                        why = str(getattr(self.backend, "_last_error", "") or "")
                        queue_reasons = {
                            "paused": "已暂停（紧急刹车），排队等你恢复",
                            "wechat-hung": "微信无响应，排队等它恢复",
                            "screen-locked": "电脑锁屏中，排队等解锁",
                            "send-timeout": "发送超时，排队重试",
                        }
                        if why in queue_reasons:
                            pending_store.add(chat, part, queue_reasons[why])
                            self._note_skip(chat, part, queue_reasons[why] + "（已进待发送队列）")
                            break
                        if why in ("open-chat-failed", "no-such-chat"):
                            self._note_skip(chat, part, "微信里找不到这个会话（名字要跟界面显示的一模一样）")
                            break
                        log.warning("回话没发出去（%s），再试一次", chat)
                        await asyncio.sleep(2.0)
                        ok = await asyncio.to_thread(self.backend.send, chat, part)
                        if not ok:
                            self._note_skip(chat, part, "发出去了但微信没确认（可能窗口被挡住）")
                            break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("会话工人出错（%s）：%s", chat, exc)
        finally:
            if not self._chat_pending.get(chat):
                self._chat_workers.pop(chat, None)

    async def _wait_for_slot(self, chat: str, limit: float = 60.0) -> Optional[float]:
        """等这条会话的冷却。返回要等几秒；超过 limit 就返回 None（别无限等）。

        只管 minIntervalSeconds 这一条防刷屏的间隔，每日条数上限在 _count_ok 判。
        """
        cfg = read_app_config()
        gap = float(cfg.get("minIntervalSeconds", 10) or 0)
        last = self._brain_last_at.get(chat, 0.0)
        wait = gap - (time.time() - last)
        if wait <= 0:
            return 0.0
        if wait > limit:
            return limit
        await asyncio.sleep(wait)
        return wait

    def _count_ok(self, chat: str) -> bool:
        """每日条数上限（每人 / 全体）：配置里写 0 或负数就是不限。"""
        cfg = read_app_config()
        today = time.strftime("%Y-%m-%d")
        if self._brain_day != today:
            self._brain_day = today
            self._brain_counts.clear()
        per_chat = self._daily_cap(cfg, "dailyMaxPerChat")
        if per_chat and self._brain_counts.get(chat, 0) >= per_chat:
            return False
        total = self._daily_cap(cfg, "dailyMaxTotal")
        if total and sum(self._brain_counts.values()) >= total:
            return False
        return True

    def _skip_reason(self, payload: Dict[str, Any]) -> str:
        """到底为什么没回：不在名单？名单里但被关了？群里没 @？"""
        chat = str(payload.get("chat") or "")
        try:
            names = effective_allowlist()
        except Exception:  # noqa: BLE001
            return "读不到名单"
        if chat not in names:
            try:
                import allowlist_store
                if any(e["name"] == chat for e in allowlist_store.load()):
                    return "名单里，但开关是关的"
            except Exception:  # noqa: BLE001
                pass
            return "不在名单"
        if str(payload.get("chatType") or "private") == "group" and not payload.get("mentioned"):
            return "群里没 @ 我"
        return "被规则拦下"

    def _note_skip(self, chat: str, text: str, reason: str) -> None:
        """记一条「这条没回，因为什么」，控制台上能看见。"""
        self._skips.append({"ts": time.time(), "chat": chat, "text": text, "reason": reason})
        if len(self._skips) > 40:
            del self._skips[:-40]
        log.info("未回 [%s]：%s", chat, reason)
        self.bump()

    async def _brain_reply(self, payload: Dict[str, Any]) -> None:
        """用设置页里那一个模型回一句。"""
        chat = str(payload.get("chat") or "")
        text = str(payload.get("text") or "")
        if not chat or not text:
            return
        if self.brain is None:
            log.warning("没有可用的大脑（配置没加载），这条没回：%s", text[:40])
            self._note_skip(chat, text, "大脑没配好")
            return
        # 名单和 @ 规则在这边判：这是最后一道闸，宁可漏回也别乱回。
        if not self._brain_allowed(payload):
            reason = "不在名单"
            try:
                names = effective_allowlist()
                if names and chat in names:
                    reason = "群里没 @ 我"
            except Exception:  # noqa: BLE001
                pass
            self._note_skip(chat, text, reason)
            return
        if not self._brain_rate_ok(chat):
            self._note_skip(chat, text, "冷却中或到今日上限")
            return
        self._brain_last_at[chat] = time.time()
        self._brain_counts[chat] = self._brain_counts.get(chat, 0) + 1
        self._thinking[chat] = time.time()
        try:
            answer = await asyncio.to_thread(self.brain.reply, chat, text)
        except Exception as exc:  # noqa: BLE001
            self._thinking.pop(chat, None)
            log.warning("大脑没答上来：%s", exc)
            self._note_skip(chat, text, "模型调用出错")
            return
        self._thinking.pop(chat, None)
        if not answer:
            err = str(getattr(self.brain, "last_error", "") or "")[:80]
            log.warning("大脑没给出答案（%s）：%s", chat, err or "无返回")
            self._note_skip(chat, text, "模型没给出答案" + (f"（{err}）" if err else ""))
            return
        log.info("回复 %s：%s", chat, answer)
        ok = await asyncio.to_thread(self.backend.send, chat, answer)
        self._record("out", chat, answer)
        if not ok:
            log.warning("回话没发出去：%s", chat)

    async def _handle(self, client: Any) -> None:
        self._clients[client] = time.time()
        self._last_client_at = time.time()
        self._legacy_since.pop(client, None)
        pending: List[Any] = []
        try:
            # 账号名要等消息库解密完才有（首次要从微信进程里抠密钥，十几秒）。
            # 等一下再握手，免得应答端那边记成「账号=未知」。
            account = self.backend.account()
            if not account:
                for _ in range(60):
                    await asyncio.sleep(0.5)
                    account = self.backend.account()
                    if account:
                        break
            hello = {
                "op": "hello",
                "account": account,
                "backend": self.backend.name,
                "protocol": PROTOCOL_VERSION,
            }
            await client.send(json.dumps(hello, ensure_ascii=False))
            # 握手：新应答端第一帧是 identify（自报版本和进程号）。认不出身份的
            # 不当僵尸杀——现场可能只跑着旧版应答端，杀了整条链路就断了。
            try:
                raw = await asyncio.wait_for(client.recv(), timeout=IDENTIFY_TIMEOUT)
            except asyncio.TimeoutError:
                raw = None
            frame = None
            if raw is not None:
                try:
                    frame = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
                except Exception:  # noqa: BLE001
                    frame = None
            if isinstance(frame, dict) and frame.get("op") == "identify":
                info = {"version": str(frame.get("version", "")), "pid": frame.get("pid")}
                self._identified[client] = info
                log.info("外部应答端已接入：版本=%s 进程=%s", info["version"], info["pid"])
            else:
                self._legacy[client] = time.time()
                self._legacy_since[client] = time.time()
                log.warning("应答端没报身份（旧版本），按兼容模式服务：%s", str(frame)[:60])
                if raw is not None:
                    pending.append(raw)
            for raw in pending:
                await self._on_frame(client, raw)
            async for raw in client:
                await self._on_frame(client, raw)
        except Exception as exc:  # noqa: BLE001
            log.info("客户端连接结束：%s", exc)
        finally:
            self._identified.pop(client, None)
            self._legacy.pop(client, None)
            self._legacy_since.pop(client, None)
            self._clients.pop(client, None)
            self._last_client_at = time.time()

    async def _think_guard(self) -> None:
        """应答端接了活却没回音，就本地大脑接手。

        典型场景：应答端那边的程序重启（或者卡死）的那几秒，消息已经投给旧连接了，对面
        再也不会回。以前界面上就一直挂「正在想…」，好友什么都收不到。
        现在超过 THINK_RESCUE_SECONDS 没动静，就用本地大脑把这句回掉。
        """
        while True:
            await asyncio.sleep(5)
            try:
                now = time.time()
                for chat, item in list(self._pending.items()):
                    if now - float(item.get("at", 0)) < THINK_RESCUE_SECONDS:
                        continue
                    payload = item.get("payload") or {}
                    self._pending.pop(chat, None)
                    self._thinking.pop(chat, None)
                    log.warning("「%s」超过 %.0f 秒没有回音，本地大脑接手", chat, THINK_RESCUE_SECONDS)
                    await self._brain_reply(payload)
            except Exception as exc:  # noqa: BLE001
                log.debug("回音守卫出错：%s", exc)

    async def _reaper(self) -> None:
        """清掉「不说话」的连接。

        应答端重启时旧实例的 socket 有时会挂着不报身份、也不干活。它们不抢消息
        （_pump 只发给报过身份的），但每重装一次多一条，日志里一排
        「应答端没报身份（旧版本）」——看着像版本不匹配。有正经客户端在的时候，
        把这些哑巴连接关掉，日志就干净了。
        """
        while True:
            await asyncio.sleep(30)
            try:
                if not self._identified:
                    continue
                now = time.time()
                for client, since in list(self._legacy_since.items()):
                    if now - since < 60:
                        continue
                    log.info("关掉一条不说话的旧连接（60 秒没报身份）")
                    self._legacy.pop(client, None)
                    self._legacy_since.pop(client, None)
                    self._clients.pop(client, None)
                    try:
                        await client.close()
                    except Exception:  # noqa: BLE001
                        pass
            except Exception as exc:  # noqa: BLE001
                log.debug("清理旧连接出错：%s", exc)

    async def _on_frame(self, client: Any, raw: Any) -> None:
        try:
            frame = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return
        op = frame.get("op")
        if op == "ping":
            await client.send(json.dumps({"op": "pong"}, ensure_ascii=False))
            return
        if op == "thinking":
            # 应答端收到消息、已经交给模型了。控制台上就能显示「正在想…」，
            # 不用再猜「为什么不回」——那是模型在跑，不是坏了。
            chat = str(frame.get("chat", ""))
            if chat:
                self._thinking[chat] = time.time()
            return
        if op == "failed":
            # 应答端那边这一轮挂了（模型不通、超时、报错）：别让界面一直转圈，
            # 立刻用本地大脑把这句回掉。这是最有价值的一条兜底——应答端那边断了网
            # 的时候，本地大脑走的是另一家，照样能回。
            chat = str(frame.get("chat", ""))
            reason = str(frame.get("reason", "") or "这一轮没跑通")
            item = self._pending.pop(chat, None) or {}
            self._thinking.pop(chat, None)
            log.warning("应答端这一轮失败了（%s）：%s，本地大脑顶上", chat, reason)
            payload = item.get("payload") or {}
            if payload:
                await self._brain_reply(payload)
            return
        if op == "skip":
            # 应答端主动说「这条我没回，因为……」：不在名单、群里没艾特、
            # 冷却中、今日上限、重复投递。这条链路以前完全不可见。
            chat = str(frame.get("chat", ""))
            reason = str(frame.get("reason", "") or "没回")
            text = str(frame.get("text", ""))
            self._thinking.pop(chat, None)
            self._pending.pop(chat, None)
            self._skips.append({"ts": time.time(), "chat": chat,
                                "text": text, "reason": reason})
            if len(self._skips) > 40:
                del self._skips[:-40]
            log.info("未回 [%s]：%s", chat, reason)
            self.bump()
            return
        if op != "send":
            return
        chat = str(frame.get("chat", ""))
        text = str(frame.get("text", ""))
        if not chat or not text:
            return
        ok = await asyncio.to_thread(self.backend.send, chat, text)
        self._record("out", chat, text)
        self._thinking.pop(chat, None)
        self._pending.pop(chat, None)
        reply = {"op": "sent" if ok else "error", "chat": chat, "ok": ok}
        if not ok:
            reply["message"] = "发送失败"
        await client.send(json.dumps(reply, ensure_ascii=False))

    # ------------------------------------------------------------------ 运行

    async def _pending_drainer(self) -> None:
        """待发送队列的自动补发。

        微信无响应、锁屏、发送超时没发出去的回复先进 pending_store。以前只有
        控制台那个「补发」按钮会排空它——用户不点，旧回复就一直堆着，等想起来
        往往已经对不上话了。这里做个后台定时器：界面缓过来就自动补几条。

        两条自律：只在 backend.can_send_now() 说「现在能点界面」时才动手
        （暂停/锁屏/微信卡住一律跳过本轮，不硬发）；只补年纪合适的，太老的留给
        手动，免得大半夜把几小时前的旧回复发出去。
        """
        while True:
            await asyncio.sleep(PENDING_DRAIN_SECONDS)
            try:
                can = getattr(self.backend, "can_send_now", None)
                if callable(can) and not can():
                    log.debug("现在不补发（暂停/锁屏/微信无响应），待发送队列留着")
                    continue
                items = pending_store.list_items()
                if not items:
                    continue
                now = time.time()
                fresh = [i for i in items
                         if now - float(i.get("at", 0) or 0) <= PENDING_MAX_AGE_SECONDS]
                if not fresh:
                    log.debug("待发送队列 %d 条都太老，不自动补（可在控制台手动补发）",
                              len(items))
                    continue
                for item in fresh[:PENDING_DRAIN_BATCH]:
                    chat = str(item.get("chat") or "")
                    text = str(item.get("text") or "")
                    if not chat or not text:
                        pending_store.remove(chat, text)
                        continue
                    # 补发前先查库：被判「超时放弃」的那条其实常常还在后台跑完，
                    # 等我们补发时它已经落库了——不查就会同一条发给真人两遍
                    # （实测 10:53:46 进队列、10:53:51 原线程发送成功、10:54:14 补发重复）。
                    done = getattr(self.backend, "already_delivered", None)
                    if callable(done) and done(chat, text, float(item.get("at", 0) or 0)):
                        log.info("待发送队列这条其实已经发出去了（%s），不重复发", chat)
                        pending_store.remove(chat, text)
                        continue
                    ok = await asyncio.to_thread(self.backend.send, chat, text)
                    if ok:
                        self._record("out", chat, text)
                        pending_store.remove(chat, text)
                        log.info("待发送队列自动补发成功：%s", chat)
                    else:
                        log.warning("待发送队列补发还没成（%s），下轮再试", chat)
            except Exception as exc:  # noqa: BLE001
                log.debug("待发送队列自动补发出错：%s", exc)

    def _dashboard_send(self, chat: str, text: str) -> bool:
        """网页控制台上的「手动发一条」。走的是同一套后端发送逻辑。"""
        return bool(self.backend.send(chat, text))

    def request_stop(self) -> None:
        """让 run() 正常收尾。必须在事件循环线程里调用。"""
        if self._stop_event is not None:
            self._stop_event.set()

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self.backend.start(self._on_inbound)
        self._client_task = asyncio.create_task(self._pump())
        asyncio.create_task(self._reaper())
        asyncio.create_task(self._think_guard())
        asyncio.create_task(self._pending_drainer())
        # 网页控制台：跟桥接同一个进程，状态最准。
        if self.http_port:
            httpd = start_dashboard(self, port=self.http_port, host=self.host,
                                    on_send=self._dashboard_send)
            if httpd is not None:
                log.info("网页控制台：http://%s:%d/", self.host, self.http_port)
            else:
                log.warning("网页控制台没起来（端口 %d 被占了吧）", self.http_port)
        try:
            async with websockets.serve(self._handle, self.host, self.port, max_size=8 * 1024 * 1024):
                log.info("桥接服务已启动：ws://%s:%d（后端 %s）", self.host, self.port, self.backend.name)
                log.info("外部应答端请配置 url = %s", DEFAULT_URL.replace("8765", str(self.port)))
                # 等停止信号，不是等一个永远不完成的 Future：以前靠信号处理器
                # 调 loop.stop()，退出时必然甩一句 Event loop stopped before
                # Future completed.
                await self._stop_event.wait()
                log.info("收到退出信号，正在收尾")
        finally:
            if self._client_task is not None:
                self._client_task.cancel()
                self._client_task = None
            try:
                self.backend.stop()
            except Exception:  # noqa: BLE001 - 退出路径不纠结
                log.debug("停止后端时出错", exc_info=True)


def _demo_script() -> List[Any]:
    """mock 后端演示用的消息脚本。

    发消息的人取 config.yaml 里的种子名单 —— 演示要能真的走到「模型 → 回复」，
    用名单外的名字会整场只报「不在名单」，看不出链路通不通。
    """
    try:
        import appconfig
        names = [str(n) for n in appconfig.seed_names() if str(n).strip()]
    except Exception:  # noqa: BLE001 - 读不到配置就用兜底名
        names = []
    names = names or ["小明", "小红"]
    first = names[0]
    second = names[1] if len(names) > 1 else names[0]
    return [
        (first, "private", first, "在吗？刚看到你那条消息", 3.0),
        (second, "private", second, "晚上吃啥，随便说说", 12.0),
    ]


def _demo_inbound() -> List[Tuple[float, str]]:
    """wechatauto 后端联调用的合成入站消息 [(延迟秒, 正文), ...]。

    收的是假消息、发出去是真的：回复会真的落进「文件传输助手」，
    整条链路（模型→桥接→微信界面）都能验，又不打扰任何真人。
    """
    return [
        (5.0, "在吗？最近咋样"),
        (16.0, "帮我说句话呗，就回两个字：收到"),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="个人微信号自动回复桥接服务")
    parser.add_argument("--backend", choices=["wechatauto", "mock"], default="wechatauto")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--http-port", type=int, default=8766,
                        help="网页控制台端口，0 = 不开")
    parser.add_argument("--demo", action="store_true", help="mock 后端自带演示消息")
    parser.add_argument(
        "--send-delay",
        default=None,
        help='回复前随机延迟区间（秒），形如 "3,8"；写 "0" 关掉。'
             "不传就用 config.yaml 里的 send.delaySeconds",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--brain",
        choices=["auto", "off"],
        default="auto",
        help="本地大脑：没有外部应答端时由桥接自己回话（auto=开，off=关）",
    )
    args = parser.parse_args()

    import appconfig
    # 刚下载完还没有 config.yaml：先从样例复制一份，免得大脑一上来就「缺料」。
    appconfig.ensure_config()

    if args.send_delay is None:
        got = appconfig.send_options().get("delaySeconds")
        parts = [float(x) for x in got] if isinstance(got, (list, tuple)) and got else [3.0, 8.0]
        delay_range = (parts[0], parts[1] if len(parts) > 1 else parts[0])
    else:
        try:
            parts = [float(x) for x in str(args.send_delay).split(",") if x.strip() != ""]
            delay_range = (parts[0], parts[1] if len(parts) > 1 else parts[0])
        except (ValueError, IndexError):
            raise SystemExit('--send-delay 看不懂：%s（要写成 "3,8" 或 "0"）' % args.send_delay)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.backend == "mock":
        backend: Backend = MockBackend(_demo_script() if args.demo else [])
    elif args.backend == "wechatauto":
        # 读的是本地消息库，天然覆盖所有会话，--listen-all 对它无意义。
        backend = WechatAutoBackend(
            demo_script=_demo_inbound() if args.demo else None,
            send_delay=delay_range,
        )
    else:
        raise SystemExit(f"不支持的后端：{args.backend}（可选 wechatauto / mock）")

    brain = None
    if args.brain == "auto":
        try:
            from brain import Brain
            brain = Brain()
            if not brain.available():
                log.warning("本地大脑缺料，关掉它（没配模型时消息会直接丢弃）")
                brain = None
        except Exception as exc:  # noqa: BLE001
            log.warning("本地大脑起不来：%s", exc)
            brain = None

    bridge = Bridge(backend, args.host, args.port, http_port=args.http_port, brain=brain)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    stop = threading.Event()

    def _shutdown(*_: Any) -> None:
        if stop.is_set():
            return
        stop.set()
        # 不是 loop.stop()：那样 run_until_complete 会抛
        # "Event loop stopped before Future completed."，退出时甩一排 traceback。
        loop.call_soon_threadsafe(bridge.request_stop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except (NotImplementedError, ValueError):
            pass

    try:
        loop.run_until_complete(bridge.run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        try:
            backend.stop()
        except Exception:  # noqa: BLE001
            pass
        loop.close()


if __name__ == "__main__":
    main()
