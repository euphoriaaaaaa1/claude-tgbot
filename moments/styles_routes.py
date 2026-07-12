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

styles_bp = Blueprint("styles", __name__)

# 保护 styles.json 的 read-modify-write：并发生成示例图时，各请求生图耗时长(30-60s)，
# 若各自拿启动快照全量覆写会互相覆盖。锁只包 json 更新(几ms)，生图在锁外并发跑。
_SAVE_LOCK = threading.Lock()

SKILL_ROOT = Path(os.path.expanduser("~/.claude/skills/novelai-skill"))
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
    return jsonify(_load_styles())


@styles_bp.route("/api/styles", methods=["POST"])
def api_upsert_style():
    body = request.get_json(force=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400

    data = _load_styles()
    style_id = (body.get("id") or "").strip()
    if not style_id:
        style_id = f"style_{int(time.time())}"

    existing = next((s for s in data["styles"] if s["id"] == style_id), None)
    entry = {
        "id": style_id,
        "name": name,
        "positive_prefix": body.get("positive_prefix", "") or "",
        "negative_prefix": body.get("negative_prefix", "") or "",
        "params": body.get("params") or {},
        "sample_image": existing.get("sample_image") if existing else None,
        "created_at": existing.get("created_at") if existing else int(time.time()),
    }
    if existing:
        data["styles"] = [entry if s["id"] == style_id else s for s in data["styles"]]
    else:
        data["styles"].append(entry)
    _save_styles(data)
    return jsonify(entry)


@styles_bp.route("/api/styles/<style_id>", methods=["DELETE"])
def api_delete_style(style_id):
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
    body = request.get_json(force=True) or {}
    style_id = (body.get("id") or "").strip()
    bot = (body.get("bot") or "").strip()
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
    data = _load_styles()
    style = next((s for s in data["styles"] if s["id"] == style_id), None)
    if style is None:
        return jsonify({"error": "style not found"}), 404

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
    ]
    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "生图超时"}), 504

    if result.returncode != 0:
        return jsonify({"error": "生图失败", "stderr": result.stderr[-2000:]}), 500

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


def _mask(tok: str) -> str:
    return ("****" + tok[-4:]) if len(tok) >= 4 else ("****" if tok else "")


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
    body = request.get_json(force=True) or {}
    token = (body.get("token") or "").strip()
    if not token:
        return jsonify({"error": "token required"}), 400
    _write_novelai_token(token)
    return jsonify({"ok": True, "masked": _mask(token)})
