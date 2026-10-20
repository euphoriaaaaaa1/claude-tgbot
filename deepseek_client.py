"""DeepSeek HTTP 客户端（共享）。

直接 urllib HTTP 调 DeepSeek Anthropic-compatible API，复用
_global.yml.jiwen.delta_llm 配置（base_url / model / proxy）；密钥经
config_loader.get_deepseek_api_key() 取自本机 configs/_secrets.yml 的 deepseek.api_key
（不进 git；旧写法 jiwen.delta_llm.api_key 仍兼容）。

优势 vs claude_cli.call_claude*：
- 不受 cc-profile 切换影响（永远用配置里的 deepseek key/model）
- 不烧 Claude OAuth 配额
- 比 CLI 启动快 3 倍（实测 4.3s vs 14.6s）

API:
- call_text(prompt, timeout, max_tokens) → str          # 纯文本输出
- call_json(prompt, timeout, max_tokens) → dict          # 强制 JSON 解析

caller：jiwen/deepseek_delta、moments/post（朋友圈翻译）、signature、
       claude_cli（DeepSeek 模式下分流）
"""
from __future__ import annotations
import json
import socket
import sys
import urllib.error
import urllib.request

# 拿不到密钥时的报错文案（写死，不带任何值）；保留旧前缀「deepseek api_key 未配置」
_NO_KEY_MESSAGE = ("deepseek api_key 未配置（configs/_secrets.yml 的 deepseek.api_key；"
                   "旧写法 _global.yml.jiwen.delta_llm.api_key 仍兼容）")


class LLMError(ValueError):
    """模型回复不可用。reason：empty / truncated / bad_json。消息只带长度，不带模型输出。"""

    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


def build_body(model: str, prompt: str, max_tokens: int, *, thinking: str = "default") -> dict:
    """统一的 Anthropic 风格请求体。thinking="disabled" 时多一个关闭思考的键。"""
    if thinking not in ("default", "disabled"):
        raise ValueError("thinking 只能是 default 或 disabled")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
        raise ValueError("max_tokens 必须是正整数")
    if not isinstance(model, str) or not model:
        raise ValueError("model 必须是非空字符串")
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("prompt 必须是非空字符串")
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if thinking == "disabled":
        body["thinking"] = {"type": "disabled"}
    return body


def _post(prompt: str, timeout: int, max_tokens: int) -> dict:
    """共享底层 HTTP POST。返回响应 JSON。"""
    import config_loader as _cfg
    g = _cfg.load_global()
    delta_cfg = ((g.get("jiwen") or {}).get("delta_llm") or {})
    api_key = _cfg.get_deepseek_api_key(delta_cfg)
    if not api_key:
        raise RuntimeError(_NO_KEY_MESSAGE)

    body = build_body(delta_cfg.get("model", "deepseek-v4-flash"), prompt, max_tokens)
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    base_url = delta_cfg.get("base_url", "https://api.deepseek.com/anthropic")
    url = f"{base_url.rstrip('/')}/v1/messages"
    proxy = delta_cfg.get("proxy", "")  # 空字符串=直连
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": proxy, "http": proxy})
        )
    else:
        opener = urllib.request.build_opener()
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                  headers=headers, method="POST")
    with opener.open(req, timeout=timeout) as r:
        return json.load(r)


def _stop_reason(resp) -> str | None:
    sr = resp.get("stop_reason") if isinstance(resp, dict) else None
    return sr if isinstance(sr, str) else None


def extract_text(resp: dict) -> tuple[str, str | None]:
    """取第一个非空 text 块：(正文, stop_reason)。没有正文抛 LLMError（truncated / empty）。"""
    content = resp.get("content") if isinstance(resp, dict) else None
    for c in (content if isinstance(content, list) else []):
        if isinstance(c, dict) and c.get("type") == "text":
            text = c.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip(), _stop_reason(resp)
    reason = "truncated" if _stop_reason(resp) == "max_tokens" else "empty"
    raise LLMError(reason, "LLM 回复没有正文（reason=%s）" % reason)


def describe(resp: dict) -> str:
    """只含状态量的一段文字：stop_reason、块类型、输出 token 数。形状不对写 ?，不抛。"""
    if not isinstance(resp, dict):
        return "stop_reason=? blocks=? out_tokens=?"
    sr = resp.get("stop_reason")
    content = resp.get("content")
    if isinstance(content, list):
        blocks = [str(c.get("type", "?")) if isinstance(c, dict) else "?" for c in content]
        blocks_s = "[%s]" % ",".join(blocks)
    else:
        blocks_s = "?"
    usage = resp.get("usage")
    out = usage.get("output_tokens") if isinstance(usage, dict) else None
    return "stop_reason=%s blocks=%s out_tokens=%s" % (
        sr if isinstance(sr, str) else "?", blocks_s, out if isinstance(out, int) else "?")


def classify_error(exc: BaseException) -> str:
    """把异常归成固定取值，供日志与退避记账使用。"""
    if isinstance(exc, LLMError):
        return exc.reason
    if isinstance(exc, urllib.error.HTTPError):
        return "http_%s" % exc.code
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, urllib.error.URLError):
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            return "timeout"
        return "network"
    if isinstance(exc, json.JSONDecodeError):
        return "bad_json"
    return type(exc).__name__


def _log_fail(reason: str, resp) -> None:
    print("[llm] 失败: reason=%s %s" % (reason, describe(resp)), file=sys.stderr)


def call_text(prompt: str, timeout: int = 60, max_tokens: int = 4000) -> str:
    """纯文本输出。没有正文返回空串（留一行失败日志）。"""
    resp = _post(prompt, timeout=timeout, max_tokens=max_tokens)
    try:
        return extract_text(resp)[0]
    except LLMError as e:
        _log_fail(e.reason, resp)
        return ""


async def call_text_stream(prompt: str, timeout: int = 30, max_tokens: int = 256):
    """流式输出：async generator，逐块 yield 文本增量（Anthropic SSE content_block_delta）。
    只给 voicecall 通话用（需要边生成边合成语音）；httpx lazy import，不影响同步 caller。"""
    import httpx  # lazy：只有 voicecall（voice-bridge venv 有 httpx）会调，jiwen 等不受影响
    import config_loader as _cfg
    g = _cfg.load_global()
    delta_cfg = ((g.get("jiwen") or {}).get("delta_llm") or {})
    api_key = _cfg.get_deepseek_api_key(delta_cfg)
    if not api_key:
        raise RuntimeError(_NO_KEY_MESSAGE)
    body = {
        "model": delta_cfg.get("model", "deepseek-v4-flash"),
        "max_tokens": max_tokens,
        "stream": True,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {"Content-Type": "application/json", "x-api-key": api_key,
               "anthropic-version": "2023-06-01"}
    base_url = delta_cfg.get("base_url", "https://api.deepseek.com/anthropic")
    url = f"{base_url.rstrip('/')}/v1/messages"
    proxy = delta_cfg.get("proxy", "") or None
    async with httpx.AsyncClient(timeout=timeout, proxy=proxy) as client:
        async with client.stream("POST", url, json=body, headers=headers) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    evt = json.loads(data)
                except Exception:
                    continue
                if evt.get("type") == "content_block_delta":
                    d = evt.get("delta", {})
                    if d.get("type") == "text_delta" and d.get("text"):
                        yield d["text"]


def call_json(prompt: str, timeout: int = 60, max_tokens: int = 800) -> dict:
    """强制 JSON 解析。没有正文或解析不出 dict 抛 LLMError（caller 自行 fallback）。"""
    resp = _post(prompt, timeout=timeout, max_tokens=max_tokens)
    try:
        text, stop_reason = extract_text(resp)
    except LLMError as e:
        _log_fail(e.reason, resp)
        raise
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(l for l in lines if not l.startswith("```"))
    parsed = None
    if "{" in text and "}" in text:
        try:
            parsed = json.loads(text[text.index("{"):text.rindex("}") + 1])
        except ValueError:
            parsed = None
    if isinstance(parsed, dict):
        return parsed
    # 正文写到一半被截断时按 truncated 归因，其余算 bad_json；消息只带长度，不带模型输出
    reason = "truncated" if stop_reason == "max_tokens" else "bad_json"
    _log_fail(reason, resp)
    raise LLMError(reason, "LLM 回复里没有可解析的 JSON（len=%d, reason=%s）" % (len(text), reason))
