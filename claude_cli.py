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
    - settings.json 缺 deepseek 配置（_global.yml.jiwen.delta_llm.api_key）
      时 deepseek_client 会抛错，caller 自行兜底。

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
    args.append(full_prompt)
    # Windows 上 claude 是 .cmd/.bat shim，直接 subprocess 会 WinError 193 → 需 shell 执行
    _win_cmd = sys.platform == "win32" and str(CLAUDE_BIN).lower().endswith((".cmd", ".bat"))
    proc = subprocess.run(
        args, cwd=cwd, env=_build_env(),
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
        return deepseek_client.call_json(prompt, timeout=timeout, max_tokens=2000)
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
