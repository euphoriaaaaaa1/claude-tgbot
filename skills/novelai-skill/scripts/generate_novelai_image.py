#!/usr/bin/env python3
"""Generate an image with NovelAI from structured intermediate JSON."""

from __future__ import annotations

import argparse
import io
import json
import os
import random
import re
import sys
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any
from urllib import error, request

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from prompt_builder import build_prompts, load_json  # noqa: E402


def skill_root() -> Path:
    return SCRIPT_DIR.parent


def sanitize_agent_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    cleaned = cleaned.strip(".-")
    return cleaned or "default"


def sanitize_session_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    cleaned = cleaned.strip(".-")
    return cleaned or "default-session"


def resolve_agent_name(cli_value: str | None = None) -> str:
    if cli_value and cli_value.strip():
        return sanitize_agent_name(cli_value)
    env_value = os.getenv("NOVELAI_AGENT_NAME", "").strip()
    if env_value:
        return sanitize_agent_name(env_value)
    return "default"


def resolve_session_name(cli_value: str | None = None) -> str:
    if cli_value and cli_value.strip():
        return sanitize_session_name(cli_value)
    for env_name in (
        "NOVELAI_SESSION_NAME",
        "OPENCLAW_SESSION_ID",
        "CLAUDE_SESSION_ID",
        "CODEX_SESSION_ID",
        "SESSION_ID",
    ):
        env_value = os.getenv(env_name, "").strip()
        if env_value:
            return sanitize_session_name(env_value)
    return "default-session"


def default_output_dir(agent_name: str, session_name: str) -> Path:
    env_value = os.getenv("NOVELAI_OUTPUT_DIR", "").strip()
    if env_value:
        return Path(env_value).expanduser() / agent_name / session_name
    return Path(os.path.expanduser("~/resource/media")) / agent_name / session_name


def load_local_env() -> None:
    env_candidates = [skill_root() / ".env.local"]
    for env_path in env_candidates:
        if not env_path.exists():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if key and value and key not in os.environ:
                os.environ[key] = value


def read_token() -> str:
    for env_name in ("NOVELAI_JWT", "NOVELAI_BEARER_TOKEN", "NOVELAI_TOKEN"):
        value = os.getenv(env_name, "").strip()
        if value:
            return value
    raise RuntimeError(
        "缺少 NovelAI 认证信息。请先在 .env.local 里写入 NOVELAI_BEARER_TOKEN，或者导出 NOVELAI_JWT。"
    )


def build_browser_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/octet-stream",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
        ),
        "Origin": "https://novelai.net",
        "Referer": "https://novelai.net/",
    }


# style params 白名单（读取端第二道防线）。写入端在 moments/styles_routes.py 已经拦过一次，
# 但 styles.json 还会被手工编辑、被旧版本写过（存量的 cfg_scale: 0 就是这么来的），非法值直接
# 进 payload 只换来 NovelAI 的英文 400，与 style 页面毫无关联线索。所以这里未知键/类型错/越界
# 一律丢弃、退回全局默认，只留一行 stderr 当排查线索——生图本身照常，不中断。
_STYLE_TOP_KEYS = ("model", "steps", "cfg_scale", "sampler")  # 落在 config 顶层
_STYLE_SUB_KEYS = ("ucPreset",)                               # 落在 config["novelai_parameters"]
_STYLE_MODELS = ("nai-diffusion-5-full", "nai-diffusion-5-curated", "nai-diffusion-4-5-full")
_STYLE_SAMPLERS = ("k_euler_ancestral", "k_euler", "k_dpmpp_2s_ancestral",
                   "k_dpmpp_2m_sde", "k_dpmpp_sde", "ddim_v3")


def _reject_style_param(key: str, value: Any) -> str | None:
    """合法返回 None，非法返回一句中文原因。bool 不算数字：JSON 的 true 在 Python 里 == 1。"""
    is_int = isinstance(value, int) and not isinstance(value, bool)
    is_num = isinstance(value, (int, float)) and not isinstance(value, bool)
    if key == "model":
        return None if value in _STYLE_MODELS else "不在可用模型清单内"
    if key == "sampler":
        return None if value in _STYLE_SAMPLERS else "不在可用采样器清单内"
    if key == "steps":
        return None if is_int and 1 <= value <= 50 else "不是 1–50 的整数"
    if key == "cfg_scale":
        # 0 是非法值不是"未设置"：CFG=0 扩散几乎不看提示词，出图必废且不报错
        return None if is_num and 1 <= value <= 10 else "不是 1–10 的数字"
    if key == "ucPreset":
        return None if is_int and value in (0, 1, 2, 3) else "不是 0、1、2、3 之一"
    return "不是本脚本支持的参数"


def apply_style_params(config: dict[str, Any], params: Any) -> dict[str, Any]:
    """把预设 params 叠加到 config：留空 = 跟随全局默认，非法 = 丢弃并退回全局默认。"""
    if not isinstance(params, dict):
        return config
    for key, value in params.items():
        if value is None or value == "":
            continue  # 留空 = 跟随全局默认，不是错误
        reason = _reject_style_param(key, value)
        if reason:
            sys.stderr.write("[novelai] style 参数被忽略：%s=%r %s，已退回全局默认\n"
                             % (key, value, reason))
            continue
        if key in _STYLE_TOP_KEYS:
            config[key] = value
        else:
            sub = dict(config.get("novelai_parameters") or {})
            sub[key] = value
            config["novelai_parameters"] = sub
    return config


def apply_active_style(config: dict[str, Any], agent_name: str | None = None) -> dict[str, Any]:
    """把激活的画风预设叠加到 config 上（可靠性优先，不依赖 LLM）。

    - 选哪个预设：env NOVELAI_ACTIVE_STYLE_ID（styles 页"生成示例图"预览用）> 该 bot 的
      active_by_bot[agent_name]（每个 bot 各自画风）> 全局 active（兜底/兼容）
      没指定 bot 时不套任何预设，见下面的注释
    - positive_prefix：**整体替换** config 原有 positive_prefix（预设本身就是完整画风串，
      像 SillyTavern 那样选哪个用哪个；若叠加到默认画师串上，默认串会盖过预设画风）
      空 positive_prefix（如"default"预设）不替换，保留 default_config 原有画师串
    - negative_prefix：非空才整体覆盖，避免空值把原有负面提示词清空
    - params：按白名单流转（model/steps/cfg_scale/sampler 落 config 顶层，ucPreset 落
      novelai_parameters），非法值丢弃并退回全局默认，见 apply_style_params
    - active 找不到对应 style 时，原样返回 config，不报错
    """
    # styles.json 的位置可用 NOVELAI_SKILL_ROOT 覆盖（与 moments/styles_routes.py 同一个接缝）：
    # 验收测试要在临时副本上改绑定，共用生产文件跑一次就会把真实 bot 画风绑定改乱。
    # .env.local 仍固定读真实 skill 根目录——令牌不该跟着测试副本走。
    styles_root = Path(os.environ.get("NOVELAI_SKILL_ROOT") or skill_root())
    styles_path = styles_root / "assets" / "styles.json"
    if not styles_path.exists():
        return config
    try:
        styles_data = load_json(styles_path)
    except Exception:
        return config

    explicit = os.getenv("NOVELAI_ACTIVE_STYLE_ID", "").strip()
    if explicit:
        active_id = explicit
    elif agent_name and agent_name != "default":
        active_id = (styles_data.get("active_by_bot", {}).get(agent_name)
                     or styles_data.get("active", ""))
    else:
        # 没指定 bot（--agent-name 缺省时 resolve_agent_name 给的就是 "default"）：不套任何预设。
        # 这种调用是手工/调试跑，--config 给什么就该出什么；套上全局预设会让画风与参数都无法解释。
        active_id = ""
    style = next(
        (s for s in styles_data.get("styles", []) if s.get("id") == active_id), None
    )
    if style is None:
        return config

    positive_prefix = str(style.get("positive_prefix", "")).strip()
    if positive_prefix:
        config["positive_prefix"] = positive_prefix  # 整体替换，不叠加默认画师串

    negative_prefix = str(style.get("negative_prefix", "")).strip()
    if negative_prefix:
        config["negative_prefix"] = negative_prefix

    return apply_style_params(config, style.get("params") or {})


# 三个守卫（景别 / 角度 / 动感）都只在"当轮正文漏写"时补；其中景别守卫按用户拍板
# 默认强制全身，只有"明确局部词"才放行。非全身词（medium shot / upper body 等）不再
# 视为"用户已指定景别"，必须从最终提示词中剥离并强制注入全身。
#
# 判定域是 prompt_body_used（当轮正文），不是 final_positive_prompt：后者含 style 的
# positive_prefix，画师串里本来就常有 portrait / from above 这类词，拿它判定会让守卫
# 永远认为"已经有了"而一次都不注入，且毫无报错。代价是 prefix 写了 close-up 而正文没写
# 景别时，最终会同时出现两个景别词——接受：注入词在最前、权重更高，且看图就能发现，
# 比静默失效强。（动感守卫仍看最终串：任何动感词都算数，没有"必须是某个词"的要求。）
_FULL_BODY_WORDS = (
    "full body", "full-body", "fullbody", "head to toe", "full shot",
    "wide shot", "long shot",
)
_EXPLICIT_LOCAL_WORDS = (
    "close-up", "closeup", "close up", "extreme close-up", "extreme closeup",
    "extreme close up", "face focus", "headshot", "portrait",
    "特写", "脸部特写", "拍脸", "大头照",
)
_NON_FULL_BODY_WORDS = (
    "medium shot", "upper body", "upper-body", "upper_body", "cowboy shot",
    "waist up", "bust shot", "knee shot", "between_legs", "lower body",
    "lower_body", "low body", "中景", "近景", "半身", "上半身",
)
# 兼容旧名：仍表示"景别/镜头词"并集，供历史引用方使用。
_SHOT_WORDS = tuple(dict.fromkeys(
    _FULL_BODY_WORDS + _EXPLICIT_LOCAL_WORDS + _NON_FULL_BODY_WORDS + ("from far away",)
))


def _phrase_pattern(term: str) -> str:
    """把可能带空格/连字符/下划线的英文短语变成宽松匹配，覆盖 Upper-body / upper_body 等写法。"""
    parts = re.split(r"[\s_-]+", term.strip().lower())
    if len(parts) == 1:
        return re.escape(parts[0])
    return r"[\s_-]+".join(re.escape(part) for part in parts)


def _compile_word_pattern(words: tuple[str, ...]) -> re.Pattern[str]:
    # 长词优先，避免"上半身"被"半身"先吃掉；英文短语的连字符/下划线由 _phrase_pattern 统一覆盖。
    unique = tuple(dict.fromkeys(words))
    pattern = "|".join(_phrase_pattern(w) for w in sorted(unique, key=len, reverse=True))
    return re.compile(pattern, re.IGNORECASE)


_FULL_BODY_PATTERN = _compile_word_pattern(_FULL_BODY_WORDS)
_EXPLICIT_LOCAL_PATTERN = _compile_word_pattern(_EXPLICIT_LOCAL_WORDS)
_NON_FULL_BODY_PATTERN = _compile_word_pattern(_NON_FULL_BODY_WORDS)

# 默认景别锁死为全身（呆板出在平视/站定/居中，不出在全身，那三维交给角度与动感守卫）。
# detailed face：竖版全身叠极端广角容易糊脸，这是"全身"的真实代价，一并写死。
_DEFAULT_SHOT = "full body, head to toe visible, detailed face"


def _clean_prompt_text(value: str) -> str:
    """清理剥离非全身词后可能留下的双逗号/多余空格，不把普通空白改成逗号。"""
    value = value.replace("，", ",")
    value = re.sub(r"(?:\s*,\s*)+", ", ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" ,")


def _find_body_segment(final: str, body: str) -> str | None:
    """在 final_positive_prompt 里定位"当轮正文"片段。

    build_prompts 会把正文中的中文逗号规范化为英文逗号，因此不能只用 prompt_body_used
    原文做子串查找；这里同时准备一个已按同样规则轻量规范化的候选。
    """
    if not body:
        return None
    candidates = [body]
    normalized_body = re.sub(r"\s*，\s*", ", ", body)
    normalized_body = re.sub(r"\s+", " ", normalized_body)
    normalized_body = normalized_body.strip(" ,")
    if normalized_body and normalized_body != body:
        candidates.append(normalized_body)
    for candidate in candidates:
        if candidate and candidate in final:
            return candidate
    return None


def ensure_shot_size(prompts: dict[str, str]) -> dict[str, str]:
    body = prompts.get("prompt_body_used", "") or ""
    final = prompts.get("final_positive_prompt", "") or ""

    # 1. 正文命中明确局部词：尊重原样，不注入全身、不剥离。
    if _EXPLICIT_LOCAL_PATTERN.search(body):
        return prompts

    # 2. 正文命中全身词：已经达标，不重复堆叠。
    if _FULL_BODY_PATTERN.search(body):
        return prompts

    # 3. 其他情况（无景别词，或只出现非全身词）：从最终提示词中剥离"正文里出现"的
    #    非全身词，再在最前面注入默认全身。prompt_body_used 保留原文供排查。
    body_segment = _find_body_segment(final, body)
    if body_segment is not None:
        cleaned_body = _NON_FULL_BODY_PATTERN.sub("", body_segment)
        cleaned_body = _clean_prompt_text(cleaned_body)
        final = final.replace(body_segment, cleaned_body)
    else:
        # 极端兜底：定位不到正文片段时也只在最后手段整体清理，避免漏掉非全身词。
        final = _NON_FULL_BODY_PATTERN.sub("", final)
    final = _clean_prompt_text(final)

    # 若剥离后 final 已空（正文仅是非全身词），只保留默认全身即可，不出现前导逗号。
    injected = f"{_DEFAULT_SHOT}, {final}" if final else _DEFAULT_SHOT
    prompts["final_positive_prompt"] = injected
    return prompts


_CAMERA_WORDS = (
    "pov", "first-person", "first person", "high angle", "low angle",
    "from above", "from below", "from side", "over-the-shoulder", "over the shoulder",
    "dutch angle", "bird's-eye", "birds-eye", "worm's-eye", "eye-level", "eye level",
    "profile view", "top-down", "overhead", "from behind", "rear view", "back view",
)
_CAMERA_POOL = [
    "from above, high angle shot", "from below, low angle shot",
    "from side, profile view", "over-the-shoulder shot",
    # 不留 eye-level：全身 + 平视 = 用户抱怨的呆板立绘，中性基线在这里是稳定产出最差结果
    "dutch angle, tilted frame", "worm's-eye view, extreme low angle looking up",
    "bird's-eye view, top-down",
]


def ensure_camera_angle(prompts: dict[str, str]) -> dict[str, str]:
    fp = prompts.get("final_positive_prompt", "")
    if any(w in prompts.get("prompt_body_used", "").lower() for w in _CAMERA_WORDS):
        return prompts  # 当轮正文已指定镜头（含 POV），尊重不动
    angle = random.SystemRandom().choice(_CAMERA_POOL)
    prompts["final_positive_prompt"] = f"{angle}, {fp}"
    return prompts


# 动态感：避免"人杵在那里"的僵硬静态图，给每张注入动感词（漏写才补，已带则尊重）。
_DYNAMIC_WORDS = (
    "dynamic", "motion", "movement", "action", "mid-",
    "bouncing", "swaying", "flowing", "windswept", "jiggle",
)
_DYNAMIC_POOL = [
    "dynamic pose, sense of motion",
    "dynamic angle, motion blur",
    "dynamic composition, motion lines",
    "mid-motion, hair and clothes in motion",
]


def ensure_dynamic_feel(prompts: dict[str, str]) -> dict[str, str]:
    fp = prompts.get("final_positive_prompt", "")
    if any(w in fp.lower() for w in _DYNAMIC_WORDS):
        return prompts  # worker 已带动感词，尊重不动
    tag = random.SystemRandom().choice(_DYNAMIC_POOL)
    prompts["final_positive_prompt"] = f"{tag}, {fp}"
    return prompts


def resolve_seed(config: dict[str, Any]) -> int:
    seed = int(config.get("seed", -1))
    if seed < 0:
        return random.SystemRandom().randint(1, 2**31 - 1)
    return seed


def build_payload(config: dict[str, Any], prompts: dict[str, str]) -> dict[str, Any]:
    raw_params = dict(config.get("novelai_parameters", {}))
    width = int(config["width"])
    height = int(config["height"])
    steps = int(config["steps"])
    scale = float(config["cfg_scale"])
    sampler = config.get("sampler", "k_euler_ancestral")
    seed = resolve_seed(config)

    params = {
        "params_version": 3,
        "width": width,
        "height": height,
        "scale": scale,
        "sampler": sampler,
        "steps": steps,
        "n_samples": 1,
        "ucPreset": int(raw_params.get("ucPreset", 0)),
        "qualityToggle": bool(raw_params.get("quality_toggle", True)),
        "sm": bool(raw_params.get("sm", False)),
        "sm_dyn": bool(raw_params.get("sm_dyn", False)),
        "autoSmea": bool(raw_params.get("autoSmea", False)),
        "dynamic_thresholding": bool(raw_params.get("dynamic_thresholding", False)),
        "controlnet_strength": float(raw_params.get("controlnet_strength", 1)),
        "legacy": bool(raw_params.get("legacy", False)),
        "add_original_image": bool(raw_params.get("add_original_image", False)),
        "cfg_rescale": float(raw_params.get("cfg_rescale", 0.0)),
        "noise_schedule": raw_params.get("noise_schedule", "karras"),
        "legacy_v3_extend": bool(raw_params.get("legacy_v3_extend", False)),
        "use_coords": bool(raw_params.get("use_coords", False)),
        "legacy_uc": False,
        "normalize_reference_strength_multiple": bool(
            raw_params.get("normalize_reference_strength_multiple", False)
        ),
        "seed": seed,
        "negative_prompt": prompts["final_negative_prompt"],
        "characterPrompts": raw_params.get("characterPrompts", []),
        "strength": float(raw_params.get("strength", 0.7)),
        "v4_prompt": {
            "caption": {
                "base_caption": prompts["final_positive_prompt"],
                "char_captions": [],
            },
            "use_coords": False,
            "use_order": True,
        },
        "v4_negative_prompt": {
            "caption": {
                "base_caption": prompts["final_negative_prompt"],
                "char_captions": [],
            },
            "legacy_uc": False,
        },
        "deliberate_euler_ancestral_bug": bool(
            raw_params.get("deliberate_euler_ancestral_bug", False)
        ),
        "prefer_brownian": bool(raw_params.get("prefer_brownian", True)),
    }
    if raw_params.get("skip_cfg_above_sigma") is not None:
        params["skip_cfg_above_sigma"] = raw_params["skip_cfg_above_sigma"]

    return {
        "input": prompts["final_positive_prompt"],
        "model": config["model"],
        "action": "generate",
        "use_new_shared_trial": True,
        "parameters": params,
    }


def build_fallback_payload(
    config: dict[str, Any], prompts: dict[str, str]
) -> dict[str, Any]:
    seed = resolve_seed(config)
    params = dict(config.get("novelai_parameters", {}))
    params.update(
        {
            "width": int(config["width"]),
            "height": int(config["height"]),
            "steps": int(config["steps"]),
            "scale": float(config["cfg_scale"]),
            "sampler": config.get("sampler", "k_euler_ancestral"),
            "seed": seed,
            "negative_prompt": prompts["final_negative_prompt"],
        }
    )
    return {
        "input": prompts["final_positive_prompt"],
        "model": config["model"],
        "use_new_shared_trial": True,
        "parameters": params,
    }


def save_binary_image(
    content: bytes, output_dir: Path, output_image_path: Path | None = None
) -> Path:
    if output_image_path is not None:
        output_image_path.parent.mkdir(parents=True, exist_ok=True)
        output_image_path.write_bytes(content)
        return output_image_path
    image_path = output_dir / f"novelai_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
    image_path.write_bytes(content)
    return image_path


def extract_image_from_zip(
    content: bytes,
    output_dir: Path,
    output_image_path: Path | None = None,
) -> Path:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        image_members = [
            member
            for member in archive.namelist()
            if member.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
        ]
        if not image_members:
            raise RuntimeError("结果包里没有图片文件。")
        target = image_members[0]
        if output_image_path is not None:
            image_path = output_image_path
        else:
            suffix = Path(target).suffix or ".png"
            image_path = (
                output_dir
                / f"novelai_{int(time.time())}_{uuid.uuid4().hex[:8]}{suffix}"
            )
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(archive.read(target))
        return image_path


def save_response(
    content: bytes, output_dir: Path, output_image_path: Path | None = None
) -> Path:
    try:
        return extract_image_from_zip(
            content, output_dir, output_image_path=output_image_path
        )
    except zipfile.BadZipFile:
        return save_binary_image(
            content, output_dir, output_image_path=output_image_path
        )


def request_image(
    endpoint: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> tuple[int, bytes]:
    request_body = json.dumps(payload).encode("utf-8")
    req = request.Request(endpoint, data=request_body, headers=headers, method="POST")
    with request.urlopen(req, timeout=120) as response:
        return response.getcode(), response.read()



def format_http_error(code: int, detail: str) -> str:
    if code == 400:
        return f"NovelAI 没认出请求内容：HTTP 400 {detail}"
    if code == 401:
        return "NovelAI 认证失败，请检查令牌有没有过期。"
    if code == 403:
        return f"NovelAI 拒绝了这次请求：HTTP 403 {detail}"
    return f"NovelAI 请求失败：HTTP {code} {detail}"


def describe_http_error(exc: error.HTTPError) -> str:
    detail = exc.read().decode("utf-8", errors="ignore").strip() or str(exc.reason)
    return format_http_error(exc.code, detail)


_RETRY_DELAYS = (3, 5, 8)


def _is_retryable(code: int, detail: str) -> bool:
    """值得重试的只有两类：5xx 是 NovelAI 侧偶发故障（V5 Full 实测约一半概率 500），
    429 + 并发锁是排队信号。其余 4xx 是请求本身不合法，重试只是等量重复同一个错误。"""
    return code >= 500 or (code == 429 and "Concurrent generation is locked" in detail)


def request_with_retry(
    endpoint: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    delays: tuple[int, ...] = _RETRY_DELAYS,
) -> tuple[bytes | None, str | None, bool]:
    """发一次请求，可重试的错误按 delays 退避重发。

    返回 (响应体, 错误说明, 是否属于可重试类错误)；响应体非 None 即成功。
    """
    last_error: str | None = None
    retryable = False
    for attempt in range(len(delays) + 1):
        try:
            status_code, body = request_image(endpoint, headers, payload)
            if status_code < 400 and body:
                return body, None, False
            # 走到这里是 2xx 空响应（服务端异常），或 urlopen 没抛异常的 4xx/5xx
            last_error = f"NovelAI 请求失败：HTTP {status_code}"
            retryable = status_code >= 500 or status_code < 400
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore").strip() or str(exc.reason)
            last_error = format_http_error(exc.code, detail)
            retryable = _is_retryable(exc.code, detail)
        except error.URLError as exc:
            # 网络没通：换个请求体也一样不通，不值得再退避空转
            return None, f"NovelAI 请求失败，网络没通：{exc.reason}", False
        if not retryable or attempt >= len(delays):
            break
        time.sleep(delays[attempt])
    return None, last_error, retryable

def load_previous_state(output_dir: Path) -> dict[str, Any] | None:
    last_request_path = output_dir / "last_request.json"
    if not last_request_path.exists():
        return None
    return load_json(last_request_path)


def save_generation_state(
    state_dir: Path,
    result: dict[str, Any],
    prompts: dict[str, Any],
    source_request: dict[str, Any] | str,
) -> dict[str, Any]:
    history_dir = state_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    record = {
        "image_path": result["image_path"],
        "reply_text": result["reply_text"],
        "positive_prefix_used": prompts["positive_prefix_used"],
        "prompt_body_used": prompts["prompt_body_used"],
        "final_positive_prompt": prompts["final_positive_prompt"],
        "negative_prefix_used": prompts["negative_prefix_used"],
        "final_negative_prompt": prompts["final_negative_prompt"],
        "request_payload": result["request_payload"],
        "normalized_intermediate": prompts.get("normalized_intermediate", {}),
        "source_request": source_request,
        "mode": result.get("mode", prompts.get("mode", "new")),
        "source_request_path": result.get("source_request_path", ""),
        "generated_at": timestamp,
    }
    history_path = history_dir / f"{timestamp}_{uuid.uuid4().hex[:8]}.json"
    history_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (state_dir / "last_request.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return record


def generate_image(
    config: dict[str, Any],
    intermediate: dict[str, Any] | str,
    output_dir: Path,
    source_request_path: str = "",
    output_image_path: Path | None = None,
    state_dir: Path | None = None,
    agent_name: str | None = None,
) -> dict[str, Any]:
    actual_state_dir = state_dir or output_dir
    previous_state = load_previous_state(actual_state_dir)
    config = apply_active_style(config, agent_name)
    prompts = build_prompts(config, intermediate, previous_state=previous_state)
    prompts = ensure_shot_size(prompts)     # 强制每张有景别，默认全身（漏写才补，尊重特写）
    prompts = ensure_camera_angle(prompts)  # 强制每张有具体镜头角度（漏写才补，尊重 POV）
    prompts = ensure_dynamic_feel(prompts)  # 强制每张有动态感（漏写才补，治僵硬静态图）
    token = read_token()
    endpoint = os.getenv(
        "NOVELAI_IMAGE_ENDPOINT", "https://image.novelai.net/ai/generate-image"
    )
    headers = build_browser_headers(token)
    payload = build_payload(config, prompts)
    response_body, last_error, retryable = request_with_retry(endpoint, headers, payload)
    if response_body is None and retryable:
        # 主请求体是 5xx / 并发锁挂的，换精简请求体再试一次（只一次，别把偶发 500 放大成限流）。
        # 4xx 不走这里：请求本身不合法，再发一次只会等量重复同一个错误。
        payload = build_fallback_payload(config, prompts)
        response_body, fallback_error, _ = request_with_retry(
            endpoint, headers, payload, delays=()
        )
        last_error = fallback_error or last_error
    if response_body is None:
        raise RuntimeError(last_error or "NovelAI 请求失败。")

    output_dir.mkdir(parents=True, exist_ok=True)
    actual_state_dir.mkdir(parents=True, exist_ok=True)
    image_path = save_response(
        response_body, output_dir, output_image_path=output_image_path
    )
    source_request = intermediate
    state = save_generation_state(
        actual_state_dir,
        {
            "image_path": str(image_path),
            "reply_text": prompts.get("reply_text", ""),
            "request_payload": payload,
            "mode": prompts.get("mode", "new"),
            "source_request_path": source_request_path,
        },
        prompts,
        source_request,
    )
    return {
        "image_path": str(image_path),
        "image_markdown": f"![generated-image]({image_path})",
        "reply_text": prompts.get("reply_text", ""),
        "positive_prefix_used": prompts["positive_prefix_used"],
        "prompt_body_used": prompts["prompt_body_used"],
        "final_positive_prompt": prompts["final_positive_prompt"],
        "negative_prefix_used": prompts["negative_prefix_used"],
        "final_negative_prompt": prompts["final_negative_prompt"],
        "request_payload": payload,
        "normalized_intermediate": prompts.get("normalized_intermediate", {}),
        "mode": prompts.get("mode", "new"),
        "source_request_path": source_request_path,
        "last_request_path": str(actual_state_dir / "last_request.json"),
        "history_record_path": str(
            max(
                (actual_state_dir / "history").glob("*.json"),
                key=lambda p: p.stat().st_mtime,
            )
        ),
        "saved_state": state,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate an image from intermediate JSON."
    )
    parser.add_argument("--intermediate", required=True, help="Path to structured JSON")
    parser.add_argument("--config", required=True, help="Path to config JSON")
    parser.add_argument(
        "--agent-name",
        help="Agent name bucket for output storage. Default: NOVELAI_AGENT_NAME or default",
    )
    parser.add_argument(
        "--session-name",
        help=(
            "Session name bucket for output storage. Default: NOVELAI_SESSION_NAME, "
            "platform session id vars, or default-session"
        ),
    )
    parser.add_argument(
        "--output-dir",
        help=(
            "Base directory for saved images. Final path appends agent and session. "
            "Default: ~/resource/media/<agent>/<session>"
        ),
    )
    parser.add_argument(
        "--output-image-path",
        help="Exact image path to write the final generated image to",
    )
    parser.add_argument(
        "--state-dir", help="Directory for last_request/history metadata"
    )
    parser.add_argument("--output-json", help="Optional path to save result JSON")
    # ─── 尺寸选择 ─────────────────────────────────────────────────────────
    # AI 根据场景从预设选一个，覆盖 default_config.json 的 1024×1024。
    # 显式 --width/--height 优先级最高（极少用，留给"宽高 XYZ"这种明指令）。
    parser.add_argument(
        "--ratio",
        choices=["square", "portrait", "landscape", "wide"],
        help=(
            "Aspect preset. portrait=832x1216 (自拍/全身/半身/竖屏); "
            "landscape=1216x832 (远景/录像/横屏); square=1024x1024 (默认/特写); "
            "wide=1536x640 (极宽景)"
        ),
    )
    parser.add_argument("--width", type=int, help="Override width (must be multiple of 64)")
    parser.add_argument("--height", type=int, help="Override height (must be multiple of 64)")
    # ─── 场景一致性：复用上一次 seed ─────────────────────────────────────
    # NovelAI 同 seed + 类似 prompt → 房间/床/灯光大概率延续。
    # 默认每次随机 seed 是"换场景"才该用的；同场景续图必须传这个。
    parser.add_argument(
        "--reuse-seed",
        action="store_true",
        help="Reuse seed from last_request.json (for same-scene continuity)",
    )
    args = parser.parse_args()

    load_local_env()
    intermediate_path = Path(args.intermediate)
    # 防御：父目录可能不存在（worker 用 Bash 重定向写 intermediate 时不会自动建目录），
    # 先建好，让上游下一次写入不再 ENOENT；本次仍缺失则给一行清晰报错而非红栈。
    intermediate_path.parent.mkdir(parents=True, exist_ok=True)
    if intermediate_path.suffix.lower() == ".json" and not intermediate_path.exists():
        sys.stderr.write(
            f"[novelai] intermediate 不存在: {intermediate_path}\n"
            f"[novelai] 上游未成功写入（常见：worker LLM 调用 connection refused / 被打断）。"
            f"已建好父目录，重试本轮生图即可。\n")
        sys.exit(2)
    if intermediate_path.suffix.lower() == ".json":
        intermediate: dict[str, Any] | str = load_json(intermediate_path)
    else:
        intermediate = intermediate_path.read_text(encoding="utf-8")
    config = load_json(Path(args.config))
    # ─── 应用尺寸覆盖 ─────────────────────────────────────────────────────
    # 优先级：显式 --width/--height > --ratio 预设 > config.json 默认
    RATIO_PRESETS = {
        "square":    (1024, 1024),  # 默认 / 头像 / 特写
        "portrait":  (832, 1216),   # 自拍 / 全身 / 半身 / 竖屏
        "landscape": (1216, 832),   # 远景 / 录像 / 横屏
        "wide":      (1536, 640),   # 极宽景
    }
    if args.ratio:
        config["width"], config["height"] = RATIO_PRESETS[args.ratio]
    if args.width is not None:
        config["width"] = int(args.width)
    if args.height is not None:
        config["height"] = int(args.height)
    # NovelAI 要求宽高都是正整数且是 64 的倍数；catch 早 fail 早。
    # 下限判断是真需要的：-64 和 0 都能过 %64，透传下去只会换来 NovelAI 的英文 400。
    try:
        width, height = int(config["width"]), int(config["height"])
    except (KeyError, TypeError, ValueError):
        width = height = -1  # 配置里根本没有/不是数字，一并按非法尺寸报
    if width <= 0 or height <= 0 or width % 64 or height % 64:
        raise SystemExit(
            "width/height must be multiples of 64 (positive); got "
            f"{config.get('width')}x{config.get('height')}"
        )
    # ─── 应用 --reuse-seed：从 last_request 拉上一次的 seed ──────────────
    # last_request.json 结构：{ "request_payload": { "parameters": { "seed": N, ... } } }
    # 读不到（首次生成 / 文件损坏）→ 静默 fallback 到 config 默认（-1 = 随机）
    if args.reuse_seed:
        try:
            agent_name_resolved = resolve_agent_name(args.agent_name)
            session_name_resolved = resolve_session_name(args.session_name)
            if args.state_dir:
                state_dir_for_seed = Path(args.state_dir).expanduser()
            elif args.output_dir:
                state_dir_for_seed = Path(args.output_dir).expanduser() / agent_name_resolved / session_name_resolved
            else:
                state_dir_for_seed = default_output_dir(agent_name_resolved, session_name_resolved)
            last_request_path = state_dir_for_seed / "last_request.json"
            if last_request_path.exists():
                last = load_json(last_request_path)
                prev_seed = (
                    last.get("request_payload", {})
                    .get("parameters", {})
                    .get("seed")
                )
                if isinstance(prev_seed, int) and prev_seed > 0:
                    config["seed"] = prev_seed
        except Exception:
            # seed 复用失败不影响生图；fallback 到默认随机
            pass
    agent_name = resolve_agent_name(args.agent_name)
    session_name = resolve_session_name(args.session_name)
    if args.output_dir:
        output_dir = Path(args.output_dir).expanduser() / agent_name / session_name
    else:
        output_dir = default_output_dir(agent_name, session_name)
    try:
        result = generate_image(
            config,
            intermediate,
            output_dir,
            source_request_path=str(intermediate_path),
            output_image_path=Path(args.output_image_path).expanduser()
            if args.output_image_path
            else None,
            state_dir=Path(args.state_dir).expanduser() if args.state_dir else None,
            agent_name=agent_name,
        )
    except (ValueError, RuntimeError) as exc:
        # 可预期的失败（提示词为空、缺令牌、NovelAI 返回错误）给一行中文说明就够了。
        # 红栈对上游 worker 只是噪声，还会把请求头/令牌一类内容带进日志。
        sys.stderr.write(f"[novelai] 生图失败：{exc}\n")
        sys.exit(1)

    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output_json:
        Path(args.output_json).write_text(serialized + "\n", encoding="utf-8")
    else:
        print(serialized)


if __name__ == "__main__":
    main()
