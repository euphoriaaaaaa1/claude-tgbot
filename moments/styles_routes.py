"""NovelAI 画风预设管理（Flask Blueprint）。

管理 ``$NOVELAI_SKILL_ROOT/assets/styles.json``（默认
``~/.claude/skills/novelai-skill``）：
- 网页 /styles 展示所有预设卡片，可新建/编辑/删除/设为当前/生成示例图
- generate_novelai_image.py 通过 apply_active_style() 在生图前读取该文件生效

公开版约束：provider 模式下本蓝图只有页面与样式管理端点可达，密钥端点
``/api/novelai_key`` 不在白名单内（恒 404）；令牌用
``python3 scripts/set_novelai_key.py`` 写入 skill 的 ``.env.local``。
"""
import os
import re
import json
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from flask import Blueprint, abort, jsonify, render_template, request, send_file

import config_loader

styles_bp = Blueprint("styles", __name__)

# 保护 styles.json 的 read-modify-write：并发生成示例图时，各请求生图耗时长(30-60s)，
# 若各自拿启动快照全量覆写会互相覆盖。锁只包 json 更新(几ms)，生图在锁外并发跑。
_SAVE_LOCK = threading.Lock()
_SAMPLING_IDS = set()  # 正在生成示例图的 style_id，由 _SAVE_LOCK 保护

# 生图脚本的 stderr 可能带上 NovelAI 令牌（请求头回显、异常里的 curl 命令等），
# 存储与回包前都必须先抹掉。
_SECRET_RE = re.compile(r"pst-[\w-]+|Bearer\s+\S+|Authorization[^\n]*", re.I)


def _scrub_secrets(text: str) -> str:
    return _SECRET_RE.sub("***", text or "")[-2000:]


# 路径可用 NOVELAI_SKILL_ROOT 覆盖：测试通过真实 HTTP 接口读写 styles.json，
# 若测试实例与生产共用同一份文件，跑一次测试就会改乱真实的 bot 画风绑定。
SKILL_ROOT = Path(os.environ.get("NOVELAI_SKILL_ROOT")
                  or os.path.expanduser("~/.claude/skills/novelai-skill"))
STYLES_PATH = SKILL_ROOT / "assets" / "styles.json"
DEFAULT_CONFIG_PATH = SKILL_ROOT / "assets" / "default_config.json"
GENERATE_SCRIPT = SKILL_ROOT / "scripts" / "generate_novelai_image.py"
PYTHON_BIN = os.environ.get("NOVELAI_PYTHON") or sys.executable
# 示例图目录在用到时由 _sample_dir() 按图片根目录现算，不在导入时固定；
# 赋了值则以赋的值为准（测试用临时目录）。
SAMPLE_DIR = None
# 样图走分角色写法：画面与画风标签进 prompt，人物标签原样进唯一的角色槽（单角色 auto）；
# 质量词由画风前缀注入，正文不写。
SAMPLE_INTERMEDIATE = {
    "mode": "new",
    "prompt": ("a nurse sitting with crossed legs in a sunlit hospital room, smiling, "
               "1girl, solo, year 2024, cover page, -1::monocrome, flat color, simple background, "
               "text logo::, photo background, hospital, indoors, sunlight, lens flare"),
    "position_mode": "auto",
    "characters": [{"positive": ("girl, nurse, latex gloves, very long hair, red eyes, red hair, "
                                 "black pantyhose, cardigan, large breasts, skinny, crossed legs, smile")}],
}


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
    cfg_scale 的 0 是**非法值不是未设置**：下限校验必须挡在写盘之前。
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


def _load_styles() -> dict:
    """读 styles.json；文件缺失 → 干净底稿。坏 JSON/IO 异常按读失败向上抛。"""
    if not STYLES_PATH.exists():
        return {"active": "default", "active_by_bot": {}, "styles": []}
    with STYLES_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("active_by_bot", {})  # 老文件平滑迁移
    return data


def _save_styles_atomic(data: dict) -> None:
    """同目录临时文件 + os.replace 原子替换：生图脚本随时直读该文件，防半截 JSON。"""
    STYLES_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(STYLES_PATH.parent),
                               prefix=".styles.json-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, STYLES_PATH)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _save_styles(data: dict) -> None:
    """常规写口：同目录临时文件 + os.replace，避免生图脚本读到半截 JSON。"""
    _save_styles_atomic(data)


def _bot_rows() -> list:
    """已发现 bot（含停用）：读失败按空清单处理，不影响样式页本身。"""
    try:
        return config_loader.list_enabled_bots(include_disabled=True)
    except Exception:
        return []


def _known_bot_ids() -> set:
    """per-bot 绑定校验用 id 集合。"""
    return {b["_bot_id"] for b in _bot_rows()}


def _bots_payload() -> list:
    """下发给 /styles 页与 /api/styles 的 bot 清单（与 models/samplers 同款单一数据源）。"""
    return [{"id": b["_bot_id"],
             "name": b.get("display_name") or b["_bot_id"],
             "has_config": True}
            for b in _bot_rows()]


@styles_bp.route("/styles")
def styles_page():
    data = _load_styles()
    return render_template("styles.html", styles=data.get("styles", []),
                           active=data.get("active", ""),
                           active_by_bot=data.get("active_by_bot", {}),
                           bots=_bots_payload())


@styles_bp.route("/styles/sample/<style_id>")
def style_sample_image(style_id):
    """只读示例图：provider 模式下旧的 ``/image/<path>`` 路由不可达，画风页改走这里。"""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", style_id):
        abort(404)
    path = _sample_dir() / f"{style_id}.png"
    try:
        if not path.is_file():
            abort(404)
    except OSError:
        abort(404)
    return send_file(path, mimetype="image/png", max_age=0)


@styles_bp.route("/api/styles", methods=["GET"])
def api_get_styles():
    data = _load_styles()
    # bot 清单与下拉选项都跟着数据一起下发，前端不用再维护第二份清单
    data["bots"] = _bots_payload()
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
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        # 数组/标量/坏 JSON 一律 400（原先对数组会 AttributeError 500，契约要求 400）
        return jsonify({"error": "请求体必须是 JSON 对象"}), 400
    style_id = (body.get("id") or "").strip()
    # bot 不做 strip：" bot_a" 被 strip 成 "bot_a" 会让一个笔误静默改掉真实绑定，必须报错
    bot = body.get("bot") or ""
    with _SAVE_LOCK:
        data = _load_styles()
        if not any(s["id"] == style_id for s in data["styles"]):
            return jsonify({"error": "style not found"}), 404
        if bot:  # 设某个 bot 的画风（非字符串真值按"非发现集合"同判，原样回显）
            if not isinstance(bot, str) or bot not in _known_bot_ids():
                return jsonify({"error": f"unknown bot {bot}"}), 400
            data.setdefault("active_by_bot", {})[bot] = style_id
        else:  # 无 bot：设全局兜底（兼容旧调用）
            data["active"] = style_id
        _save_styles(data)
    return jsonify({"ok": True, "active": data.get("active", ""),
                    "active_by_bot": data.get("active_by_bot", {})})


# 示例图生成 = 后台任务 + 轮询：生图要 30–60s（上限 180s），同步跑会把请求线程
# （以及点按钮的用户界面）卡满。POST 立即回任务标识（task_id + poll_url），生图 + 回写
# 在后台线程跑；轮询走**同一条路径的 GET**。_SAMPLING_IDS 挡"同一预设并发"（第二个 409），
# _SAMPLE_TASKS 保留该预设最近一次任务的状态供轮询。
_SAMPLE_TASKS = {}            # style_id -> {task_id, status, sample_image, sample_ts, error, stderr}
_SAMPLE_TASKS_LOCK = threading.Lock()


@styles_bp.route("/api/styles/<style_id>/sample", methods=["POST"])
def api_generate_sample(style_id):
    with _SAVE_LOCK:
        data = _load_styles()
        if not any(s["id"] == style_id for s in data["styles"]):
            return jsonify({"error": "style not found"}), 404
        # 同一预设重复点击会有两个进程写同一张图与同一个 state 目录，直接挡在门口
        if style_id in _SAMPLING_IDS:
            return jsonify({"error": "示例图生成中"}), 409
        _SAMPLING_IDS.add(style_id)
        task_id = secrets.token_hex(8)
        with _SAMPLE_TASKS_LOCK:
            _SAMPLE_TASKS[style_id] = {"task_id": task_id, "status": "running",
                                       "sample_image": None, "sample_ts": None,
                                       "error": None, "stderr": None}
    # 线程在锁外启动：worker 结尾要拿 _SAVE_LOCK 回写，锁内起线程白白制造等待
    threading.Thread(target=_sample_worker, args=(style_id, task_id), daemon=True).start()
    return jsonify({"ok": True, "task_id": task_id, "status": "running",
                    "poll_url": "/api/styles/%s/sample" % style_id}), 202


@styles_bp.route("/api/styles/<style_id>/sample", methods=["GET"])
def api_sample_status(style_id):
    """轮询示例图任务（与 POST 同路径的 GET）：running 时 done=False；done/failed 后给
    sample_image 或 error。路径不另开新参数化路由。"""
    with _SAMPLE_TASKS_LOCK:
        task = _SAMPLE_TASKS.get(style_id)
        snapshot = dict(task) if task else None
    if snapshot is None:
        return jsonify({"error": "no sample task for this style"}), 404
    status = snapshot["status"]
    body = {
        "task_id": snapshot["task_id"],
        "done": status in ("done", "failed"),
        "ok": status == "done",
        "status": status,
        "sample_image": snapshot["sample_image"],
        "sample_ts": snapshot["sample_ts"],
    }
    if snapshot.get("error"):
        body["error"] = snapshot["error"]
    if snapshot.get("stderr"):
        body["stderr"] = snapshot["stderr"]
    return jsonify(body)


def _sample_worker(style_id: str, task_id: str):
    """后台线程体：生图 + 回写 + 记任务状态；任何异常都不许把任务永久留在 running。"""
    try:
        result = _run_sample(style_id)
    except Exception as e:
        # 只记异常类型：异常原文可能带路径/上游地址等，不落日志（详细原因经轮询 error 出站，同样脱敏）
        sys.stderr.write("[styles] 示例图任务异常 %s\n" % type(e).__name__)
        result = {"ok": False, "error": "生图异常：%s" % type(e).__name__}
    with _SAMPLE_TASKS_LOCK:
        task = _SAMPLE_TASKS.get(style_id)
        if task and task.get("task_id") == task_id:   # 期间又发起了新任务则不覆盖
            task["status"] = "done" if result.get("ok") else "failed"
            task["sample_image"] = result.get("sample_image")
            task["sample_ts"] = result.get("sample_ts")
            task["error"] = result.get("error")
            task["stderr"] = result.get("stderr")
    with _SAVE_LOCK:
        _SAMPLING_IDS.discard(style_id)


def _sample_dir() -> Path:
    """示例图目录：图片根目录取 MOMENTS_MEDIA_ROOT，没设时 ~/resource/media，后接 __styles__。"""
    if SAMPLE_DIR is not None:
        return Path(SAMPLE_DIR)
    root = os.environ.get("MOMENTS_MEDIA_ROOT") or "~/resource/media"
    return Path(os.path.expanduser(root)) / "__styles__"


def _run_sample(style_id):
    """同步生图 + 回写 styles.json，返回结果 dict（供后台线程与轮询出站）。"""
    sample_dir = _sample_dir()
    sample_dir.mkdir(parents=True, exist_ok=True)
    output_image_path = sample_dir / f"{style_id}.png"
    state_dir = sample_dir / "_state" / style_id
    state_dir.mkdir(parents=True, exist_ok=True)

    intermediate_path = state_dir / "intermediate.json"
    with intermediate_path.open("w", encoding="utf-8") as f:
        json.dump(SAMPLE_INTERMEDIATE, f, ensure_ascii=False)

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
        return {"ok": False, "error": "生图超时"}

    if result.returncode != 0:
        return {"ok": False, "error": "生图失败", "stderr": _scrub_secrets(result.stderr)}

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
            return {"ok": False, "error": "style deleted during generation"}
        _save_styles(fresh)
    return {"ok": True, "sample_image": str(output_image_path), "sample_ts": ts}
