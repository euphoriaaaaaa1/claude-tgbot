# -*- coding: utf-8 -*-
"""§3.1 / §3.2 高级模式（原文读写）的契约与固定校验顺序。"""
import pytest

import hub2util as u
import spec

PH = spec.PLACEHOLDER


def _raw(api):
    return api.get("/hub/api/params/raw")


def _post_raw(api, text, version):
    return api.post("/hub/api/params/raw", {"text": text, "version": version})


def test_读原文_敏感值被换成占位符且不回显明文(api, global_yml, capfd):
    code, body = _raw(api)
    assert code == 200, body
    assert body["placeholder"] == PH
    assert f"api_key: {PH}" in body["text"]
    out, err = capfd.readouterr()
    assert spec.SECRET_KEY not in body["text"]
    assert spec.SECRET_KEY not in u.flat_text(body)
    assert spec.SECRET_KEY not in out and spec.SECRET_KEY not in err


def test_读原文_每个敏感标量都被遮蔽(api, global_yml):
    """敏感判定 = 键名匹配 key|token|secret|password|credential|auth（§3.1）。"""
    _, body = _raw(api)
    assert "s3cr3t-do-not-echo-me" not in body["text"], "webhook_secret 没被遮蔽"
    assert set(body["sensitive_paths"]) == {"jiwen.delta_llm.api_key", "webhook_secret"}


def test_读原文_只换敏感那几行其余字节原样(api, global_yml):
    """F2 行级替换：diff 出来的行数必须等于敏感标量个数。"""
    _, body = _raw(api)
    diff = u.changed_lines(global_yml.read_text(encoding="utf-8"), body["text"])
    assert len(diff) == 2, f"遮蔽动了不该动的行：{diff}"
    assert all("api_key" in d[1] or "webhook_secret" in d[1] for d in diff)


def test_读原文_注释锚点块标量全保真(api, global_yml):
    _, body = _raw(api)
    for token in ("# ===== claudebotlife 全局配置 =====", "&base", "<<: *base",
                  "prompt: |", "notice: 0.20"):
        assert token in body["text"], f"遮蔽把 {token} 弄没了"


def test_读原文_version与磁盘一致(api, global_yml):
    _, body = _raw(api)
    assert body["version"] == u.version_of(global_yml)


def test_读原文_文件不存在_404_not_found(api, configs_dir, global_yml):
    assert _raw(api)[0] == 200, "端点没活着，下面的 404 会空过"
    (configs_dir / "_global.yml").unlink(missing_ok=True)
    code, body = _raw(api)
    assert code == 404
    assert body["error"] == "not_found"


def test_读原文_磁盘坏yaml_409且给行号且不回显原文(api, global_yml):
    """§3.1：坏 YAML 无法定位敏感路径，吐原文就等于吐明文 key。"""
    global_yml.write_text("user_display_name: 哥哥\napi_key: " + spec.SECRET_KEY
                          + "\njiwen: [oops\n  x: : :\n", encoding="utf-8")
    code, body = _raw(api)
    assert code == 409, body
    assert body["error"] == "global_yml_unreadable"
    assert any(ch.isdigit() for ch in body["detail"]), "detail 要给解析错误行号"
    assert spec.SECRET_KEY not in u.flat_text(body)
    assert "text" not in body, "坏 YAML 时不许回显原文"


# ---- §3.2 POST /hub/api/params/raw：校验顺序表逐行 ----------------------

def test_写原文_成功_200带备份新版本与生效提示(api, global_yml):
    _, raw = _raw(api)
    new = raw["text"].replace("max_calls_per_5h: 100", "max_calls_per_5h: 120")
    code, body = _post_raw(api, new, raw["version"])
    assert code == 200, body
    assert body["ok"] is True
    assert body["backup"]
    assert body["version"] == u.version_of(global_yml)
    assert body["restart_hint"] == {"bot": True, "moments_web": True}, \
        "§5：高级模式无法逐键归因时两个都要 true"
    assert u.load(global_yml)["max_calls_per_5h"] == 120


@pytest.mark.parametrize("payload,why", [
    pytest.param([1, 2], "顶层是数组", id="顶层是数组"),
    pytest.param({"text": "a: 1"}, "缺 version", id="缺version"),
    pytest.param({"version": "1-1"}, "缺 text", id="缺text"),
    pytest.param({"text": 123, "version": "1-1"}, "text 非字符串", id="text非字符串"),
    pytest.param({"text": "a: 1", "version": 5}, "version 非字符串", id="version非字符串"),
])
def test_写原文_body形状不对_400_bad_body(api, global_yml, payload, why):
    code, body = api.post("/hub/api/params/raw", payload)
    assert code == 400, (why, body)
    assert body["error"] == "bad_body"


def test_写原文_超过1MiB_413_payload_too_large(api, global_yml):
    huge = "# " + "a" * (1024 * 1024 + 32) + "\nuser_display_name: x\n"
    code, body = _post_raw(api, huge, u.version_of(global_yml))
    assert code == 413, body
    assert body["error"] == "payload_too_large"


def test_写原文_超大且内容也坏_先报413(api, global_yml):
    """校验顺序：检查 2（大小）排在检查 3（YAML 解析）之前。"""
    code, body = _post_raw(api, "[oops\n" + "b" * (1024 * 1024 + 32),
                           u.version_of(global_yml))
    assert code == 413, body
    assert body["error"] == "payload_too_large"


def test_写原文_坏yaml_400_bad_yaml且不写盘不备份不回显原文(api, global_yml, configs_dir):
    before = global_yml.read_bytes()
    bad = "user_display_name: 哥哥\nsecret_marker_in_text: 12345\njiwen: [oops\n  x: : :\n"
    code, body = _post_raw(api, bad, u.version_of(global_yml))
    assert code == 400, body
    assert body["error"] == "bad_yaml"
    assert any(ch.isdigit() for ch in body["detail"]), "detail 要含行号"
    assert "secret_marker_in_text" not in u.flat_text(body), "不许回显用户提交的原文"
    assert global_yml.read_bytes() == before
    assert list(configs_dir.glob("_global.yml.bak.*")) == []


def test_写原文_顶层不是映射_400_bad_yaml(api, global_yml):
    code, body = _post_raw(api, "- a\n- b\n", u.version_of(global_yml))
    assert code == 400, body
    assert body["error"] == "bad_yaml"


@pytest.mark.parametrize("missing", spec.REQUIRED_TOP_KEYS)
def test_写原文_缺关键顶层键_400_schema_missing_key(api, global_yml, missing):
    _, raw = _raw(api)
    lines = raw["text"].splitlines(True)
    out, skipping = [], False
    for line in lines:
        if line.startswith(missing + ":"):
            skipping = True
            continue
        if skipping and (line.startswith(" ") or line.strip() == ""):
            continue
        skipping = False
        out.append(line)
    code, body = _post_raw(api, "".join(out), raw["version"])
    assert code == 400, body
    assert body["error"] == "schema_missing_key"
    assert missing in body["detail"]


def test_写原文_坏yaml优先于缺顶层键(api, global_yml):
    """检查 3 排在检查 5 之前。"""
    code, body = _post_raw(api, "jiwen: [oops\n  x: : :\n", u.version_of(global_yml))
    assert code == 400
    assert body["error"] == "bad_yaml"


def test_写原文_版本过期_409_config_stale且不写盘不备份(api, global_yml, configs_dir):
    _, raw = _raw(api)
    before = global_yml.read_bytes()
    code, body = _post_raw(api, raw["text"], "1-1")
    assert code == 409, body
    assert body["error"] == "config_stale"
    assert global_yml.read_bytes() == before
    assert list(configs_dir.glob("_global.yml.bak.*")) == []


def test_写原文_占位符解不掉_优先于版本过期(api, global_yml):
    """校验顺序：检查 6（占位符）排在检查 7（版本）之前。"""
    _, raw = _raw(api)
    txt = raw["text"].replace("api_key: ", "api_key_new: ")
    code, body = _post_raw(api, txt, "1-1")
    assert code == 400, body
    assert body["error"] == "placeholder_unresolved"


def test_写原文_缺顶层键优先于占位符解不掉(api, global_yml):
    """校验顺序：检查 5 排在检查 6 之前。"""
    _, raw = _raw(api)
    txt = raw["text"].replace("api_key: ", "api_key_new: ")
    txt = txt.replace("user_display_name: 哥哥\n", "")
    code, body = _post_raw(api, txt, raw["version"])
    assert code == 400, body
    assert body["error"] == "schema_missing_key"


def test_写原文_不改内容原样回写_也算一次成功写盘(api, global_yml):
    """K1 的接口面：原样回写是合法操作（内容一致，但仍走写盘顺序）。"""
    _, raw = _raw(api)
    code, body = _post_raw(api, raw["text"], raw["version"])
    assert code == 200, body
    assert body["ok"] is True
