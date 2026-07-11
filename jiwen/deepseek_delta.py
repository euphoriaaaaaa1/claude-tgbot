"""DeepSeek 第二层情绪 delta 计算 — Phase 2 核心。

**独立于主模型配置**：直接 HTTP 调 DeepSeek API（不读 settings.json，不依赖 cc-profile）。
无论用户当前主模型切到 sonnet 还是 deepseek，本模块都用固定的 DeepSeek API key + model。

输入：bot persona 简介 + 最近对话片段
输出：{connection, pride, valence, arousal} delta，每个 -0.3 到 +0.3
失败降级：返回零 delta（不破坏状态）

外部依赖：
  - urllib (stdlib)
  - json (stdlib)
"""
from __future__ import annotations
import json
import urllib.request
import urllib.error
import sys


# ─── 默认配置（从 _global.yml.jiwen.delta_llm 覆盖）────────────

DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic"
DEFAULT_API_KEY = ""  # 必须由调用方提供
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_TIMEOUT = 30
DEFAULT_PROXY = "http://127.0.0.1:7897"


# ─── Prompt 模板 ─────────────────────────────────────────────

DELTA_PROMPT = """你是一个情绪观察者，读对话片段判断 AI 角色的内在状态变化。

【角色简介】
{persona}{hints_section}

【最近对话片段】
{conversation}

请返回 5 维浮点 delta JSON（一行），每个字段范围 **-0.3 到 +0.3**：

{{
  "connection": <连接需求变化。说完想说的→负；被冷落/简短回应→正>,
  "pride":      <骄傲变化。被夸→负（拉下脸）；被质疑/冷落→正（更倔）>,
  "valence":    <角色**对用户**的情绪反应。用户让 ta 开心/被安慰/被逗→正；被用户骂/冷落/失望→负。**角色因自身琐事(工作累、汤糊了)的坏心情不算在内**>,
  "arousal":    <激活度。紧张/兴奋/激烈讨论→正；平静收尾→负>,
  "desire":     <**性张力**变化(只指情色/被撩起，不是普通兴奋)。被撩/调情/亲密升温→正；无性意味的普通聊天/吵架→0>
}}

【判断准则】
- 这是**变化量 delta**，不是当前值。delta=0 表示该维度无变化。
- 数值要谨慎，强情绪才用 ±0.3，平淡对话用 ±0.05~0.1。
- 不要一刀切：可以只动 1-2 个维度，其他保持 0。
- **desire 只在真有情色/暧昧张力时才非 0**；聊游戏/八卦再兴奋，desire 也是 0(那是 arousal)。
- 5 个字段都必须出现（即使为 0）。
- **immersion（沉浸度）由活动系统管理，不要返回该字段**——
  即使你觉得角色在做某事，也只输出上面 4 个维度。

只输出 JSON 单行，不要 markdown 不要解释。"""


# ─── HTTP 调用核心 ──────────────────────────────────────────

def _http_post(url: str, headers: dict, body: dict, proxy: str = None,
               timeout: int = DEFAULT_TIMEOUT) -> dict:
    """直接 urllib HTTP POST，可选代理。返回响应 JSON。"""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": proxy, "http": proxy})
        )
    else:
        opener = urllib.request.build_opener()
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _build_conversation_str(messages: list[dict]) -> str:
    """对话列表 → 多行字符串。messages = [{"role":"user"|"assistant", "content":"..."}]

    DeepSeek-V4 1M 上下文，无需激进截断。最近 30 条 + 单条 2000 字封顶（防止极端长 dump）。
    """
    lines = []
    for m in messages[-30:]:
        role = m.get("role", "?")
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if len(content) > 2000:
            content = content[:2000] + "..."
        prefix = "用户" if role == "user" else "AI角色"
        lines.append(f"{prefix}：{content}")
    return "\n".join(lines) if lines else "（无对话）"


# ─── 主接口 ─────────────────────────────────────────────────

def compute_delta(persona: str,
                  messages: list[dict],
                  api_key: str,
                  delta_hints: str = "",
                  base_url: str = DEFAULT_BASE_URL,
                  model: str = DEFAULT_MODEL,
                  proxy: str = DEFAULT_PROXY,
                  timeout: int = DEFAULT_TIMEOUT) -> dict:
    """计算情绪 delta。

    返回：{"connection","pride","valence","arousal","desire"} 各 clamp [-0.3,+0.3]；
    **API 调用/解析失败返回 None**（调用方据此不推进消息游标，下 tick 重试，避免静默丢对话）。
    无 api_key / 无消息返回零 delta（合法 no-op，不算失败）。
    """
    zero = {"connection": 0.0, "pride": 0.0, "valence": 0.0, "arousal": 0.0, "desire": 0.0}

    if not api_key:
        print("[jiwen.delta] api_key 为空，返回零 delta", file=sys.stderr)
        return zero

    if not messages:
        return zero

    # DeepSeek-V4 1M 上下文，放完整 persona（不截断）
    persona_full = (persona or "").strip() or "（未配置）"
    conv = _build_conversation_str(messages)
    hints_section = ""
    if delta_hints and delta_hints.strip():
        hints_section = f"\n\n【该角色的专属判断准则（优先于下方通用准则）】\n{delta_hints.strip()}"
    prompt = DELTA_PROMPT.format(persona=persona_full, conversation=conv, hints_section=hints_section)

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    body = {
        "model": model,
        "max_tokens": 2000,  # thinking 1000-1500 + text 50，留余量  # thinking 占 ~200，text 放 ~50，留余量
        "messages": [{"role": "user", "content": prompt}],
    }
    url = f"{base_url.rstrip('/')}/v1/messages"

    try:
        resp = _http_post(url, headers, body, proxy=proxy, timeout=timeout)
    except urllib.error.HTTPError as e:
        print(f"[jiwen.delta] HTTP {e.code}: {e.reason}", file=sys.stderr)
        return None  # API 失败 → 别推进游标，下 tick 重试
    except Exception as e:
        print(f"[jiwen.delta] 异常: {type(e).__name__}: {e}", file=sys.stderr)
        return None

    # 提取 text content
    try:
        content_arr = resp.get("content", [])
        text = ""
        for c in content_arr:
            if c.get("type") == "text":
                text = c.get("text", "")
                break
        if not text:
            print(f"[jiwen.delta] 响应无 text content: {resp}", file=sys.stderr)
            return None
    except Exception as e:
        print(f"[jiwen.delta] 解析响应错误: {e}", file=sys.stderr)
        return None

    # 提取 JSON 单行（容错：去 markdown / 多行）
    text = text.strip()
    if text.startswith("```"):
        # 去除 markdown 代码块
        lines = text.split("\n")
        text = "\n".join(l for l in lines if not l.startswith("```"))
    try:
        # 找第一个 { 到最后一个 }
        start = text.index("{")
        end = text.rindex("}")
        delta = json.loads(text[start:end+1])
    except (ValueError, json.JSONDecodeError) as e:
        print(f"[jiwen.delta] JSON 解析失败: {e}\n  text: {text[:200]}", file=sys.stderr)
        return None

    # clamp 到 [-0.3, +0.3] 并保证 5 字段都存在
    out = {}
    for k in ["connection", "pride", "valence", "arousal", "desire"]:
        v = delta.get(k, 0)
        try:
            v = float(v)
        except (TypeError, ValueError):
            v = 0.0
        out[k] = max(-0.3, min(0.3, v))
    return out


# ─── CLI 入口（手动测试）─────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--key", required=True, help="DeepSeek API key")
    p.add_argument("--persona", default="35岁小学语文老师，性格温和内敛")
    args = p.parse_args()

    sample = [
        {"role": "user", "content": "今天怎么样"},
        {"role": "assistant", "content": "还行，刚改完作业，有点累。"},
        {"role": "user", "content": "嗯。"},
    ]
    delta = compute_delta(args.persona, sample, api_key=args.key)
    print(json.dumps(delta, ensure_ascii=False, indent=2))
