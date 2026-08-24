# -*- coding: utf-8 -*-
"""styles 接口验收的夹具：整套跑在 Flask 测试客户端上，不监听端口、不碰真实 styles.json。

为什么不像线上那样起服务再打 HTTP：本机随时可能有一个真的 moments 服务在跑，
测试一旦连上去就会改乱真实的 bot 画风绑定。测试客户端走同一套路由与 JSON 契约，
但没有 socket，也就没有打错端口的可能。
"""
import json
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture
def styles_root(tmp_path, monkeypatch):
    """把 styles.json / 示例图目录整体挪到 tmp_path，返回 styles.json 路径。"""
    from moments import styles_routes

    root = tmp_path / "novelai-skill"
    (root / "assets").mkdir(parents=True)
    styles_path = root / "assets" / "styles.json"
    styles_path.write_text(json.dumps(
        {"active": "default", "active_by_bot": {}, "styles": []}, ensure_ascii=False),
        encoding="utf-8")
    monkeypatch.setattr(styles_routes, "SKILL_ROOT", root)
    monkeypatch.setattr(styles_routes, "STYLES_PATH", styles_path)
    monkeypatch.setattr(styles_routes, "DEFAULT_CONFIG_PATH", root / "assets" / "default_config.json")
    monkeypatch.setattr(styles_routes, "GENERATE_SCRIPT", root / "scripts" / "generate_novelai_image.py")
    monkeypatch.setattr(styles_routes, "SAMPLE_DIR", tmp_path / "samples")
    monkeypatch.setattr(styles_routes, "NOVELAI_ENV", root / ".env.local")
    styles_routes._SAMPLING_IDS.clear()
    return styles_path


@pytest.fixture
def client(styles_root):
    """只挂 styles 蓝图的最小 Flask app（不引 moments 的其它路由与数据库）。"""
    from flask import Flask
    from moments.styles_routes import styles_bp

    app = Flask(__name__, template_folder=str(_ROOT / "moments" / "templates"))
    app.register_blueprint(styles_bp)
    app.config["TESTING"] = True
    # 不用 `with app.test_client()`：那会把请求上下文一直留着，并发用例里两个线程
    # 同时在飞时会互相顶掉上下文栈。这里每个请求各自压栈出栈。
    return app.test_client()


@pytest.fixture
def api(client):
    """把测试客户端包成 (code, body) 的小工具，断言里不用重复解 json。"""
    class _Api:
        def get(self, path):
            r = client.get(path)
            return r.status_code, _body(r)

        def post(self, path, payload=None, **kw):
            r = client.post(path, json=payload) if not kw else client.post(path, **kw)
            return r.status_code, _body(r)

        def delete(self, path):
            r = client.delete(path)
            return r.status_code, _body(r)

    def _body(resp):
        try:
            return resp.get_json()
        except Exception:
            return {"_raw_text": resp.get_data(as_text=True)[:500]}

    return _Api()


@pytest.fixture(autouse=True)
def _no_real_skill_root(monkeypatch):
    """兜底：即使某条用例忘了用 styles_root，也不让它落到真实 skill 目录。"""
    monkeypatch.delenv("NOVELAI_SKILL_ROOT", raising=False)
    os.environ.setdefault("NOVELAI_PYTHON", sys.executable)
