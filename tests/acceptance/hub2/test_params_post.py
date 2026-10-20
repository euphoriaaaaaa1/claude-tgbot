# -*- coding: utf-8 -*-
"""§2.3 `POST /hub/api/params` 的校验契约（逐行照 §2.3 的表断确切码）。"""
import pytest

import hub2util as u
import spec

CK = "jiwen.rates.connection_per_min"


def save(api, global_yml, values, version=None):
    payload = {"values": values}
    if version is not u.OMIT:
        payload["version"] = version if version is not None else u.version_of(global_yml)
    return api.post("/hub/api/params", payload)


def test_保存_改一个键_200并回changed与新版本(api, global_yml):
    code, body = save(api, global_yml, {CK: 0.0012})
    assert code == 200, body
    assert body["ok"] is True
    assert body["changed"] == [CK]
    assert body["version"] == u.version_of(global_yml)
    assert u.dig(u.load(global_yml), "jiwen.rates.connection_per_min") == 0.0012


def test_保存_提交与磁盘同值_不计入changed也不备份(api, global_yml):
    before = global_yml.stat().st_mtime_ns
    code, body = save(api, global_yml, {CK: spec.RATES["connection_per_min"]})
    assert code == 200, body
    assert body["changed"] == []
    assert body["backup"] is None
    assert body["restart_hint"] == {"bot": False, "moments_web": False}, \
        "§13 A8：空操作两者均为 false"
    assert global_yml.stat().st_mtime_ns == before, "全同值不该写盘"


def test_保存_空values_200空操作不写盘不备份(api, global_yml, configs_dir):
    before = global_yml.stat().st_mtime_ns
    code, body = save(api, global_yml, {})
    assert code == 200, body
    assert body["changed"] == []
    assert body["backup"] is None
    assert body["restart_hint"] == {"bot": False, "moments_web": False}, \
        "§13 A8：`changed: []` 时两者均为 false"
    assert global_yml.stat().st_mtime_ns == before
    assert list(configs_dir.glob("_global.yml.bak.*")) == []


def test_保存_部分更新_只改提交的那个键(api, global_yml):
    code, body = save(api, global_yml, {CK: 0.0012,
                                        "moments.daily_post_limit_per_bot": 5})
    assert code == 200, body
    assert body["changed"] == [CK], "同值的 moments 键不该进 changed"


def test_保存_未知键_400_bad_field且detail点名该键(api, global_yml):
    code, body = save(api, global_yml, {"jiwen.rates.no_such_knob": 0.5})
    assert code == 400
    assert body["error"] == "bad_field"
    assert "no_such_knob" in body["detail"]


@pytest.mark.parametrize("key", spec.FORBIDDEN_IN_SCHEMA)
def test_保存_不进表单的thresholds键_400_bad_field(api, global_yml, key):
    """文件里有这些键，但表单无权改它们（§2.2）。"""
    code, body = save(api, global_yml, {key: 0.4})
    assert code == 400
    assert body["error"] == "bad_field"


NUMERIC = [k for k in spec.KEYS if k[1] != "bool"]
NUM_IDS = [k[0] for k in NUMERIC]


@pytest.mark.parametrize("key,typ,lo,hi", NUMERIC, ids=NUM_IDS)
def test_保存_下界值本身合法(api, global_yml, key, typ, lo, hi):
    code, body = save(api, global_yml, {key: lo if typ == "int" else float(lo)})
    assert code == 200, body


@pytest.mark.parametrize("key,typ,lo,hi", NUMERIC, ids=NUM_IDS)
def test_保存_上界值本身合法(api, global_yml, key, typ, lo, hi):
    code, body = save(api, global_yml, {key: hi if typ == "int" else float(hi)})
    assert code == 200, body


@pytest.mark.parametrize("key,typ,lo,hi", NUMERIC, ids=NUM_IDS)
def test_保存_低于下界_400_bad_field且detail带键名与范围(api, global_yml, key, typ, lo, hi):
    bad = lo - 1 if typ == "int" else round(lo - 0.1, 6)
    code, body = save(api, global_yml, {key: bad})
    assert code == 400, body
    assert body["error"] == "bad_field"
    assert key.split(".")[-1] in body["detail"]
    assert str(lo) in body["detail"] or str(hi) in body["detail"], "detail 要给出允许范围"


@pytest.mark.parametrize("key,typ,lo,hi", NUMERIC, ids=NUM_IDS)
def test_保存_高于上界_400_bad_field(api, global_yml, key, typ, lo, hi):
    bad = hi + 1 if typ == "int" else round(hi + 0.1, 6)
    code, body = save(api, global_yml, {key: bad})
    assert code == 400, body
    assert body["error"] == "bad_field"


def test_保存_float键给字符串_400_bad_field且detail含期望类型(api, global_yml):
    code, body = save(api, global_yml, {CK: "0.5"})
    assert code == 400
    assert body["error"] == "bad_field"
    assert "connection_per_min" in body["detail"]
    assert "float" in body["detail"].lower()


def test_保存_bool键给1_400_bad_field(api, global_yml):
    """§2.3 类型规则写死：bool 只接受 JSON true/false，不接受 0/1。"""
    code, body = save(api, global_yml, {"moments.enabled": 1})
    assert code == 400
    assert body["error"] == "bad_field"


def test_保存_bool键给字符串true_400_bad_field(api, global_yml):
    code, body = save(api, global_yml, {"moments.enabled": "true"})
    assert code == 400
    assert body["error"] == "bad_field"


def test_保存_bool键给false_200(api, global_yml):
    code, body = save(api, global_yml, {"moments.enabled": False})
    assert code == 200, body
    assert body["changed"] == ["moments.enabled"]
    assert u.dig(u.load(global_yml), "moments.enabled") is False


def test_保存_int键给5点0_400_bad_field(api, global_yml):
    """§2.3：int 只接受 JSON 整数，不接受 5.0。"""
    code, body = save(api, global_yml, {"jiwen.tick_interval_min": 6.0})
    assert code == 400
    assert body["error"] == "bad_field"


def test_保存_float键给整数_合法并存成浮点(api, global_yml):
    """§2.3：float 接受 int 与 float（5 → 5.0 合法）。"""
    code, body = save(api, global_yml, {"jiwen.rates.accel_threshold_min": 5})
    assert code == 200, body
    assert float(u.dig(u.load(global_yml), "jiwen.rates.accel_threshold_min")) == 5.0


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_保存_NaN与无穷_400_bad_field(api, global_yml, literal):
    """§2.3 越界一行明写含 NaN/Infinity。这些值 json 库默认能塞进去，必须被挡。"""
    raw = ('{"version": "%s", "values": {"%s": %s}}'
           % (u.version_of(global_yml), CK, literal))
    code, body = api.post("/hub/api/params", data=raw,
                          content_type="application/json")
    assert code == 400, body
    assert body["error"] == "bad_field"


@pytest.mark.parametrize("payload", [
    pytest.param([1, 2], id="顶层是数组"),
    pytest.param("just a string", id="顶层是字符串"),
])
def test_保存_body不是对象_400_bad_body(api, global_yml, payload):
    code, body = api.post("/hub/api/params", payload)
    assert code == 400
    assert body["error"] == "bad_body"


def test_保存_非法JSON_400_bad_body(api, global_yml):
    code, body = api.post("/hub/api/params", data="{not json",
                          content_type="application/json")
    assert code == 400
    assert body["error"] == "bad_body"


def test_保存_ContentType不是json_400_bad_body(api, global_yml):
    code, body = api.post("/hub/api/params", data="version=x",
                          content_type="application/x-www-form-urlencoded")
    assert code == 400
    assert body["error"] == "bad_body"


def test_保存_空body_400_bad_body(api, global_yml):
    code, body = api.post("/hub/api/params", data="",
                          content_type="application/json")
    assert code == 400
    assert body["error"] == "bad_body"


def test_保存_缺values_400_bad_body(api, global_yml):
    code, body = api.post("/hub/api/params", {"version": u.version_of(global_yml)})
    assert code == 400
    assert body["error"] == "bad_body"


def test_保存_values不是对象_400_bad_body(api, global_yml):
    code, body = api.post("/hub/api/params",
                          {"version": u.version_of(global_yml), "values": [CK]})
    assert code == 400
    assert body["error"] == "bad_body"


def test_保存_缺version_400_bad_body(api, global_yml):
    code, body = save(api, global_yml, {CK: 0.5}, version=u.OMIT)
    assert code == 400
    assert body["error"] == "bad_body"


def test_保存_version不是字符串_400_bad_body(api, global_yml):
    code, body = api.post("/hub/api/params", {"version": 12345, "values": {CK: 0.5}})
    assert code == 400
    assert body["error"] == "bad_body"


def test_保存_磁盘坏yaml_409_global_yml_unreadable并指路高级模式(api, global_yml):
    global_yml.write_text("jiwen: [oops\n  x: : :\n", encoding="utf-8")
    code, body = save(api, global_yml, {CK: 0.5})
    assert code == 409, body
    assert body["error"] == "global_yml_unreadable"
    assert "高级模式" in body["detail"]


def test_保存_文件不存在_409_global_yml_unreadable(api, global_yml):
    global_yml.unlink()
    code, body = api.post("/hub/api/params", {"version": "absent", "values": {CK: 0.5}})
    assert code == 409, body
    assert body["error"] == "global_yml_unreadable"


def test_保存_版本过期_409_config_stale且磁盘未改未备份(api, global_yml, configs_dir):
    before = global_yml.read_bytes()
    code, body = save(api, global_yml, {CK: 0.5}, version="1-1")
    assert code == 409, body
    assert body["error"] == "config_stale"
    assert global_yml.read_bytes() == before
    assert list(configs_dir.glob("_global.yml.bak.*")) == []


def test_保存_同时有越界值和过期版本_先报400_bad_field(api, global_yml):
    """§2.3：config_stale 排在全部输入校验之后 —— 纯输入错优先于冲突错。"""
    code, body = save(api, global_yml, {CK: 99}, version="1-1")
    assert code == 400, body
    assert body["error"] == "bad_field"


def test_保存_同时缺values和过期版本_先报400_bad_body(api, global_yml):
    code, body = api.post("/hub/api/params", {"version": "1-1"})
    assert code == 400
    assert body["error"] == "bad_body"


def test_保存_同一version连用两次_第二次409_config_stale(api, global_yml):
    """幂等性反面：版本乐观锁必须挡住"拿旧快照重放"。"""
    v = u.version_of(global_yml)
    code1, _ = save(api, global_yml, {CK: 0.0011}, version=v)
    assert code1 == 200
    code2, body2 = save(api, global_yml, {CK: 0.0013}, version=v)
    assert code2 == 409, body2
    assert body2["error"] == "config_stale"
    assert u.dig(u.load(global_yml), "jiwen.rates.connection_per_min") == 0.0011


def test_保存_同值重复提交两次_都是200且都不写盘(api, global_yml):
    """幂等正面：空操作重复多少次都一样，且始终不动磁盘。"""
    same = {CK: spec.RATES["connection_per_min"]}
    before = global_yml.stat().st_mtime_ns
    for _ in range(2):
        code, body = save(api, global_yml, same)
        assert code == 200, body
        assert body["changed"] == []
    assert global_yml.stat().st_mtime_ns == before


def test_保存_目标键和父映射都不存在_409_key_absent(api, global_yml):
    """§2.4：行级编辑器不猜结构。"""
    txt = global_yml.read_text(encoding="utf-8")
    global_yml.write_text(txt.split("    immersion_map:\n")[0], encoding="utf-8")
    code, body = save(api, global_yml, {"jiwen.rates.immersion_map.reading": 0.5})
    assert code == 409, body
    assert body["error"] == "key_absent"
    assert "高级模式" in body["detail"]


def test_保存_叶子键不存在但父映射在_按父缩进补一行(api, global_yml):
    txt = global_yml.read_text(encoding="utf-8")
    global_yml.write_text(txt.replace("      reading: 0.6\n", ""), encoding="utf-8")
    code, body = save(api, global_yml, {"jiwen.rates.immersion_map.reading": 0.55})
    assert code == 200, body
    assert body["changed"] == ["jiwen.rates.immersion_map.reading"]
    assert u.dig(u.load(global_yml), "jiwen.rates.immersion_map.reading") == 0.55


def test_保存_响应与日志都不出现密钥明文(api, global_yml, capfd):
    code, body = save(api, global_yml, {CK: 0.0012})
    assert code == 200
    out, err = capfd.readouterr()
    for secret in (spec.SECRET_KEY, "s3cr3t-do-not-echo-me"):
        assert secret not in u.flat_text(body)
        assert secret not in out and secret not in err


def test_保存_写盘后api_key仍在磁盘上原样(api, global_yml):
    """反向：表单保存不许把敏感值弄丢或改写。"""
    assert save(api, global_yml, {CK: 0.0012})[0] == 200, "保存没成功，这条会空过"
    assert u.dig(u.load(global_yml), "jiwen.delta_llm.api_key") == spec.SECRET_KEY
