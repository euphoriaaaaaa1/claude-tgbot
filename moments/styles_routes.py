"""NovelAI 画风预设管理（Flask Blueprint）。

管理 ~/.claude/skills/novelai-skill/assets/styles.json：
- 网页 /styles 展示所有预设卡片，可新建/编辑/删除/设为当前/生成示例图
- generate_novelai_image.py 通过 apply_active_style() 在生图前读取该文件生效
"""
import os
import sys
import json
import time
import threading
import subprocess
from pathlib import Path
from flask import Blueprint, render_template, request, jsonify

from moments import redact
from moments.redact import mask as _mask

styles_bp = Blueprint("styles", __name__)

# 保护 styles.json 的 read-modify-write：并发生成示例图时，各请求生图耗时长(30-60s)，
# 若各自拿启动快照全量覆写会互相覆盖。锁只包 json 更新(几ms)，生图在锁外并发跑。
_SAVE_LOCK = threading.Lock()
_SAMPLING_IDS = set()  # 正在生成示例图的 style_id，由 _SAVE_LOCK 保护

# 生图脚本的 stderr 可能带上 NovelAI 令牌（请求头回显、异常里的 curl 命令等），回给页面前先抹掉。
# 正则本体已迁到 moments/redact.py（[RA] M1：脱敏是核心能力，不该让核心反向依赖本模块）。
def _scrub_secrets(text: str) -> str:
    return redact.scrub_text(text)[-2000:]


# 路径可用 NOVELAI_SKILL_ROOT 覆盖：验收测试通过真实 HTTP 接口读写 styles.json，
# 若测试实例与生产共用同一份文件，跑一次测试就会改乱真实的 bot 画风绑定。
# 不设此变量时行为与原来完全一致。
SKILL_ROOT = Path(os.environ.get("NOVELAI_SKILL_ROOT")
                  or os.path.expanduser("~/.claude/skills/novelai-skill"))
STYLES_PATH = SKILL_ROOT / "assets" / "styles.json"
DEFAULT_CONFIG_PATH = SKILL_ROOT / "assets" / "default_config.json"
GENERATE_SCRIPT = SKILL_ROOT / "scripts" / "generate_novelai_image.py"
PYTHON_BIN = os.environ.get("NOVELAI_PYTHON") or sys.executable
SAMPLE_DIR = Path(os.path.expanduser("~/resource/media/__styles__"))
SAMPLE_PROMPT = ("1girl, year 2024, cover page, -1::monocrome, flat color, simple background, "
                 "text logo::, masterpiece, best quality, very aesthetic, absurdres, solo, "
                 "nurse, latex gloves, very long hair, red eyes, red hair, black pantyhose, "
                 "cardigan, large breasts, skinny, crossed legs, smile, photo background, "
                 "hospital, indoors, sunlight, lens flare")


# per-bot 画风的 bot 列表现在从 configs/*.yml 动态取（见 _enabled_bots），不再硬编码。

# ── 生图参数白名单 ──────────────────────────────────────────────
# 同一份清单同时供前端下拉渲染与后端校验：加新模型/采样器只改这里一行，
# 不会出现"下拉里有、后端不认"的错位。value 为空串 = 跟随 default_config.json。
NOVELAI_MODELS = (
    {"value": "", "label": "跟随全局默认（推荐）"},
    {"value": "nai-diffusion-5-full", "label": "V5 Full（最新，偶发 500，会自动重试）"},
    {"value": "nai-diffusion-5-curated", "label": "V5 Curated（稳定）"},
    {"value": "nai-diffusion-4-5-full", "label": "V4.5 Full（稳定）"},
)
NOVELAI_SAMPLERS = ("k_euler_ancestral", "k_euler", "k_dpmpp_2s_ancestral",
                    "k_dpmpp_2m_sde", "k_dpmpp_sde", "ddim_v3")
UC_PRESETS = (
    {"value": 0, "label": "0 · 不用负面预设"},
    {"value": 1, "label": "1 · 轻度（Light）"},
    {"value": 2, "label": "2 · 人体强化（Human Focus）"},
    {"value": 3, "label": "3 · 重度（Heavy）"},
)
_MODEL_VALUES = tuple(m["value"] for m in NOVELAI_MODELS if m["value"])
_UC_VALUES = tuple(p["value"] for p in UC_PRESETS)
ALLOWED_PARAM_KEYS = ("model", "steps", "cfg_scale", "sampler", "ucPreset")

_E_MODEL = "model 必须是以下之一: " + " / ".join(_MODEL_VALUES)
_E_STEPS = "steps 必须是 1–50 的整数"
_E_CFG = "cfg_scale 必须是 1–10 的数字"
_E_SAMPLER = "sampler 必须是以下之一: " + ", ".join(NOVELAI_SAMPLERS)
_E_UC = "ucPreset 必须是 0、1、2、3 之一"


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)  # JSON 的 true 在 Python 里 == 1


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _validate_params(params):
    """校验并规范化 style 的 params。返回 (规范化后的 dict, 错误文案)，二者只有一个非空。

    值为 null / 空串 = 未设置，直接剔除；剩下的逐个查白名单与区间。
    cfg_scale 的 0 是**非法值不是未设置**：`if params.get("cfg_scale")` 这种真值判断会把 0
    当假值吞掉，而 CFG=0 出图必废且 NovelAI 不报错，所以下限校验必须挡在写盘之前。
    """
    if params is None:
        return {}, None
    if not isinstance(params, dict):
        return None, "params 必须是 JSON 对象"
    for key in params:
        if key not in ALLOWED_PARAM_KEYS:
            return None, "params 不支持的字段: %s" % key

    clean = {k: v for k, v in params.items() if v is not None and v != ""}
    if "model" in clean and clean["model"] not in _MODEL_VALUES:
        return None, _E_MODEL
    if "steps" in clean and not (_is_int(clean["steps"]) and 1 <= clean["steps"] <= 50):
        return None, _E_STEPS
    if "cfg_scale" in clean and not (_is_num(clean["cfg_scale"]) and 1 <= clean["cfg_scale"] <= 10):
        return None, _E_CFG
    if "sampler" in clean and clean["sampler"] not in NOVELAI_SAMPLERS:
        return None, _E_SAMPLER
    if "ucPreset" in clean and not (_is_int(clean["ucPreset"]) and clean["ucPreset"] in _UC_VALUES):
        return None, _E_UC
    return clean, None


def _validate_text(body, field, limit):
    """前缀字段：缺省/null 视为空串；非字符串与超长都是 400（用户可控值直连生图 API）。"""
    value = body.get(field)
    if value is None:
        return "", None
    if not isinstance(value, str):
        return None, "%s 必须是字符串" % field
    if len(value) > limit:
        return None, "%s 长度不得超过 %d" % (field, limit)
    return value, None


def _new_style_id(data) -> str:
    """秒级时间戳做 id，同一秒内连建两条会撞号（后者直接覆盖前者），撞了就顺延后缀。"""
    taken = {s["id"] for s in data.get("styles", [])}
    base = "style_%d" % int(time.time())
    if base not in taken:
        return base
    n = 2
    while "%s_%d" % (base, n) in taken:
        n += 1
    return "%s_%d" % (base, n)


def _load_styles() -> dict:
    if not STYLES_PATH.exists():
        return {"active": "default", "active_by_bot": {}, "styles": []}
    with STYLES_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("active_by_bot", {})  # 老文件平滑迁移
    return data


def _save_styles(data: dict) -> None:
    STYLES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STYLES_PATH.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _enabled_bots() -> list:
    """从 configs/*.yml 动态拿 enabled bot 列表 [{id, name}]（不再硬编码 chenlulu）。"""
    try:
        import config_loader
        return [{"id": b["_bot_id"], "name": b.get("display_name") or b["_bot_id"]}
                for b in config_loader.list_enabled_bots()]
    except Exception:
        return []


@styles_bp.route("/styles")
def styles_page():
    data = _load_styles()
    return render_template("styles.html", styles=data.get("styles", []),
                            active=data.get("active", ""),
                            active_by_bot=data.get("active_by_bot", {}),
                            bots=_enabled_bots())


@styles_bp.route("/api/styles", methods=["GET"])
def api_get_styles():
    data = _load_styles()
    # 下拉选项跟着数据一起下发，前端不用再维护第二份清单
    data["models"] = list(NOVELAI_MODELS)
    data["samplers"] = list(NOVELAI_SAMPLERS)
    data["uc_presets"] = list(UC_PRESETS)
    return jsonify(data)


@styles_bp.route("/api/styles", methods=["POST"])
def api_upsert_style():
    # 不用 force=True：Content-Type 不是 json 时应当明确报错，而不是猜着解析
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "请求体必须是 JSON 对象"}), 400
    name = (body.get("name") or "").strip() if isinstance(body.get("name"), str) else ""
    if not name:
        return jsonify({"error": "name required"}), 400
    if len(name) > 200:
        return jsonify({"error": "name 长度不得超过 200"}), 400
    positive, err = _validate_text(body, "positive_prefix", 2000)
    if err:
        return jsonify({"error": err}), 400
    negative, err = _validate_text(body, "negative_prefix", 2000)
    if err:
        return jsonify({"error": err}), 400
    params, err = _validate_params(body.get("params"))
    if err:  # 校验全部通过才进锁写盘 → 400 请求对 styles.json 零副作用
        return jsonify({"error": err}), 400

    style_id = (body.get("id") or "").strip()

    # 与生成示例图的回写共用同一把锁：否则本次的整表覆写会把并发写入的 sample_* 抹掉
    with _SAVE_LOCK:
        data = _load_styles()
        if not style_id:
            style_id = _new_style_id(data)
        existing = next((s for s in data["styles"] if s["id"] == style_id), None)
        entry = {
            "id": style_id,
            "name": name,
            "positive_prefix": positive,
            "negative_prefix": negative,
            "params": params,
            "sample_image": existing.get("sample_image") if existing else None,
            "created_at": existing.get("created_at") if existing else int(time.time()),
        }
        if existing and "sample_ts" in existing:
            entry["sample_ts"] = existing["sample_ts"]  # 丢了它前端缩略图的 cache-busting 就失效
        if existing:
            data["styles"] = [entry if s["id"] == style_id else s for s in data["styles"]]
        else:
            data["styles"].append(entry)
        _save_styles(data)
    return jsonify(entry)


@styles_bp.route("/api/styles/<style_id>", methods=["DELETE"])
def api_delete_style(style_id):
    with _SAVE_LOCK:  # 同 upsert：读改写整表期间不能有别的请求插进来写
        data = _load_styles()
        remaining = [s for s in data["styles"] if s["id"] != style_id]
        if len(remaining) == len(data["styles"]):
            return jsonify({"error": "not found"}), 404
        data["styles"] = remaining
        if data.get("active") == style_id:
            data["active"] = remaining[0]["id"] if remaining else ""
        # 清理指向被删预设的 per-bot 项，避免悬空 id
        data["active_by_bot"] = {b: sid for b, sid in data.get("active_by_bot", {}).items()
                                if sid != style_id}
        _save_styles(data)
    return jsonify({"ok": True, "active": data["active"], "active_by_bot": data["active_by_bot"]})


@styles_bp.route("/api/styles/active", methods=["POST"])
def api_set_active():
    body = request.get_json(silent=True) or {}
    style_id = (body.get("id") or "").strip()
    # bot 不做 strip：" mybot" 被 strip 成 "mybot" 会让一个笔误静默改掉真实绑定，必须报错
    bot = body.get("bot") or ""
    with _SAVE_LOCK:
        data = _load_styles()
        if not any(s["id"] == style_id for s in data["styles"]):
            return jsonify({"error": "style not found"}), 404
        if bot:  # 设某个 bot 的画风
            if bot not in {b["id"] for b in _enabled_bots()}:
                return jsonify({"error": f"unknown bot {bot}"}), 400
            data.setdefault("active_by_bot", {})[bot] = style_id
        else:  # 无 bot：设全局兜底（兼容旧调用）
            data["active"] = style_id
        _save_styles(data)
    return jsonify({"ok": True, "active": data.get("active", ""),
                    "active_by_bot": data.get("active_by_bot", {})})


@styles_bp.route("/api/styles/<style_id>/sample", methods=["POST"])
def api_generate_sample(style_id):
    # 同一预设重复点击会有两个进程写同一张图与同一个 state 目录，直接挡在门口
    with _SAVE_LOCK:
        data = _load_styles()
        if not any(s["id"] == style_id for s in data["styles"]):
            return jsonify({"error": "style not found"}), 404
        if style_id in _SAMPLING_IDS:
            return jsonify({"error": "示例图生成中"}), 409
        _SAMPLING_IDS.add(style_id)
    try:
        return _run_sample(style_id)
    finally:
        with _SAVE_LOCK:
            _SAMPLING_IDS.discard(style_id)


def _run_sample(style_id):
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    output_image_path = SAMPLE_DIR / f"{style_id}.png"
    state_dir = SAMPLE_DIR / "_state" / style_id
    state_dir.mkdir(parents=True, exist_ok=True)

    intermediate_path = state_dir / "intermediate.json"
    with intermediate_path.open("w", encoding="utf-8") as f:
        json.dump({"prompt": SAMPLE_PROMPT, "mode": "new"}, f, ensure_ascii=False)

    env = {**os.environ, "NOVELAI_ACTIVE_STYLE_ID": style_id}
    cmd = [
        PYTHON_BIN, str(GENERATE_SCRIPT),
        "--intermediate", str(intermediate_path),
        "--config", str(DEFAULT_CONFIG_PATH),
        "--output-image-path", str(output_image_path),
        "--state-dir", str(state_dir),
        "--ratio", "square",  # 示例图恒为 1024 见方，画风之间才有可比性
    ]
    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "生图超时"}), 504

    if result.returncode != 0:
        return jsonify({"error": "生图失败", "stderr": _scrub_secrets(result.stderr)}), 500

    ts = int(time.time())  # 前端用它做 cache-busting，覆盖后强制刷新
    # 重新读最新 styles.json，只改自己这条 → 不覆盖其他并发请求刚写的示例图
    with _SAVE_LOCK:
        fresh = _load_styles()
        found = False
        for s in fresh["styles"]:
            if s["id"] == style_id:
                s["sample_image"] = str(output_image_path)
                s["sample_ts"] = ts
                found = True
                break
        if not found:  # 生图期间该预设被删了
            return jsonify({"error": "style deleted during generation"}), 404
        _save_styles(fresh)
    return jsonify({"ok": True, "sample_image": str(output_image_path), "sample_ts": ts})


# ── NovelAI Key 管理（写 skill 的 .env.local，generate 脚本 load_local_env 会读）──
NOVELAI_ENV = SKILL_ROOT / ".env.local"
_NOVELAI_KEYS = ("NOVELAI_JWT", "NOVELAI_BEARER_TOKEN", "NOVELAI_TOKEN")


def _current_novelai_token() -> str:
    """读 .env.local 里现有的 NovelAI token（任一命名）。找不到返回空。"""
    if not NOVELAI_ENV.exists():
        return ""
    for line in NOVELAI_ENV.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        if k.strip() in _NOVELAI_KEYS:
            v = v.strip().strip("'").strip('"')
            if v:
                return v
    return ""


def _write_novelai_token(token: str) -> None:
    """把 token 写进 .env.local 的 NOVELAI_BEARER_TOKEN，删掉旧的 JWT/TOKEN 行
    （否则读取优先级 JWT>BEARER 会让旧 key 盖过新设的）。保留其它无关行。文件权限 600。"""
    kept = []
    if NOVELAI_ENV.exists():
        for line in NOVELAI_ENV.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if "=" in s and s.split("=", 1)[0].strip() in _NOVELAI_KEYS:
                continue  # 丢弃所有旧 novelai token 行
            kept.append(line)
    kept.append(f"NOVELAI_BEARER_TOKEN={token}")
    NOVELAI_ENV.parent.mkdir(parents=True, exist_ok=True)
    NOVELAI_ENV.write_text("\n".join(kept).strip() + "\n", encoding="utf-8")
    os.chmod(NOVELAI_ENV, 0o600)  # 仅本人可读写，防止 key 被其它用户读到


@styles_bp.route("/api/novelai_key", methods=["GET"])
def api_get_novelai_key():
    tok = _current_novelai_token()
    return jsonify({"set": bool(tok), "masked": _mask(tok)})  # 只回状态+末4位，绝不回明文


@styles_bp.route("/api/novelai_key", methods=["POST"])
def api_set_novelai_key():
    body = request.get_json(silent=True) or {}
    token = (body.get("token") or "").strip()
    if not token:
        return jsonify({"error": "token required"}), 400
    _write_novelai_token(token)
    return jsonify({"ok": True, "masked": _mask(token)})
