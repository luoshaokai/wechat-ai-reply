"""桥接自带的大脑（本地大脑）。

回复的「大脑」就是这个模块：照着你自己的人设直接调模型，不依赖任何外部程序。
桥接进程自己就能把「收到消息 → 想一句 → 发出去」跑完，界面开着关着都一样。

配置来源（优先级从高到低）：

1. 运行时存档 ``brain.json`` / ``style.json``——网页控制台改完写在这儿，即时生效；
2. 项目根目录 ``config.yaml`` 的 ``llm`` 段和 ``persona`` 段（解析在 appconfig）；
3. 环境变量 ``WXREPLY_API_KEY``（密钥建议走环境变量，别写进文件）。

密钥只在内存里用，不打印、不落盘（存档里的 apiKey 字段由控制台自己管理，
.gitignore 已经把 config.yaml 和 brain.json 排除掉了）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import appconfig

log = logging.getLogger("wxbridge.brain")

HERE = Path(__file__).resolve().parent

# 人设兜底：config.yaml 没写 persona 时至少别冷启动。
FALLBACK_PERSONA = (
    "你正在用微信跟熟人聊天。你就是号主本人，是活生生的人，不是 AI。"
    "每条最多 20 个字，一句话，口语化，不用表情符号，不用 markdown，不写长文。"
)

# 微信里不想看到的排版痕迹。
_MD_HEAD = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_MD_LIST = re.compile(r"^\s{0,6}(?:[-*+]|\d+[.)])\s+", re.MULTILINE)
_MD_EMPH = re.compile(r"(\*\*|__|\*|_|`)")
_MD_FENCE = re.compile(r"```.*?```", re.DOTALL)
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF\U0000FE0F\U00002B00-\U00002BFF]+"
)
_SPACES = re.compile(r"[ \t]+")
_BLANKS = re.compile(r"\n{2,}")


#: 模型想「连发几条」时用这个分隔（真人聊嗨了会分两下说，更像人）。
SPLIT = "|||"
#: 最多拆几条。现在的规则：默认整条发，正文里有逗号/句号/问号才拆成 2 条；再多就像刷屏了。
MAX_PARTS = 2
#: 每次调用给模型的输出额度——不给回复设长度上限，所以这里只是「给足」。
#: 额度太小会踩坑：碰上思考型（reasoning）模型，那点 token 全花在 thinking 上，
#: 正文一个字都出不来，表现就是「整条静默不回」。
#: 模型自己会 end_turn，不会真写 8000 字，日常一句还是十几二十个字。
MAX_OUTPUT_TOKENS = 8000

#: 不管风格怎么写，这些话一句都不能出现（客服腔 / 自曝 AI / 乱叫人）。
_BANNED = ("主人", "老板", "用户", "学弟", "助手", "作为AI", "作为 AI", "我是AI", "我是 AI",
           "有什么可以帮你", "希望对你有帮助", "作为一个")
#: 风格里写「最多 N 字」就按它来。
_STYLE_LIMIT = re.compile(r"最多\s*(\d{1,3})\s*个?字")


def style_length_limit(text: str) -> Optional[int]:
    """从风格正文里读出字数上限（没写就 None）。"""
    found = _STYLE_LIMIT.search(text or "")
    if not found:
        return None
    try:
        value = int(found.group(1))
    except ValueError:
        return None
    return value if 4 <= value <= 400 else None


#: 分条用的标点：中文逗号、句号、问号，外加英文逗号。顿号、分号、感叹号不算；
#: 英文句点和英文问号也不算——不然「1.5 块」和网址后面的 ?a=1 会被切成两截。
_SEP = re.compile(r"[，。？,]")


def split_at_punct(text: str, max_parts: int = MAX_PARTS) -> List[str]:
    """默认整条发出去；正文里有逗号/句号/问号才拆成 2 条，像真人分两下说。

    以前按所有标点强拆 1~3 条，结果一句话被切成三条刷屏，后来改成只认逗号句号问号。
    现在只认逗号、句号、问号，最多两条：在第一个「两边都还剩俩字以上」的
    分隔符处切一刀，后面再有的标点原样留着；两边不足 2 个字的（「嗯嗯，好」
    「行，我知道了」）跳过不切，免得发出去一个单字泡泡。
    """
    text = (text or "").strip()
    if not text or max_parts < 2:
        return [text] if text else []
    for mark in _SEP.finditer(text):
        if mark.start() >= 2 and len(text) - mark.end() >= 2:
            parts = [text[:mark.start()].strip(), text[mark.end():].strip()]
            return [p for p in parts if p][:max_parts]
    return [text]


def banned_words(text: str) -> List[str]:
    """这句话里踩了哪些不该说的词。"""
    return [w for w in _BANNED if w in (text or "")]


#: 人设里那些「字数上限」的软规则：一旦用户写了自己的风格，就把它们摘掉，
#: 否则模型两边都看到，最后按哪条全看运气（表现就是「不按风格来」）。
_LEN_RULE = re.compile(
    r"^.*?(?:最多\s*\d+\s*个?字|一条最多|能短就短|别写长文|7~8\s*个字).*$", re.MULTILINE)


def _strip_length_rules(text: str) -> str:
    """去掉人设里的字数限制，只留硬底线。"""
    return _LEN_RULE.sub("", text).strip()


def _clean(text: str, limit: int = 400) -> str:
    """把模型的话收拾成微信里像人发的一句。"""
    out = _MD_FENCE.sub("", text or "")
    out = _MD_LINK.sub(r"\1", out)
    out = _MD_HEAD.sub("", out)
    out = _MD_LIST.sub("", out)
    out = _MD_EMPH.sub("", out)
    out = _EMOJI.sub("", out)
    out = out.replace("\u00a0", " ")
    out = _SPACES.sub(" ", out)
    out = _BLANKS.sub("\n", out).strip()
    if len(out) > limit:
        cut = out[:limit]
        for sep in ("。", "！", "？", "，", " ", "\n"):
            pos = cut.rfind(sep)
            if pos >= limit * 0.5:
                cut = cut[: pos + 1]
                break
        out = cut.strip() + ("…" if len(out) > len(cut) else "")
    return out.strip()


#: 自己搭的推理服务（Ollama / LM Studio / llama.cpp / vLLM）默认不设密钥，
#: 所以「本机或局域网 + 密钥留空」是正常配置，不能当成缺料。
_LOCAL_NAME = re.compile(
    r"^(localhost|host\.docker\.internal|.*\.localhost|.*\.local)$", re.I)
_PRIVATE_NET = re.compile(
    r"^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|\[?::1\]?)")


def _is_local_server(base_url: str) -> bool:
    """这串地址是不是用户自己机器/局域网里的模型服务（这类通常没有密钥）。"""
    raw = str(base_url or "").strip().lower()
    if not raw:
        return False
    for scheme in ("https://", "http://", "://"):
        if raw.startswith(scheme):
            raw = raw[len(scheme):]
            break
    host = raw.split("/", 1)[0].split("@")[-1]
    if host.startswith("["):                      # IPv6 字面量：[::1]:11434
        host, _, rest = host.partition("]")
        host = host + "]"
        port, _, _ = rest.partition(":")
        if port:
            host += ":" + port
    elif ":" in host:
        host = host.split(":", 1)[0]
    host = host.strip("[]")
    return bool(_PRIVATE_NET.match(host) or _LOCAL_NAME.match(host))


class Brain:
    """按人设调模型的最小实现：一个会话一份短历史。"""

    def __init__(self, timeout: float = 60.0, history_size: int = 8) -> None:
        self.timeout = float(timeout)
        self.history_size = int(history_size)
        self._lock = threading.Lock()
        self._history: Dict[str, List[Dict[str, str]]] = {}
        self._persona: Optional[str] = None
        self._llm: Dict[str, Any] = {}
        self._key: Optional[str] = None
        self._loaded = False
        #: 控制台「测试连接」时临时指定的一组参数（不写盘、测完就清）。
        self._probe: Optional[Dict[str, Any]] = None
        #: 最近一次调用失败的原因（给控制台「测试连接」显示，别只说「没拿到回复」）。
        self.last_error: str = ""

    # ------------------------------------------------------------------ 配置

    def _load(self) -> None:
        """一次性把人设、端点、密钥读齐。"""
        if self._loaded:
            return
        self._loaded = True
        self._persona = self._load_persona()
        self._llm = self._load_llm()
        self._key = self._load_key()
        local = _is_local_server(str(self._llm.get("baseURL") or ""))
        if self._persona and self._llm and (self._key or local):
            log.info("本地大脑就绪：%s（模型 %s，%s）", self._llm.get("baseURL"),
                     self._llm.get("model"), "本地服务，无需密钥" if local else "云端")
        else:
            log.warning(
                "本地大脑缺料（人设=%s 端点=%s 密钥=%s）——请编辑 config.yaml，"
                "或者打开网页控制台填「模型」和「风格」",
                bool(self._persona), bool(self._llm), bool(self._key or local),
            )

    def _system_prompt(self) -> str:
        """硬底线 + 用户写的那段风格。

        为什么要把人设里的「字数上限」摘掉：用户写了自己的角色卡（可能允许说长一点），
        人设里还留着「最多 20 字」，模型两边都看到，最后按哪条全看运气——
        表现就是「回复不按风格来」。所以有自定义风格时，只留硬底线。
        """
        base = self._persona or FALLBACK_PERSONA
        try:
            import style_store
            text = style_store.effective()
            default_text = style_store.PRESETS[0]["text"].strip()
        except Exception:  # noqa: BLE001 - 读不到风格就只用人设
            return base
        if not text.strip():
            return base
        # 有自定义风格时，人设只留一小段（否则 2KB 人设会把风格冲淡），
        # 而且风格排在前面当正文，硬底线压最后——模型对最后的指令最敏感。
        if text.strip() == default_text:
            head = base
        else:
            head = "你在用微信跟熟人聊天，对方是你的朋友。你不是助手，也不是 AI。"
        # 放在最后：模型对「最后一条要求」最敏感，这条永远不被风格覆盖。
        return (f"{head}\n\n【说话风格：这就是你的提示词，照它说】\n{text}"
                "\n\n【几条铁律（永远有效，风格不能覆盖）】"
                "\n- 被问「你是谁」：回「我啊」「咋了」「这都认不出」这种，不要说「我是这个号的本人」——那不是人话。"
                "\n- 被问「是不是 AI / 机器人」：回「你猜」「看像吗」「说啥呢」。"
                "\n- 不叫对方「主人」「老板」「用户」「学弟」；不自称助手或 AI；不用任何表情符号。"
                "\n- 不说「有什么可以帮你」「希望对你有帮助」；不像客服，不讲道理，不总结。"
                "\n- 只聊你们两个人之间的事。绝不提、不转述、不引用其他任何人的聊天内容（就算你记得），"
                "也不报别人的名字或隐私。对方问「别人跟你说啥了」就回「我俩的事你问那么多干啥」这类。"
                "\n- 对方提到一个你不确定认识的人：就说「谁啊」「不认识」，别装作认识、别编关系。"
                "\n- 你写的字默认整条发出去，只有句中出现逗号、句号、问号才会拆成两条发（最多两条），"
                "所以一句话说一件事就够了，别堆长句、别用分号列点。"
                "\n- 对方那条只有 `[表情]` 三个字，是他发了个表情包（看不出是哪个），就按「收到一个表情」接话，"
                "别把「[表情]」原样回出去、也别问「这是啥表情」；句末的 `（他引用了「…」）` 是他在回上面某一句，"
                "按那一句的意思接。")

    def _load_persona(self) -> str:
        text = appconfig.persona_text()
        if text:
            log.info("人设取自 config.yaml")
            return text
        log.warning("config.yaml 没写 persona，先用内置兜底人设（建议改成你自己的说法）")
        return FALLBACK_PERSONA

    def _load_llm(self) -> Dict[str, Any]:
        """读 config.yaml 的 llm 段：端点、协议、模型名。

        注意这三个必须成对——拿 A 家的地址去问 B 家的模型，只会得到一个 401 或者
        「模型不存在」。控制台里填的（brain.json）优先级更高，见 :meth:`_endpoint`。
        """
        got = appconfig.llm_settings()
        base = str(got.get("baseURL") or "").strip().rstrip("/")
        if not base:
            return {}
        api = str(got.get("api") or "openai").strip().lower()
        if api.startswith("anthropic"):
            api = "anthropic"
        elif api.startswith("openai"):
            api = "openai"
        else:
            log.warning("llm.api=%r 不认得，按 openai 处理（支持 openai / anthropic）", api)
            api = "openai"
        return {"baseURL": base, "api": api, "model": str(got.get("model") or "").strip()}

    def _load_key(self) -> Optional[str]:
        """密钥来源：环境变量优先，其次 config.yaml 的 llm.apiKey。"""
        for name in ("WXREPLY_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            env = os.environ.get(name)
            if env and env.strip():
                return env.strip()
        key = str((appconfig.llm_settings() or {}).get("apiKey") or "").strip()
        return key or None

    # ------------------------------------------------------------------ 对话

    def available(self) -> bool:
        """人设、端点、密钥齐了才算能用（本地模式不强制要密钥）。"""
        self._load()
        ep = self._endpoint()
        if not (self._persona and ep["baseURL"] and ep["model"]):
            return False
        return bool(ep["key"]) or bool(ep.get("local"))

    def _messages(self, chat: str, text: str) -> List[Dict[str, str]]:
        with self._lock:
            history = self._history.setdefault(chat, [])
            messages = list(history)
            messages.append({"role": "user", "content": text})
            return messages

    def _remember(self, chat: str, text: str, answer: str) -> None:
        with self._lock:
            history = self._history.setdefault(chat, [])
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": answer})
            if len(history) > self.history_size * 2:
                del history[: len(history) - self.history_size * 2]

    # ------------------------------------------------------------------ 端点

    def _endpoint(self) -> Dict[str, Any]:
        """这次用哪个端点：测试用的临时参数 > 控制台填的 > config.yaml 里的。"""
        if self._probe:
            return dict(self._probe)
        try:
            import brain_store
            cfg = brain_store.load()
            if brain_store.configured(cfg):
                base = cfg["baseURL"].rstrip("/")
                local = _is_local_server(base)
                # 本地服务不假造一把叫 "local" 的密钥：真要鉴权请在密钥框里填。
                return {"baseURL": base, "api": cfg["api"], "model": cfg["model"],
                        "key": cfg["apiKey"] or (self._key or ""), "local": local}
        except Exception as exc:  # noqa: BLE001 - 存档读不了就退回配置文件
            log.warning("读控制台存的模型配置失败，退回 config.yaml：%s", exc)
        base = str(self._llm.get("baseURL") or "").rstrip("/")
        return {"baseURL": base,
                "api": str(self._llm.get("api") or "openai"),
                "model": str(self._llm.get("model") or ""),
                "key": self._key or "", "local": _is_local_server(base)}

    @staticmethod
    def _url_of(ep: Dict[str, Any]) -> str:
        """把 baseURL 拼成真正要打的那个地址。

        大家习惯把服务商文档里的完整 endpoint 整条粘进来（`.../v1/chat/completions`、
        Anthropic 的 `.../v1`），这里先把尾部重复的一段剥掉再拼，否则得到
        `/v1/chat/completions/v1/chat/completions` 这种地址，只能是 404。
        """
        base = str(ep.get("baseURL") or "").strip().rstrip("/")
        low = base.lower()
        if ep.get("api") == "openai":
            for tail in ("/chat/completions", "/completions"):
                if low.endswith(tail):
                    base = base[: -len(tail)].rstrip("/")
                    low = base.lower()
                    break
            # 已经带版本号就不补 /v1：/v1、/compatible-mode/v1、/v1beta/openai 都算
            if not re.search(r"/v\d+", low):
                base += "/v1"
            return base + "/chat/completions"
        for tail in ("/messages", "/v1"):
            if low.endswith(tail):
                base = base[: -len(tail)].rstrip("/")
                low = base.lower()
        return base + "/v1/messages"

    def probe(self, api: Optional[str] = None, baseURL: Optional[str] = None,
              model: Optional[str] = None, apiKey: Optional[str] = None) -> Dict[str, Any]:
        """真打一句话试通不通（控制台「测试连接」）。不写盘、不留在会话历史里。

        参数就是页面上那四个框：填了就用这组试（还没保存也能试），
        空着的沿用已存/配置文件里的值 —— 否则用户改完地址直接点测试，
        测的却是上一条保存过的配置，只会得到一个莫名其妙的失败。
        """
        stored = self._endpoint()
        base = str(baseURL or "").strip() or str(stored.get("baseURL") or "")
        name = str(model or "").strip() or str(stored.get("model") or "")
        kind = str(api or "").strip().lower() or str(stored.get("api") or "openai")
        kind = "anthropic" if kind.startswith("anthropic") else "openai"
        key = str(apiKey or "").strip() or str(stored.get("key") or "")
        ep = {"baseURL": base, "api": kind, "model": name, "key": key,
              "local": _is_local_server(base)}
        info = {"model": name, "url": self._url_of(ep), "local": ep["local"]}
        if not base or not name:
            miss = "模型服务地址（baseURL）" if not base else "模型名（model）"
            return {"ok": False, "error": f"还缺{miss}，地址和模型名都要填。", "info": info}
        self._probe = ep
        try:
            raw = self._generate("__probe__", "在吗")
        finally:
            self._probe = None
            with self._lock:
                self._history.pop("__probe__", None)
        if raw.strip():
            return {"ok": True, "reply": _clean(raw, limit=200), "info": info}
        return {"ok": False, "error": self._friendly_error(self.last_error, info), "info": info}

    def _friendly_error(self, err: str, info: Dict[str, Any]) -> str:
        """把原始报错翻译成人能照着修的一句话。"""
        e = str(err or "").lower()
        url = str(info.get("url") or "")
        where = "本地服务" if info.get("local") else "云端地址"
        if not e:
            return "模型只给了思考、没给正文（重试一次还是空）。换成指令型模型（名字带 -chat / -instruct 的）试试。"
        if "does not exist" in e or "not_found" in e or "invalid model" in e or "unknown model" in e:
            return f"模型名不对，服务里没有这个模型：{info.get('model')}（以服务商列出的名字为准，大小写要一致）"
        if "401" in e or "403" in e or "unauthorized" in e or "forbidden" in e:
            return (f"密钥被拒（401/403）。云端服务必须填对 apiKey；{where}地址不用填密钥。"
                    "注意密钥别带引号、别多带空格。")
        if "404" in e or "not found" in e:
            return (f"地址打不到（404），实际打的是 {url}。baseURL 只填到版本号那一层："
                    "OpenAI 兼容以 /v1 结尾，别把 /chat/completions 也粘进来；Anthropic 填根地址。")
        if "timed out" in e or "timeout" in e:
            return f"等了 {int(self.timeout)} 秒还没回。本地模型第一次加载很慢（大模型要一两分钟），再点一次试试；或换小一点的模型。"
        if "ssl" in e or "certificate" in e:
            return "HTTPS 证书校验没过，通常是走了代理或自签证书：把 baseURL 换成服务自己的直连地址。"
        if "getaddrinfo" in e or "name or service not known" in e or "unknown host" in e:
            return "这个域名解析不了：baseURL 是不是漏了 http:// 或 https://，或者地址写错了。"
        if "refused" in e or "10061" in e or "unable to connect" in e or "couldn't connect" in e:
            return (f"连不上 {url}。{where}：确认服务已经启动、端口对不对；"
                    "127.0.0.1 只在本机有效，服务在另一台机器上就换成它的局域网 IP。")
        return f"没通：{str(err)[:160]}"

    def reply(self, chat: str, text: str, style: Optional[str] = None) -> str:
        """一句话版本（预览、测试用）。真正发微信用 reply_parts。"""
        return "，".join(self.reply_parts(chat, text, style))

    def reply_parts(self, chat: str, text: str, style: Optional[str] = None) -> List[str]:
        """生成 1~2 条（默认整条发，正文里有逗号/句号/问号才拆两条），并且逐条过清洗和违禁词。"""
        raw = self._generate(chat, text, style)
        if not raw:
            return []
        limit = style_length_limit(self._system_prompt()) or 400
        # 模型自己用 ||| 标的分段，等同于一个逗号；然后只在逗号/句号/问号处拆条。
        joined = raw.replace(SPLIT, "，")
        parts: List[str] = []
        for piece in split_at_punct(joined, MAX_PARTS):
            cleaned = _clean(piece, limit=limit)
            if cleaned and not banned_words(cleaned):
                parts.append(cleaned)
        if not parts:
            return []
        self._remember(chat, text, "，".join(parts))
        return parts

    def _generate(self, chat: str, text: str, style: Optional[str] = None) -> str:
        """回一句。出错就返回空串（宁可不出声，也别发半句话出去）。

        @param chat - 会话名（只用来分历史）。
        @param text - 对方说的话。
        @param style - 临时风格（控制台预览用）；不给就用已保存的。
        """
        self._load()
        ep = self._endpoint()
        if not (self._persona and ep["baseURL"] and ep["model"]):
            log.warning("大脑缺料：人设=%s 端点=%s 模型=%s",
                        bool(self._persona), bool(ep["baseURL"]), bool(ep["model"]))
            return ""
        text = (text or "").strip()
        if not text:
            return ""
        history = self._messages(chat, text)
        system = self._system_prompt()
        if style is not None and str(style).strip():
            system = f"{self._persona or FALLBACK_PERSONA}\n\n【当前说话风格】\n{str(style).strip()[:2000]}"
        if ep["api"] == "openai":
            body = {
                "model": ep["model"],
                "messages": [{"role": "system", "content": system}] + history,
                "max_tokens": MAX_OUTPUT_TOKENS,
                "temperature": 0.8,
                "stream": False,
            }
            headers = {"content-type": "application/json"}
            if ep["key"]:
                headers["authorization"] = f"Bearer {ep['key']}"
        else:
            body = {
                "model": ep["model"],
                "max_tokens": MAX_OUTPUT_TOKENS,
                "temperature": 0.8,
                "system": system,
                "messages": history,
            }
            headers = {
                "content-type": "application/json",
                "x-api-key": ep["key"],
                "authorization": f"Bearer {ep['key']}",
                "anthropic-version": "2023-06-01",
            }

        def _ask(extra: str) -> str:
            """打一次模型，只取正文（text 块）。出错或没正文都返回空串。

            @param extra - 追加到系统提示后面的那句话（重写要求 / 别思考直接回）。
            """
            sent = dict(body)
            if extra:
                if ep["api"] == "openai":
                    msgs = list(body["messages"])
                    msgs[0] = {"role": "system",
                               "content": str(msgs[0].get("content") or "") + extra}
                    sent["messages"] = msgs
                else:
                    sent["system"] = str(body.get("system") or "") + extra
            request = urllib.request.Request(
                self._url_of(ep),
                data=json.dumps(sent, ensure_ascii=False).encode("utf-8"),
                headers=headers, method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:200]
                except Exception:  # noqa: BLE001
                    pass
                self.last_error = f"HTTP {exc.code}：{detail[:120]}"
                log.warning("调模型失败 %s", self.last_error)
                return ""
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"[:160]
                log.warning("调模型失败：%s", self.last_error)
                return ""
            out = ""
            stop = ""
            if ep["api"] == "openai":
                for choice in payload.get("choices") or []:
                    out += str(((choice or {}).get("message") or {}).get("content") or "")
                    stop = str((choice or {}).get("finish_reason") or "")
            else:
                for block in payload.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        out += str(block.get("text") or "")
                stop = str(payload.get("stop_reason") or "")
            if out.strip():
                self.last_error = ""
            else:
                # 思考模型最常咬的一口：token 全花在 thinking 上，正文一个字没有
                think = ((payload.get("usage") or {}).get("output_tokens_details") or {})
                log.warning("模型没出正文（stop_reason=%s 思考 token=%s）",
                            stop, think.get("thinking_tokens"))
            return out

        self.last_error = ""
        limit = style_length_limit(self._system_prompt()) or 400
        answer = _ask("")
        if not answer.strip():
            # 它在「思考」里把话说完了：压一句只要成品话，再试一次（只试一次）
            answer = _ask("\n\n【直接回】别分析、别思考、别解释，"
                          "直接给一句要发到微信上的话，20 字以内。")
        if not answer.strip():
            self.last_error = self.last_error or "模型只出思考没出正文（重试一次还是空）"
            return ""
        answer = _clean(answer, limit=limit)
        bad = banned_words(answer)
        if bad:
            # 第一遍说了不该说的话（比如"主人"、客服腔）：带上这句提醒再问一次，
            # 只重试一次，避免死循环。
            log.info("这一句踩了 %s，重写一次", bad)
            note = ("\n\n【重写要求】上一句里出现了「" + "、".join(bad) +
                    "」，这些词一个都不能有。换一种正常微信聊天的话说，别解释，别重复上一句。")
            second = _clean(_ask(note), limit=limit)
            if second and not banned_words(second):
                answer = second
                bad = []
        if answer and bad:
            # 重写也没过：宁可只发前半句，也别把「主人」发出去。
            log.warning("重写后仍有违禁词 %s，按句切断", bad)
            for cut in ("。", "！", "？", "\n"):
                if cut in answer:
                    answer = answer.split(cut)[0] + cut
                    break
            else:
                answer = ""
        return answer
