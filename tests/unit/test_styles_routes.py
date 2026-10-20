# -*- coding: utf-8 -*-
"""T11 画风页单测：样式 CRUD、参数校验、示例图路径、stderr 打码、轮询与密钥 CLI。

全部落盘在 tmp_path 下，不启动真实 8767，不连真实 NovelAI，不读真实
``~/.claude/secrets/`` 或 ``~/.claude/skills/novelai-skill``。
"""
import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from flask import Flask

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from moments import panel_auth, styles_routes, web  # noqa: E402

PASSWORD = "test-panel-pw-12345"


@pytest.fixture(autouse=True)
def _clean_globals():
    styles_routes._SAMPLE_TASKS.clear()
    styles_routes._SAMPLING_IDS.clear()
    yield
    styles_routes._SAMPLE_TASKS.clear()
    styles_routes._SAMPLING_IDS.clear()


@pytest.fixture
def styles_env(tmp_path, monkeypatch):
    root = tmp_path / "skill"
    (root / "assets").mkdir(parents=True)
    (root / "scripts").mkdir()
    monkeypatch.setattr(styles_routes, "SKILL_ROOT", root)
    monkeypatch.setattr(styles_routes, "STYLES_PATH", root / "assets" / "styles.json")
    monkeypatch.setattr(styles_routes, "DEFAULT_CONFIG_PATH", root / "assets" / "default_config.json")
    monkeypatch.setattr(styles_routes, "GENERATE_SCRIPT", root / "scripts" / "generate_novelai_image.py")
    monkeypatch.setattr(styles_routes, "PYTHON_BIN", sys.executable)
    monkeypatch.setattr(styles_routes, "SAMPLE_DIR", tmp_path / "media" / "__styles__")
    # bot 清单钉死，避免读仓库/机器上的真实 configs。
    monkeypatch.setattr(styles_routes, "_bot_rows", lambda: [
        {"_bot_id": "bot1", "display_name": "Bot One"},
        {"_bot_id": "bot2", "display_name": "Bot Two"},
    ])
    return root


@pytest.fixture
def client(styles_env):
    app = Flask(__name__, template_folder=str(_ROOT / "moments" / "templates"))
    app.register_blueprint(styles_routes.styles_bp)
    return app.test_client()


@pytest.fixture
def web_client(tmp_path, monkeypatch, styles_env):
    monkeypatch.setattr(panel_auth, "SECRET_PATH",
                        str(tmp_path / "secrets" / "moments-panel.env"))
    monkeypatch.setattr(panel_auth, "SESSIONS_PATH",
                        str(tmp_path / "secrets" / "moments-panel-sessions.json"))
    panel_auth._fail.clear()
    panel_auth.set_password(PASSWORD)
    c = web.app.test_client()
    r = c.post("/login", data={"password": PASSWORD, "next": "/styles"})
    assert r.status_code == 302, r.get_data(as_text=True)
    yield c
    panel_auth._fail.clear()


def _create(client, **over):
    body = {"name": "预设 A", "positive_prefix": "neon", "negative_prefix": "blur"}
    body.update(over)
    r = client.post("/api/styles", json=body)
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


def _wait_done(client, style_id, timeout=5.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get("/api/styles/%s/sample" % style_id).get_json()
        if last and last.get("done"):
            return last
        time.sleep(0.02)
    raise AssertionError("轮询未在时限内完成：%r" % (last,))


# ── 读取与 CRUD ────────────────────────────────────────────────────────────

def test_get_styles_initial_payload(client):
    d = client.get("/api/styles").get_json()
    assert d["active"] == "default"
    assert d["styles"] == []
    assert d["active_by_bot"] == {}
    assert d["bots"] == [{"id": "bot1", "name": "Bot One", "has_config": True},
                         {"id": "bot2", "name": "Bot Two", "has_config": True}]
    assert d["models"][0]["value"] == ""
    assert "k_euler_ancestral" in d["samplers"]
    assert [p["value"] for p in d["uc_presets"]] == [0, 1, 2, 3]


def test_create_style_persists_atomic_json(client, styles_env):
    entry = _create(client, name="赛博霓虹", params={"steps": 20, "cfg_scale": 5.5,
                                                   "sampler": "k_euler", "ucPreset": 2})
    assert entry["id"].startswith("style_")
    assert entry["name"] == "赛博霓虹"
    assert entry["params"] == {"steps": 20, "cfg_scale": 5.5,
                               "sampler": "k_euler", "ucPreset": 2}
    saved = json.loads(styles_routes.STYLES_PATH.read_text(encoding="utf-8"))
    assert saved["styles"][0]["id"] == entry["id"]
    # 原子写不留下临时文件
    assert not list(styles_routes.STYLES_PATH.parent.glob(".styles.json-*.tmp"))


def test_update_style_keeps_sample_metadata(client, styles_env):
    entry = _create(client, id="s1")
    data = json.loads(styles_routes.STYLES_PATH.read_text(encoding="utf-8"))
    data["styles"][0]["sample_image"] = "/tmp/s1.png"
    data["styles"][0]["sample_ts"] = 111
    styles_routes.STYLES_PATH.write_text(json.dumps(data), encoding="utf-8")

    r = client.post("/api/styles", json={"id": "s1", "name": "改名"})
    assert r.status_code == 200
    updated = r.get_json()
    assert updated["name"] == "改名"
    assert updated["sample_image"] == "/tmp/s1.png"
    assert updated["sample_ts"] == 111
    assert updated["created_at"] == entry["created_at"]


@pytest.mark.parametrize("body", [
    [], {"name": ""}, {"name": 123}, {"name": "x" * 201},
    {"name": "ok", "positive_prefix": 123},
    {"name": "ok", "positive_prefix": "x" * 2001},
    {"name": "ok", "params": []},
    {"name": "ok", "params": {"unknown": 1}},
    {"name": "ok", "params": {"model": "bad-model"}},
    {"name": "ok", "params": {"steps": 0}},
    {"name": "ok", "params": {"steps": 51}},
    {"name": "ok", "params": {"steps": True}},
    {"name": "ok", "params": {"steps": "10"}},
    {"name": "ok", "params": {"cfg_scale": 0}},
    {"name": "ok", "params": {"cfg_scale": 11}},
    {"name": "ok", "params": {"cfg_scale": True}},
    {"name": "ok", "params": {"sampler": "bad"}},
    {"name": "ok", "params": {"ucPreset": 4}},
    {"name": "ok", "params": {"ucPreset": True}},
])
def test_create_style_rejects_invalid_body_without_write(client, body, styles_env):
    r = client.post("/api/styles", json=body)
    assert r.status_code == 400, r.get_data(as_text=True)
    assert "error" in r.get_json()
    assert not styles_routes.STYLES_PATH.exists()


def test_create_style_rejects_non_json(client, styles_env):
    r = client.post("/api/styles", data="not json", content_type="text/plain")
    assert r.status_code == 400
    assert not styles_routes.STYLES_PATH.exists()


def test_delete_style_cleans_active_and_per_bot(client):
    first = _create(client, id="s1", name="一")
    second = _create(client, id="s2", name="二")
    assert client.post("/api/styles/active", json={"id": "s1"}).status_code == 200
    assert client.post("/api/styles/active",
                       json={"id": "s2", "bot": "bot1"}).status_code == 200

    r = client.delete("/api/styles/s2")
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is True
    assert d["active_by_bot"] == {}
    assert client.delete("/api/styles/s2").status_code == 404

    # 删除当前全局预设 → active 落到剩下的第一条
    r = client.delete("/api/styles/s1")
    assert r.status_code == 200
    assert r.get_json()["active"] == ""
    assert first["id"] != second["id"]


def test_set_active_errors_and_success(client):
    _create(client, id="s1")
    assert client.post("/api/styles/active", json=[]).status_code == 400
    assert client.post("/api/styles/active", json={"id": "nope"}).status_code == 404
    assert client.post("/api/styles/active",
                       json={"id": "s1", "bot": "nobody"}).status_code == 400
    assert client.post("/api/styles/active",
                       json={"id": "s1", "bot": 123}).status_code == 400
    # 不做 strip：" bot1" 是笔误，必须报错而不是静默改绑定
    assert client.post("/api/styles/active",
                       json={"id": "s1", "bot": " bot1"}).status_code == 400

    r = client.post("/api/styles/active", json={"id": "s1", "bot": "bot1"})
    assert r.status_code == 200
    assert r.get_json()["active_by_bot"] == {"bot1": "s1"}
    r = client.post("/api/styles/active", json={"id": "s1"})
    assert r.status_code == 200
    assert r.get_json()["active"] == "s1"


# ── 示例图只读路径 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("style_id", [
    "..", "..%2Fetc%2Fpasswd", "bad id", "a" * 101, ".hidden", "ok.PNG",
])
def test_sample_image_rejects_bad_id(client, style_id):
    assert client.get("/styles/sample/" + style_id).status_code == 404


def test_sample_image_missing_and_present(client, styles_env):
    assert client.get("/styles/sample/s1").status_code == 404
    sample_dir = styles_routes._sample_dir()
    sample_dir.mkdir(parents=True)
    (sample_dir / "s1.png").write_bytes(b"\x89PNG-fake")
    (sample_dir / "adir.png").mkdir()
    r = client.get("/styles/sample/s1")
    assert r.status_code == 200
    assert r.mimetype == "image/png"
    assert r.data == b"\x89PNG-fake"
    assert client.get("/styles/sample/adir").status_code == 404


# ── 示例图后台任务：并发、轮询、失败打码 ──────────────────────────────────

def test_sample_task_conflict_and_polling(client, monkeypatch):
    _create(client, id="s1")
    gate = threading.Event()

    def fake_run(style_id):
        gate.wait(timeout=5)
        out = styles_routes._sample_dir() / ("%s.png" % style_id)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"png")
        with styles_routes._SAVE_LOCK:
            data = styles_routes._load_styles()
            for s in data["styles"]:
                if s["id"] == style_id:
                    s["sample_image"] = str(out)
                    s["sample_ts"] = 123
            styles_routes._save_styles(data)
        return {"ok": True, "sample_image": str(out), "sample_ts": 123}

    monkeypatch.setattr(styles_routes, "_run_sample", fake_run)
    try:
        r1 = client.post("/api/styles/s1/sample")
        assert r1.status_code == 202
        first = r1.get_json()
        assert first["ok"] is True and first["status"] == "running"
        assert first["poll_url"] == "/api/styles/s1/sample"
        assert first["task_id"]

        r2 = client.post("/api/styles/s1/sample")
        assert r2.status_code == 409

        status = client.get("/api/styles/s1/sample").get_json()
        assert status["done"] is False and status["status"] == "running"
    finally:
        gate.set()

    done = _wait_done(client, "s1")
    assert done["ok"] is True
    assert done["sample_image"].endswith("s1.png")
    assert done["sample_ts"] == 123
    saved = json.loads(styles_routes.STYLES_PATH.read_text(encoding="utf-8"))
    assert saved["styles"][0]["sample_image"].endswith("s1.png")


def test_sample_post_and_status_404(client):
    assert client.post("/api/styles/nope/sample").status_code == 404
    r = client.get("/api/styles/nope/sample")
    assert r.status_code == 404
    assert r.get_json()["error"]


def test_scrub_secrets_masks_token_shapes():
    raw = ("cmd -H 'Authorization: Bearer pst-abc123' "
           "Bearer plain-secret pst-xyz-789")
    out = styles_routes._scrub_secrets(raw)
    for secret in ("pst-abc123", "plain-secret", "pst-xyz-789"):
        assert secret not in out
    assert "***" in out
    assert styles_routes._scrub_secrets("") == ""


def test_run_sample_failure_scrubs_stderr_and_poll_response(client, monkeypatch):
    _create(client, id="s1")

    class FakeCompleted:
        returncode = 1
        stdout = ""
        stderr = ("curl -H 'Authorization: Bearer pst-secret-123' "
                  "-H 'Bearer another-secret' https://example.invalid")

    monkeypatch.setattr(styles_routes.subprocess, "run",
                        lambda *a, **k: FakeCompleted())
    result = styles_routes._run_sample("s1")
    assert result["ok"] is False
    assert "pst-secret-123" not in result["stderr"]
    assert "another-secret" not in result["stderr"]
    assert "***" in result["stderr"]

    styles_routes._SAMPLE_TASKS["s1"] = {
        "task_id": "task-1", "status": "running", "sample_image": None,
        "sample_ts": None, "error": None, "stderr": None}
    styles_routes._sample_worker("s1", "task-1")
    d = client.get("/api/styles/s1/sample").get_json()
    assert d["done"] is True and d["ok"] is False
    assert "pst-secret-123" not in (d.get("stderr") or "")
    assert "another-secret" not in (d.get("stderr") or "")
    assert "***" in d["stderr"]


def test_run_sample_success_writes_styles_atomically(client, monkeypatch, styles_env):
    _create(client, id="s1")

    class OkCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(styles_routes.subprocess, "run",
                        lambda *a, **k: OkCompleted())
    result = styles_routes._run_sample("s1")
    assert result["ok"] is True
    assert result["sample_image"].endswith("s1.png")
    saved = json.loads(styles_routes.STYLES_PATH.read_text(encoding="utf-8"))
    assert saved["styles"][0]["sample_image"] == result["sample_image"]
    assert isinstance(saved["styles"][0]["sample_ts"], int)
    assert not list(styles_routes.STYLES_PATH.parent.glob(".styles.json-*.tmp"))


# ── 模板与壳可达面 ────────────────────────────────────────────────────────

def test_styles_template_has_no_external_or_key_ui():
    html = (_ROOT / "moments" / "templates" / "styles.html").read_text(encoding="utf-8")
    assert "fonts.googleapis.com" not in html
    assert "fonts.gstatic.com" not in html
    assert "/api/novelai_key" not in html
    assert "http://" not in html and "https://" not in html


def test_provider_mode_styles_reachable_key_not(web_client):
    assert web_client.get("/styles").status_code == 200
    assert web_client.get("/api/styles").status_code == 200
    assert web_client.get("/styles/sample/missing").status_code == 404

    assert web_client.get("/api/novelai_key").status_code == 404
    r = web_client.post("/api/novelai_key", json={"token": "pst-should-not-write"})
    assert r.status_code == 404


def test_provider_mode_styles_requires_login(tmp_path, monkeypatch, styles_env):
    monkeypatch.setattr(panel_auth, "SECRET_PATH",
                        str(tmp_path / "secrets" / "moments-panel.env"))
    monkeypatch.setattr(panel_auth, "SESSIONS_PATH",
                        str(tmp_path / "secrets" / "moments-panel-sessions.json"))
    panel_auth.set_password(PASSWORD)
    panel_auth._fail.clear()
    c = web.app.test_client()
    assert c.get("/api/styles").status_code == 401
    r = c.get("/styles")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


# ── set_novelai_key.py CLI ────────────────────────────────────────────────

def _run_cli(root, args=(), stdin_text=None):
    env = {**os.environ, "NOVELAI_SKILL_ROOT": str(root)}
    return subprocess.run(
        [sys.executable, str(_ROOT / "scripts" / "set_novelai_key.py"), *args],
        input=stdin_text, text=True, capture_output=True, env=env, timeout=30)


def test_set_key_cli_writes_0600_without_echo(tmp_path):
    root = tmp_path / "skill"
    token = "pst-cli-secret-token-0001"
    p = _run_cli(root, stdin_text=token + "\n")
    assert p.returncode == 0, p.stderr
    assert token not in p.stdout and token not in p.stderr
    env_file = root / ".env.local"
    assert env_file.read_text(encoding="utf-8") == "NOVELAI_BEARER_TOKEN=%s\n" % token
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600

    c = _run_cli(root, args=("--check",))
    assert c.returncode == 0
    assert c.stdout == "set: true\n"
    assert token not in c.stdout + c.stderr


def test_set_key_cli_replaces_old_names_and_keeps_other_lines(tmp_path):
    root = tmp_path / "skill"
    root.mkdir()
    env_file = root / ".env.local"
    env_file.write_text("NOVELAI_JWT=old-jwt\nFOO=bar\nexport NOVELAI_TOKEN=old-token\n",
                        encoding="utf-8")
    token = "pst-new-token-0002"
    p = _run_cli(root, stdin_text=token + "\n")
    assert p.returncode == 0
    text = env_file.read_text(encoding="utf-8")
    assert "old-jwt" not in text and "old-token" not in text
    assert "FOO=bar" in text
    assert text.count("NOVELAI_BEARER_TOKEN=") == 1
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert token not in p.stdout + p.stderr


def test_set_key_cli_empty_token_and_bad_args(tmp_path):
    root = tmp_path / "skill"
    p = _run_cli(root, stdin_text="\n")
    assert p.returncode == 1
    assert not (root / ".env.local").exists()

    p = _run_cli(root, args=("--bogus",), stdin_text="x\n")
    assert p.returncode == 2
    assert not (root / ".env.local").exists()

    c = _run_cli(root, args=("--check",))
    assert c.returncode == 0
    assert c.stdout == "set: false\n"


# ── style_id 路径穿越 ─────────────────────────────────────────────────────

from urllib.parse import quote  # noqa: E402


@pytest.mark.parametrize("bad", [
    "..\\..\\x", "../../../x", "a/b", "", "bad id", "ok.PNG", "a" * 65, "a\x00b",
])
def test_valid_style_id_rejects_bad_values(bad):
    assert not styles_routes._valid_style_id(bad)


@pytest.mark.parametrize("good", ["a", "style-1", "Style_2", "x" * 64])
def test_valid_style_id_accepts_identifiers(good):
    assert styles_routes._valid_style_id(good)


@pytest.mark.parametrize("bad", ["../../x", "..\\..\\x", "a/b", "bad id", "a" * 65])
def test_upsert_rejects_bad_style_id(client, bad):
    r = client.post("/api/styles", json={
        "id": bad, "name": "x", "positive_prefix": "p", "negative_prefix": "n",
    })
    assert r.status_code == 422, r.get_data(as_text=True)
    assert r.get_json() == {"error": "invalid"}


@pytest.mark.parametrize("bad", ["..\\..\\x", "bad id", "a" * 65])
def test_delete_and_sample_routes_reject_bad_style_id(client, bad):
    seg = quote(bad, safe="")
    assert client.delete("/api/styles/" + seg).status_code == 422
    assert client.post("/api/styles/" + seg + "/sample").status_code == 422
    assert client.get("/api/styles/" + seg + "/sample").status_code == 422


def test_slash_style_id_never_reaches_handler(client):
    """含 `/` 的 id 被 URL 路由切段，根本到不了 handler；同样属于拒绝。"""
    assert client.delete("/api/styles/a%2Fb").status_code in (404, 422)
    assert not styles_routes._valid_style_id("a/b")


def test_normal_style_id_still_works(client):
    entry = _create(client, id="ok-1_2")
    assert entry["id"] == "ok-1_2"
    assert client.delete("/api/styles/ok-1_2").status_code == 200


def test_run_sample_revalidates_style_id(tmp_path, monkeypatch, styles_env):
    monkeypatch.setattr(styles_routes, "SAMPLE_DIR", tmp_path / "media" / "__styles__")
    with pytest.raises(ValueError):
        styles_routes._run_sample("../../x")
    assert not (tmp_path / "media" / "__styles__").exists()
