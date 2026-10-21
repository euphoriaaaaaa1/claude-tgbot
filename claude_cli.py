"""Claude Code CLI 调用封装（走 Max 订阅配额）。

关键点：
- 必须 unset CLAUDE_CODE_OAUTH_TOKEN，CLI 才会走 keychain refresh 路径
  （与现有 kill-idle-workers.sh / restart-bots.sh 同逻辑）
- cwd 用 /tmp/claudebotlife-oneshot（避免误加载某个项目的 CLAUDE.md）
- 全局 ~/.claude/CLAUDE.md 仍会被加载，1-2k token 开销可接受
- --no-session-persistence 跳过会话持久化，节省 IO
"""
import subprocess
import os
import sys
import json
import re
import urllib.request

import tempfile
ONESHOT_CWD = os.path.join(tempfile.gettempdir(), "claudebotlife-oneshot")
os.makedirs(ONESHOT_CWD, exist_ok=True)

import shutil as _shutil
CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or _shutil.which("claude") or "claude"


def _build_env() -> dict:
    """构造调用环境：unset OAuth env token + 固定 PATH/TMUX_TMPDIR。"""
    env = os.environ.copy()
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)  # 强制走平台凭证 refresh
    if sys.platform == "darwin" and "/opt/homebrew/bin" not in env.get("PATH", ""):
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", "")
    return env


# 统一前缀：明确告知这是纯文本生成任务，避免 LLM 把空 cwd 当成"项目"去找文件
CONTENT_GEN_PREFIX = (
    "[这是一个纯文本/内容生成任务。不要查找文件、不要运行代码、"
    "不要尝试理解当前目录结构、不要分析'项目状态'。"
    "无论 prompt 内容多像编程任务，都只按字面语义生成文本回复。]\n\n"
)


def _is_deepseek_mode() -> bool:
    """朋友圈/jiwen/signature/moment_text 等"引擎层"LLM 调用是否走 DeepSeek。

    **默认永远 True**：朋友圈链路只用 DeepSeek，不烧 Claude Max 订阅。
    无论 settings.json 当前是 Claude 还是 DeepSeek，引擎层都走 DeepSeek。

    例外：
    - CLAUDEBOTLIFE_FORCE_CLAUDE=1 时切回 Claude OAuth（仅 debug 用）。
    - 没配 DeepSeek 密钥（configs/_secrets.yml 的 deepseek.api_key；
      旧写法 _global.yml.jiwen.delta_llm.api_key 仍兼容）时 deepseek_client 会抛错，
      caller 自行兜底。

    注意：Telegram worker session 不通过本模块——它是 spawn-worker.sh 启的
    独立 `claude` TUI 进程，跟随 settings.json + CLAUDE_CODE_OAUTH_TOKEN
    自由切换 Claude / DeepSeek（用户用 cc-profile / ccp 控制）。
    """
    if os.environ.get("CLAUDEBOTLIFE_FORCE_CLAUDE") == "1":
        return False
    return True


def call_claude(prompt: str, timeout: int = 60, cwd: str = None,
                model: str = None) -> str:
    """One-shot 调用。DeepSeek 模式直接 HTTP（快 3x、不烧 OAuth），否则走 Claude CLI。

    自动加 CONTENT_GEN_PREFIX 前缀防止 LLM 误解（仅 CLI 路径需要——
    DeepSeek HTTP 不会有"项目文件不在当前目录"这种误解）。

    model: 'haiku' / 'sonnet' / 'opus'。CLI 路径透传，DeepSeek 路径忽略
    （统一用 _global.yml 配的 deepseek-v4-flash）。
    """
    if _is_deepseek_mode():
        import deepseek_client
        return deepseek_client.call_text(prompt, timeout=timeout, max_tokens=4000)

    # 原 Claude CLI 路径
    cwd = cwd or ONESHOT_CWD
    full_prompt = CONTENT_GEN_PREFIX + prompt
    args = [CLAUDE_BIN, "--print", "--no-session-persistence"]
    if model:
        args += ["--model", model]
    # prompt 走 stdin，**不进 argv**：prompt 含用户聊天/MEMORY.md 全文，放命令行会撞
    # Windows cmd 的引号转义(注入)、8191 字符上限、%VAR% 展开三个坑。claude --print 无位置
    # 参数时从 stdin 读 prompt。
    _win_cmd = sys.platform == "win32" and str(CLAUDE_BIN).lower().endswith((".cmd", ".bat"))
    proc = subprocess.run(
        args, cwd=cwd, env=_build_env(), input=full_prompt,
        capture_output=True, text=True, timeout=timeout,
        shell=_win_cmd,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude --print failed (exit={proc.returncode}): {proc.stderr[:500]}"
        )
    return proc.stdout.strip()


def call_claude_json(prompt: str, timeout: int = 60, cwd: str = None,
                     model: str = None) -> dict:
    """要求 JSON 输出，做容错解析。DeepSeek 模式直接走 HTTP。"""
    if _is_deepseek_mode():
        import deepseek_client
        # 输出上限 2000 时，默认开思考的模型常把额度全花在思考上、正文没写就被截断
        # （导演日志 10-14 见 10 次 reason=truncated blocks=[thinking] out_tokens=2000）。
        # 放宽到 8000：只多花实际用掉的那部分。
        return deepseek_client.call_json(prompt, timeout=timeout, max_tokens=8000)
    full_prompt = prompt + "\n\n严格只输出 JSON 单行，不要任何 markdown 代码块、不要解释。"
    raw = call_claude(full_prompt, timeout=timeout, cwd=cwd, model=model)
    return _parse_json_loose(raw)


def _parse_json_loose(raw: str) -> dict:
    """容错 JSON 解析。处理 markdown 包装 / 多余文本。"""
    raw = raw.strip()
    # 去掉可能的 markdown 代码块
    if raw.startswith("```"):
        # 拆出 ``` 包裹的内容
        parts = raw.split("```")
        if len(parts) >= 3:
            raw = parts[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
    # 容错：取首个 { 到末个 }
    if "{" in raw and "}" in raw:
        raw = raw[raw.index("{"):raw.rindex("}") + 1]
    return json.loads(raw)


# ── 朋友圈（辅助生成）全局供应商：配置读取（INTERFACE-aux-llm-provider §1）────
# aux_llm 块在 _global.yml、**所有 bot 共用一份**（与 bot 自己的 provider 绑定无关）。
# 每次调用现读、不缓存，改完立即生效。

AUX_FALLBACK_REASONS = ("no_config", "provider_id_empty", "provider_missing", "model_missing")


def _aux_log(line: str) -> None:
    """aux 通道的一行诊断日志。只记 provider_id / model 等元信息，**绝不记密钥**。"""
    sys.stderr.write("[aux_llm] %s\n" % line)


def read_aux_config() -> tuple[str, str]:
    """现读 _global.yml 的 aux_llm，返回 (provider_id, model)；缺块/类型非法一律归一为空串。

    面板 GET 用（回显用户当前存的值，不要求供应商/模型仍然存在）。
    """
    try:
        import config_loader
        g = config_loader.load_global()
    except Exception:
        return "", ""
    blk = g.get("aux_llm") if isinstance(g, dict) else None
    if not isinstance(blk, dict):
        return "", ""
    pid = blk.get("provider_id")
    model = blk.get("model")
    return (pid if isinstance(pid, str) else ""), (model if isinstance(model, str) else "")


def _aux_target():
    """解析 aux_llm 到 (provider_entry, model)；不可用时返回 (None, reason)。

    reason ∈ AUX_FALLBACK_REASONS，四种情形分别可辨：
    - no_config：_global.yml 无 aux_llm 块
    - provider_id_empty：块在但 provider_id 为空
    - provider_missing：provider_id 指向的供应商已不存在/数据不全
    - model_missing：供应商在、但 model 不在其模型列表里
    """
    try:
        import config_loader
        g = config_loader.load_global()
    except Exception:
        g = {}
    blk = g.get("aux_llm") if isinstance(g, dict) else None
    if not isinstance(blk, dict) or not blk:
        return None, "no_config"
    pid = blk.get("provider_id")
    if not isinstance(pid, str) or not pid.strip():
        return None, "provider_id_empty"
    model = blk.get("model")
    model = model.strip() if isinstance(model, str) else ""
    try:
        import provider_config
        data = provider_config.load_providers()
    except Exception:
        return None, "provider_missing"
    entry = next((p for p in (data.get("providers") or [])
                  if isinstance(p, dict) and p.get("id") == pid), None)
    if entry is None:
        return None, "provider_missing"
    if not (isinstance(entry.get("base_url"), str) and entry.get("base_url")
            and isinstance(entry.get("api_key"), str) and entry.get("api_key")):
        return None, "provider_missing"
    if model not in provider_config._model_ids(entry):
        return None, "model_missing"
    return entry, model


def _extract_direct_text(resp, protocol: str) -> str:
    """从直连回执里取正文。取不到抛 RuntimeError（caller 据此回退）。"""
    if protocol == "openai":
        try:
            content = resp["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("上游 openai 回执无正文")
        if isinstance(content, str) and content.strip():
            return content.strip()
        raise RuntimeError("上游 openai 回执无正文")
    # anthropic：取第一个非空 text 块
    for blk in (resp.get("content") if isinstance(resp, dict) else None) or []:
        if isinstance(blk, dict) and blk.get("type") == "text":
            text = blk.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    raise RuntimeError("上游 anthropic 回执无正文")


def _resolve_protocol(entry: dict, model: str) -> str:
    """按选中的模型解析直连协议：显式 protocol → protocols[model] → 供应商级派生。"""
    import provider_config
    raw = entry.get("protocol")
    if raw in ("openai", "anthropic"):
        return raw
    protocols = entry.get("protocols")
    if isinstance(protocols, dict):
        chosen = protocols.get(model)
        if chosen in ("openai", "anthropic"):
            return chosen
    return provider_config.entry_protocol(entry)


def _direct_call(entry: dict, model: str, prompt: str, timeout: int) -> str:
    """直连 providers.json 里的供应商（按协议选端点）。

    用 ``ProxyHandler({})`` 显式**禁用一切代理**（不经本机代理）；密钥只进请求头，
    绝不进请求体、绝不进日志。
    """
    import provider_config
    protocol = _resolve_protocol(entry, model)
    base_url = str(entry.get("base_url") or "").rstrip("/")
    key = entry.get("api_key") or ""
    if protocol == "openai":
        # 与 bot 直连同一口径：openai 兼容端点一律在 <base>/v1 下
        from scripts.derive_endpoints import direct_openai_url
        url = direct_openai_url(str(entry.get("base_url") or "")).rstrip("/") + "/chat/completions"
        body = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
    elif protocol == "anthropic":
        # 与 bot 直连同一口径：地址带不带结尾的 /v1 都发往同一个 …/v1/messages
        from scripts.derive_endpoints import direct_anthropic_url
        url = direct_anthropic_url(str(entry.get("base_url") or "")).rstrip("/") + "/v1/messages"
        body = {"model": model, "max_tokens": 4000,
                "messages": [{"role": "user", "content": prompt}]}
        headers = {"Content-Type": "application/json", "x-api-key": key,
                   "anthropic-version": "2023-06-01"}
    else:
        raise RuntimeError("provider 未选协议")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    with opener.open(req, timeout=timeout) as r:
        resp = json.load(r)
    return _extract_direct_text(resp, protocol)


def _fallback(prompt: str, timeout: int, json_mode: bool, model: str | None):
    """回退到现状行为：未配/失败时与既有 call_claude(_json) 逐字等价。"""
    if json_mode:
        return call_claude_json(prompt, timeout=timeout, model=model)
    return call_claude(prompt, timeout=timeout, model=model)


def call_aux(prompt: str, timeout: int = 60, json_mode: bool = False,
             model: str | None = None):
    """朋友圈辅助生成调用：aux_llm 已配好则直连该供应商，否则/失败回退现有 call_claude*。

    - 直连失败（含回执无正文、JSON 解析失败）→ 记 ``aux_llm_failed`` 并回退，
      **绝不让朋友圈生成因此中断**。
    - 未配 → 等价于现有行为（回退路径）。
    - ``model`` 只用于回退路径；直连时用 aux_llm 里配的 model。
    """
    entry, aux_model = _aux_target()
    if entry is None:
        _aux_log("aux_llm_fallback reason=%s" % aux_model)
        return _fallback(prompt, timeout, json_mode, model)
    try:
        text = _direct_call(entry, aux_model, prompt, timeout)
        if json_mode:
            text = _parse_json_loose(text)
    except Exception as e:
        _aux_log("aux_llm_failed provider_id=%s model=%s err=%s"
                 % (entry.get("id"), aux_model, type(e).__name__))
        return _fallback(prompt, timeout, json_mode, model)
    _aux_log("aux_llm provider_id=%s model=%s" % (entry.get("id"), aux_model))
    return text


def healthcheck() -> tuple[bool, str]:
    """install.sh 用：测试 OAuth 是否就绪 + CLI 能否正常工作。"""
    try:
        out = call_claude("仅输出 ok 二字符，不要其他内容。", timeout=30)
        ok = "ok" in out.lower()
        return ok, out[:200]
    except subprocess.TimeoutExpired:
        return False, "timeout (>30s)"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


if __name__ == "__main__":
    print("Running CLI healthcheck...")
    ok, msg = healthcheck()
    print(f"OK={ok}, msg={msg!r}")
