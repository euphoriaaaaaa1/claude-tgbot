# -*- coding: utf-8 -*-
"""styles 接口的参数白名单与并发保护验收（黑盒：只走路由与 JSON 契约）。"""
import json

import pytest

MODELS = ("nai-diffusion-5-full", "nai-diffusion-5-curated", "nai-diffusion-4-5-full")
SAMPLERS = ("k_euler_ancestral", "k_euler", "k_dpmpp_2s_ancestral",
            "k_dpmpp_2m_sde", "k_dpmpp_sde", "ddim_v3")
ALLOWED_PARAM_KEYS = {"model", "steps", "cfg_scale", "sampler", "ucPreset"}
E_CFG = "cfg_scale 必须是 1–10 的数字"
E_STEPS = "steps 必须是 1–50 的整数"
E_UC = "ucPreset 必须是 0、1、2、3 之一"


def _mk(api, **fields):
    body = {"name": "测试预设"}
    body.update(fields)
    code, data = api.post("/api/styles", body)
    assert code == 200, (code, data)
    return data


def _snapshot(api):
    code, data = api.get("/api/styles")
    assert code == 200
    return json.dumps(data["styles"], sort_keys=True, ensure_ascii=False)


# ── GET 契约 ────────────────────────────────────────────────────

def test_列表接口_返回三个原有顶层键(api):
    code, data = api.get("/api/styles")
    assert code == 200
    assert {"active", "active_by_bot", "styles"} <= set(data)


def test_列表接口_下发模型采样器与uc预设清单(api):
    _, data = api.get("/api/styles")
    assert {m["value"] for m in data["models"]} == set(MODELS) | {""}
    assert all(isinstance(m.get("label"), str) and m["label"].strip() for m in data["models"])
    assert set(data["samplers"]) == set(SAMPLERS)
    values = [p["value"] for p in data["uc_presets"]]
    assert all(isinstance(v, int) and not isinstance(v, bool) for v in values)
    assert set(values) == {0, 1, 2, 3}


# ── POST 合法路径 ───────────────────────────────────────────────

def test_合法参数被原样保存(api):
    entry = _mk(api, params={"model": MODELS[0], "steps": 28, "cfg_scale": 5.5,
                             "sampler": SAMPLERS[0], "ucPreset": 2})
    assert entry["params"] == {"model": MODELS[0], "steps": 28, "cfg_scale": 5.5,
                               "sampler": SAMPLERS[0], "ucPreset": 2}
    assert set(entry["params"]) <= ALLOWED_PARAM_KEYS


@pytest.mark.parametrize("blank", [None, ""])
def test_留空的参数被剔除而不是写进去(api, blank):
    entry = _mk(api, params={"steps": blank, "cfg_scale": 5})
    assert "steps" not in entry["params"]
    assert entry["params"]["cfg_scale"] == 5


def test_编辑时保留示例图时间戳(api, styles_root):
    entry = _mk(api, params={"steps": 20})
    data = json.loads(styles_root.read_text(encoding="utf-8"))
    data["styles"][0]["sample_image"] = "/tmp/x.png"
    data["styles"][0]["sample_ts"] = 1234567890
    styles_root.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    code, updated = api.post("/api/styles", {"id": entry["id"], "name": "改名后"})
    assert code == 200
    assert updated["sample_ts"] == 1234567890, "sample_ts 丢了前端缩略图刷新就失效"
    assert updated["sample_image"] == "/tmp/x.png"


# ── POST 非法路径：一律 400 且零副作用 ──────────────────────────

@pytest.mark.parametrize("zero", [0, 0.0, False, "0"])
def test_陷阱_cfg_scale为0必须报错而不是当成未设置(api, zero):
    code, data = api.post("/api/styles", {"name": "x", "params": {"cfg_scale": zero}})
    assert code == 400, "cfg_scale=%r 被放行了" % (zero,)
    assert data["error"] == E_CFG


def test_对照_ucPreset的0是合法值(api):
    entry = _mk(api, params={"ucPreset": 0})
    assert entry["params"]["ucPreset"] == 0


@pytest.mark.parametrize("params,expect", [
    ({"steps": 0}, E_STEPS),
    ({"steps": 51}, E_STEPS),
    ({"steps": 3.5}, E_STEPS),
    ({"steps": True}, E_STEPS),
    ({"cfg_scale": 11}, E_CFG),
    ({"ucPreset": 4}, E_UC),
    ({"ucPreset": True}, E_UC),
])
def test_越界参数返回400与具体原因(api, params, expect):
    code, data = api.post("/api/styles", {"name": "x", "params": params})
    assert code == 400
    assert data["error"] == expect


def test_表外模型与采样器被拒(api):
    code, data = api.post("/api/styles", {"name": "x", "params": {"model": "sdxl-turbo"}})
    assert code == 400 and data["error"].startswith("model 必须是")
    code, data = api.post("/api/styles", {"name": "x", "params": {"sampler": "k_nonexistent"}})
    assert code == 400 and data["error"].startswith("sampler 必须是")


def test_表外字段被拒且原因带字段名(api):
    code, data = api.post("/api/styles", {"name": "x", "params": {"scale": 1}})
    assert code == 400
    assert "scale" in data["error"]


@pytest.mark.parametrize("params", [[], "steps=20", 3])
def test_params不是对象时返回400(api, params):
    code, data = api.post("/api/styles", {"name": "x", "params": params})
    assert code == 400
    assert data["error"] == "params 必须是 JSON 对象"


def test_请求体不是JSON对象时返回400(api, client):
    r = client.post("/api/styles", data="not json", content_type="text/plain")
    assert r.status_code == 400
    assert r.get_json()["error"] == "请求体必须是 JSON 对象"


def test_缺名称返回400(api):
    code, data = api.post("/api/styles", {"params": {"steps": 20}})
    assert code == 400 and data["error"] == "name required"


def test_超长文本被拒(api):
    code, data = api.post("/api/styles", {"name": "x" * 201})
    assert code == 400 and "200" in data["error"]
    code, data = api.post("/api/styles", {"name": "x", "positive_prefix": "y" * 2001})
    assert code == 400 and "positive_prefix" in data["error"]


def test_被拒的请求对styles_json零副作用(api):
    _mk(api, params={"cfg_scale": 5})
    before = _snapshot(api)
    for bad in ({"cfg_scale": 0}, {"steps": 99}, {"bogus": 1}, "not-a-dict"):
        code, _ = api.post("/api/styles", {"name": "脏数据", "params": bad})
        assert code == 400
    assert _snapshot(api) == before, "400 请求改动了 styles.json"


def test_改已有预设失败时原值不被改掉(api):
    entry = _mk(api, params={"cfg_scale": 5})
    code, _ = api.post("/api/styles", {"id": entry["id"], "name": entry["name"],
                                       "params": {"cfg_scale": 0}})
    assert code == 400
    _, snap = api.get("/api/styles")
    assert snap["styles"][0]["params"]["cfg_scale"] == 5


# ── 删除 / 绑定 ─────────────────────────────────────────────────

def test_删除清理悬空绑定(api, styles_root):
    entry = _mk(api)
    data = json.loads(styles_root.read_text(encoding="utf-8"))
    data["active"] = entry["id"]
    data["active_by_bot"] = {"somebot": entry["id"]}
    styles_root.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    code, resp = api.delete("/api/styles/%s" % entry["id"])
    assert code == 200
    assert resp["active"] == ""
    assert resp["active_by_bot"] == {}, "被删预设的 per-bot 绑定必须一起清掉"


def test_删除不存在的预设返回404(api):
    code, data = api.delete("/api/styles/nope")
    assert code == 404 and data["error"] == "not found"


def test_设为当前时未知bot返回400且不写盘(api):
    entry = _mk(api)
    before = _snapshot(api)
    code, data = api.post("/api/styles/active", {"id": entry["id"], "bot": "不存在的bot"})
    assert code == 400 and "unknown bot" in data["error"]
    assert _snapshot(api) == before


def test_设为当前时预设不存在返回404(api):
    code, data = api.post("/api/styles/active", {"id": "style_nope"})
    assert code == 404 and data["error"] == "style not found"


# ── 并发保护 ────────────────────────────────────────────────────

def test_同一预设重复生成示例图返回409(api, client, monkeypatch):
    """第一次生成还在跑时，第二次点击必须被挡在门口，而不是两个进程写同一张图。

    第二次请求在另一个线程里发（真实场景就是另一个窗口/另一个请求），第一次卡在
    _run_sample 里不返回，正好复现"生成中"这个窗口期。
    """
    import threading

    from moments import styles_routes

    entry = _mk(api)
    entered, release, second = threading.Event(), threading.Event(), {}

    def fake_run(style_id):
        entered.set()
        release.wait(timeout=5)
        return {"ok": True}

    def hit_again():
        entered.wait(timeout=5)
        r = client.post("/api/styles/%s/sample" % entry["id"])
        second["code"], second["data"] = r.status_code, r.get_json()
        release.set()

    monkeypatch.setattr(styles_routes, "_run_sample", fake_run)
    t = threading.Thread(target=hit_again)
    t.start()
    api.post("/api/styles/%s/sample" % entry["id"])
    t.join(timeout=5)

    assert second["code"] == 409
    assert second["data"]["error"] == "示例图生成中"
    assert entry["id"] not in styles_routes._SAMPLING_IDS, "生成结束必须摘掉标记"


def test_给不存在的预设生成示例图返回404(api):
    code, data = api.post("/api/styles/nope/sample")
    assert code == 404 and data["error"] == "style not found"


def test_错误响应不回显任何令牌(api):
    from moments.styles_routes import _scrub_secrets

    dirty = "curl -H 'Authorization: Bearer pst-REALTOKEN123456' https://image.novelai.net"
    clean = _scrub_secrets(dirty)
    assert "pst-" not in clean and "REALTOKEN" not in clean
