"""_generate 的离线核对：思考模型不出正文时别再静默（打桩 urlopen，不联网、不发微信）。"""

import io
import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "wxbridge"))

import brain  # noqa: E402


class Resp:
    def __init__(self, raw: bytes):
        self._raw = raw

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def think_only():
    return Resp(json.dumps({
        "content": [{"type": "thinking", "thinking": "想了一堆", "signature": "x"}],
        "stop_reason": "max_tokens",
        "usage": {"output_tokens_details": {"thinking_tokens": 299}},
    }, ensure_ascii=False).encode("utf-8"))


def with_text(t):
    return Resp(json.dumps({
        "content": [{"type": "thinking", "thinking": "想了一下"}, {"type": "text", "text": t}],
        "stop_reason": "end_turn",
        "usage": {"output_tokens_details": {"thinking_tokens": 30}},
    }, ensure_ascii=False).encode("utf-8"))


def make(responses):
    """按顺序吐 responses，并把每次请求的 system 记下来。"""
    seen = []
    queue = list(responses)

    def fake_open(request, timeout=None):
        body = json.loads(request.data.decode("utf-8"))
        seen.append(body)
        assert body["max_tokens"] >= 1000, "max_tokens 不该还写死 300：%s" % body["max_tokens"]
        if isinstance(queue[0], Exception):
            raise queue[0]
        return queue.pop(0)

    brain.urllib.request.urlopen = fake_open
    return seen


def new_brain():
    b = brain.Brain()
    b._loaded = True
    b._persona = "你在用微信跟熟人聊天。"
    b._key = "k"
    b._remember = lambda *a, **k: None
    b._endpoint = lambda: {"baseURL": "https://api.example.com", "api": "anthropic",
                           "model": "example-reasoning-model", "key": "k"}
    return b


results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, ("  | " + detail) if detail else ""))


# 1) 第一次只出思考 → 压一句重试，第二次出正文
seen = make([think_only(), with_text("在开会，等下说")])
b = new_brain()
got = b._generate("张三", "在吗")
check("只出思考时会自动重试一次", got == "在开会，等下说", repr(got))
check("重试那次带上「直接回」的要求", len(seen) == 2 and "【直接回】" in str(seen[1]["system"]),
      "发了 %d 次请求" % len(seen))

# 2) 两次都只出思考 → 返回空，但 last_error 要说清原因（别再是「无返回」）
seen = make([think_only(), think_only()])
b = new_brain()
got = b._generate("张三", "在吗")
check("两次都空就回空串", got == "", repr(got))
check("last_error 写明只出思考", "只出思考" in b.last_error, b.last_error)

# 3) 违禁词重试还管用
seen = make([with_text("好的主人"), with_text("行，知道了")])
b = new_brain()
got = b._generate("张三", "在吗")
check("踩了违禁词会重写", got == "行，知道了", repr(got))
check("重写那次带上重写要求", len(seen) == 2 and "【重写要求】" in str(seen[1]["system"]))

# 4) 正常一句话：一次就够
seen = make([with_text("嗯嗯 我在")])
b = new_brain()
got = b._generate("张三", "在吗")
check("正常返回不多打一次", got == "嗯嗯 我在" and len(seen) == 1, "%r 请求%d次" % (got, len(seen)))

# 5) HTTP 出错：返回空 + last_error 有内容
seen = make([urllib.error.HTTPError("https://api.example.com", 401, "Unauthorized", {}, io.BytesIO(b'{"e":1}'))])
b = new_brain()
got = b._generate("张三", "在吗")
check("401 不抛异常", got == "", repr(got))
check("401 写进 last_error", b.last_error.startswith("HTTP 401"), b.last_error)

# 6) 拆条 + 生成串起来：思考模型那句也能变成 2 条
seen = make([with_text("在开会，等下跟你说")])
b = new_brain()
parts = b.reply_parts("张三", "在吗")
check("整条链路拆成两条", parts == ["在开会", "等下跟你说"], repr(parts))

def oai_text(t):
    return Resp(json.dumps({"choices": [{"message": {"content": t}, "finish_reason": "stop"}]},
                           ensure_ascii=False).encode("utf-8"))


# 7) baseURL 的常见写法都要拼成对的地址 —— 有人把服务商给的整条 endpoint 粘进来，
#    旧代码会拼出 /v1/chat/completions/v1/chat/completions，测试必然 404。
URL_CASES = [
    ("openai", "http://127.0.0.1:11434/v1", "http://127.0.0.1:11434/v1/chat/completions"),
    ("openai", "http://127.0.0.1:11434/v1/", "http://127.0.0.1:11434/v1/chat/completions"),
    ("openai", "https://api.openai.com", "https://api.openai.com/v1/chat/completions"),
    ("openai", "https://api.openai.com/v1/chat/completions",
     "https://api.openai.com/v1/chat/completions"),
    ("openai", "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"),
    ("openai", "https://generativelanguage.googleapis.com/v1beta/openai",
     "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"),
    ("anthropic", "https://api.anthropic.com", "https://api.anthropic.com/v1/messages"),
    ("anthropic", "https://api.anthropic.com/v1", "https://api.anthropic.com/v1/messages"),
    ("anthropic", "https://api.anthropic.com/v1/messages", "https://api.anthropic.com/v1/messages"),
]
for kind, base, want in URL_CASES:
    got = brain.Brain._url_of({"baseURL": base, "api": kind})
    check("URL %s %s" % (kind, base), got == want, got)

# 8) 自己机器 / 局域网里的推理服务本来就没密钥
for base, want in [("http://127.0.0.1:11434/v1", True), ("http://localhost:1234/v1", True),
                   ("http://192.168.1.20:8000/v1", True), ("http://10.0.0.5:8080/v1", True),
                   ("http://[::1]:11434/v1", True), ("http://nas.local:11434/v1", True),
                   ("http://host.docker.internal:11434/v1", True),
                   ("https://api.openai.com/v1", False), ("http://8.8.8.8:11434/v1", False)]:
    check("本地判定 %s" % base, brain._is_local_server(base) is want, str(want))


def plain(base, key=None):
    """一个只认 config.yaml 那套的大脑（不碰 brain.json）。"""
    b = brain.Brain()
    b._loaded = True
    b._persona = "你在用微信跟熟人聊天。"
    b._key = key
    b._llm = {"baseURL": base, "api": "openai", "model": "qwen2.5:7b"}
    return b


# 9) 样例配置就是「本地 Ollama + 密钥留空」，这种必须算配好了，别再判「没配好」
check("本地模型没密钥也算可用", plain("http://127.0.0.1:11434/v1").available() is True)
check("局域网模型也算可用", plain("http://192.168.1.20:8000/v1").available() is True)
check("云端地址没密钥才算不可用", plain("https://api.openai.com/v1").available() is False)

# 10) 「测试连接」：用页面刚填、还没保存的那组参数打；没密钥就别编一个出去
reqs = []
queue = [oai_text("在的")]


def spy(request, timeout=None):
    reqs.append(request)
    first = queue[0]
    if callable(first):
        raise first()              # 每次现造一个新的：body 只能读一遍，重试那次也得有
    if isinstance(first, Exception):
        raise first                # 错一直报，重试那次也是同一个错
    return queue.pop(0)


brain.urllib.request.urlopen = spy
b = plain("https://api.openai.com/v1")          # 存的是云端，测的是刚填的本地
r = b.probe(api="openai", baseURL="http://127.0.0.1:9899/v1", model="fake-model", apiKey="")
check("测的是刚填那组没保存的值",
      reqs and reqs[-1].full_url == "http://127.0.0.1:9899/v1/chat/completions",
      reqs[-1].full_url if reqs else "没发请求")
check("没密钥就别发 authorization 头",
      reqs and reqs[-1].get_header("Authorization") is None,
      str(reqs[-1].get_header("Authorization")) if reqs else "")
check("probe 通了就回 ok", r.get("ok") is True and r.get("reply") == "在的", repr(r)[:120])
check("probe 不留在会话历史里", "__probe__" not in b._history, repr(list(b._history)))


def http_err(code, msg, body):
    return lambda: urllib.error.HTTPError("http://x", code, msg, {}, io.BytesIO(body))


queue[:] = [http_err(404, "Not Found", b'{"e":404}')]
r = b.probe(api="openai", baseURL="http://127.0.0.1:9899/v1", model="fake-model", apiKey="")
check("404 翻译成人话（地址填到哪一层）",
      r.get("ok") is False and "404" in r.get("error", "") and "/v1" in r.get("error", ""),
      r.get("error", "")[:100])

queue[:] = [urllib.error.URLError("ConnectionRefusedError [WinError 10061] 拒绝连接")]
r = b.probe(api="openai", baseURL="http://127.0.0.1:9899/v1", model="fake-model", apiKey="")
check("连不上会说服务没开 / 端口", "连不上" in r.get("error", ""), r.get("error", "")[:100])

queue[:] = [http_err(400, "Bad Request", b'{"error":{"message":"The model does not exist"}}')]
r = b.probe(api="openai", baseURL="http://127.0.0.1:9899/v1", model="fake-model", apiKey="")
check("模型名不对要说模型名", "模型名" in r.get("error", ""), r.get("error", "")[:100])

b0 = brain.Brain()
b0._loaded = True
b0._persona = "你在用微信跟熟人聊天。"
b0._key = None
b0._llm = {}
r = b0.probe(api="openai", baseURL="", model="")
check("地址模型都空时说清缺什么", r.get("ok") is False and "baseURL" in r.get("error", ""),
      r.get("error", "")[:100])

# 11) 密钥框平时是空的：保存时留空 = 不改动，不能把已存的密钥擦掉
import pathlib  # noqa: E402
import tempfile  # noqa: E402
import brain_store  # noqa: E402

brain_store.CONFIG_FILE = pathlib.Path(tempfile.mkdtemp()) / "brain.json"
brain_store.save("openai", "https://a.example.com/v1", "m1", "kk-old-11111111")
brain_store.save("openai", "https://a.example.com/v1", "m2", "")
check("密钥留空保存不会擦掉旧密钥", brain_store.load()["apiKey"] == "kk-old-11111111",
      repr(brain_store.load()["apiKey"]))
brain_store.save("openai", "https://a.example.com/v1", "m2", "kk-new-22222222")
check("填了新密钥就替换", brain_store.load()["apiKey"] == "kk-new-22222222",
      repr(brain_store.load()["apiKey"]))
brain_store.save("openai", "", "", "", clear_key=True)
check("显式清空才真的清掉", brain_store.load()["apiKey"] == "", repr(brain_store.load()))

bad = [n for n, ok, _ in results if not ok]
print("\n通过 %d 项，失败 %d 项" % (len(results) - len(bad), len(bad)))
sys.exit(1 if bad else 0)
