"""DeepSeek HTTP 客户端（共享）。

直接 urllib HTTP 调 DeepSeek Anthropic-compatible API，复用
_global.yml.jiwen.delta_llm 配置（base_url / api_key / model / proxy）。

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
import urllib.request


def _post(prompt: str, timeout: int, max_tokens: int) -> dict:
    """共享底层 HTTP POST。返回响应 JSON。"""
    import config_loader as _cfg
    g = _cfg.load_global()
    delta_cfg = ((g.get("jiwen") or {}).get("delta_llm") or {})
    api_key = delta_cfg.get("api_key", "")
    if not api_key:
        raise RuntimeError("deepseek api_key 未配置（_global.yml.jiwen.delta_llm.api_key）")

    body = {
        "model": delta_cfg.get("model", "deepseek-v4-flash"),
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
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


def _extract_text(resp: dict) -> str:
    """从 Anthropic 响应里取 text content。"""
    for c in resp.get("content", []):
        if c.get("type") == "text":
            return (c.get("text") or "").strip()
    return ""


def call_text(prompt: str, timeout: int = 60, max_tokens: int = 4000) -> str:
    """纯文本输出。"""
    resp = _post(prompt, timeout=timeout, max_tokens=max_tokens)
    return _extract_text(resp)


async def call_text_stream(prompt: str, timeout: int = 30, max_tokens: int = 256):
    """流式输出：async generator，逐块 yield 文本增量（Anthropic SSE content_block_delta）。
    只给 voicecall 通话用（需要边生成边合成语音）；httpx lazy import，不影响同步 caller。"""
    import httpx  # lazy：只有 voicecall（voice-bridge venv 有 httpx）会调，jiwen 等不受影响
    import config_loader as _cfg
    g = _cfg.load_global()
    delta_cfg = ((g.get("jiwen") or {}).get("delta_llm") or {})
    api_key = delta_cfg.get("api_key", "")
    if not api_key:
        raise RuntimeError("deepseek api_key 未配置（_global.yml.jiwen.delta_llm.api_key）")
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
    """强制 JSON 解析。失败抛异常（caller 自行 fallback）。"""
    text = call_text(prompt, timeout=timeout, max_tokens=max_tokens)
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(l for l in lines if not l.startswith("```"))
    start = text.index("{")
    end = text.rindex("}")
    return json.loads(text[start:end+1])
