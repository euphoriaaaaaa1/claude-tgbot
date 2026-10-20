# -*- coding: utf-8 -*-
"""§2.1 `GET /hub/api/params` + §2.2 键表契约。

§2.2 原话：「表单只放这些，逐键写死，测试直接照此断言」——本文件就是那张表的验收。
"""
import pytest

import hub2util as u
import spec


def _schema_map(body):
    return {s["key"]: s for s in body.get("schema", []) if isinstance(s, dict)}


def test_取参数_值与磁盘yml一致(api, global_yml):
    code, body = api.get("/hub/api/params")
    assert code == 200
    values = body["values"]
    for k, expected in spec.EXPECTED_VALUES.items():
        assert values[k] == expected, f"{k} 读出来是 {values.get(k)!r}"


def test_取参数_schema键集合与22表完全一致(api, global_yml):
    code, body = api.get("/hub/api/params")
    assert code == 200
    got = set(_schema_map(body))
    assert got == spec.KEY_SET, (f"多出：{sorted(got - spec.KEY_SET)}；"
                                f"缺少：{sorted(spec.KEY_SET - got)}")


@pytest.mark.parametrize("key,typ,lo,hi", spec.KEYS, ids=[k for k, _, _, _ in spec.KEYS])
def test_取参数_schema逐键的类型与范围照22表(api, global_yml, key, typ, lo, hi):
    _, body = api.get("/hub/api/params")
    item = _schema_map(body).get(key)
    assert item is not None, f"schema 里没有 {key}"
    assert item.get("type") == typ
    if typ == "bool":
        assert "min" not in item or item.get("min") is None
    else:
        assert item.get("min") == lo, f"{key} 下界应为 {lo}"
        assert item.get("max") == hi, f"{key} 上界应为 {hi}"


@pytest.mark.parametrize("key", spec.FORBIDDEN_IN_SCHEMA)
def test_取参数_不进表单的thresholds字段不许出现在schema(api, global_yml, key):
    """§2.2：它们出现在 schema 里会被视为契约违反。"""
    code, body = api.get("/hub/api/params")
    assert code == 200 and _schema_map(body), "端点没活着，这条断言会空过"
    assert key not in _schema_map(body)


def test_取参数_每个schema项都满足最小值不大于默认值不大于最大值(api, global_yml):
    """R7 的启动期 assert③ 的可观测投影（valence_mild_multiplier 默认 1.5 就是它抓的）。"""
    code, body = api.get("/hub/api/params")
    assert code == 200 and _schema_map(body), "端点没活着，这条断言会空过"
    bad = []
    for key, item in _schema_map(body).items():
        if item.get("type") == "bool":
            continue
        d, lo, hi = item.get("default"), item.get("min"), item.get("max")
        if not (lo <= d <= hi):
            bad.append((key, lo, d, hi))
    assert bad == [], f"默认值落在范围之外：{bad}"


def test_取参数_每个schema项都带标签(api, global_yml):
    """B1：schema 只含 FIELD_META 字段 —— 没标签的浮点旋钮不许交给用户。"""
    code, body = api.get("/hub/api/params")
    assert code == 200 and _schema_map(body), "端点没活着，这条断言会空过"
    missing = [k for k, it in _schema_map(body).items() if not it.get("label")]
    assert missing == []


def test_取参数_version等于文件的mtime纳秒加字节数(api, global_yml):
    _, body = api.get("/hub/api/params")
    assert body["version"] == u.version_of(global_yml)


def test_取参数_文件齐全时可用高级模式且无告警(api, global_yml):
    _, body = api.get("/hub/api/params")
    assert body["advanced_available"] is True
    assert body["warnings"] == []


def test_取参数_yml缺某个键时回落默认值并给missing告警(api, global_yml):
    """§2.1：首次安装就是这个态，不是错误。"""
    txt = global_yml.read_text(encoding="utf-8")
    global_yml.write_text(
        txt.replace("  silence_threshold_minutes: 30   # 静默多久算冷场\n", ""),
        encoding="utf-8")
    code, body = api.get("/hub/api/params")
    assert code == 200
    assert "missing_in_yml:moments.silence_threshold_minutes" in body["warnings"]
    item = _schema_map(body)["moments.silence_threshold_minutes"]
    assert body["values"]["moments.silence_threshold_minutes"] == item["default"]


def test_取参数_文件不存在时仍200且标记不可用高级模式(api, configs_dir):
    (configs_dir / "_global.yml").unlink(missing_ok=True)
    code, body = api.get("/hub/api/params")
    assert code == 200
    assert body["advanced_available"] is False
    assert body["version"] == "absent"
    assert "global_yml_unreadable" in body["warnings"]
    sm = _schema_map(body)
    assert body["values"] == {k: sm[k]["default"] for k in sm}


def test_取参数_坏yaml时仍200且高级模式不可用(api, global_yml):
    global_yml.write_text("jiwen: [unclosed\n  bad: : :\n", encoding="utf-8")
    code, body = api.get("/hub/api/params")
    assert code == 200
    assert body["advanced_available"] is False
    assert "global_yml_unreadable" in body["warnings"]


def test_取参数_响应里不出现任何密钥明文(api, global_yml, capfd):
    """S6：参数表单端点根本不该碰到 api_key / webhook_secret。"""
    code, body = api.get("/hub/api/params")
    assert code == 200, body
    blob = u.flat_text(body)
    out, err = capfd.readouterr()
    for secret in (spec.SECRET_KEY, "s3cr3t-do-not-echo-me"):
        assert secret not in blob, "响应体里出现了密钥明文"
        assert secret not in out and secret not in err, "stdout/stderr 里出现了密钥明文"


def test_取参数_空配置目录也不炸(api, configs_dir):
    for p in configs_dir.iterdir():
        p.unlink()
    code, body = api.get("/hub/api/params")
    assert code == 200
    assert set(_schema_map(body)) == spec.KEY_SET
