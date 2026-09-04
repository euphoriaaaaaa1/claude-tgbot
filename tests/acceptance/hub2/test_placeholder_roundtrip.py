# -*- coding: utf-8 -*-
"""§3.3 占位符往返契约 K1–K9（BRIEF S2 / S6 的核心）。用例名直接带 K 号。"""
import hub2util as u
import spec

PH = spec.PLACEHOLDER
DELTA_BLOCK = ("  delta_llm:\n"
               "    <<: *base               # merge key：K8\n"
               f"    api_key: {PH}\n"
               "    endpoint: https://api.deepseek.com/v1\n")


def _raw(api):
    code, body = api.get("/hub/api/params/raw")
    assert code == 200, body
    return body


def _post(api, text, version):
    return api.post("/hub/api/params/raw", {"text": text, "version": version})


def test_K1_原样回写_整个文件逐字节相同且响应不含key明文(api, global_yml):
    before = global_yml.read_bytes()
    raw = _raw(api)
    code, body = _post(api, raw["text"], raw["version"])
    assert code == 200, body
    assert global_yml.read_bytes() == before, "原样回写却改变了文件字节"
    assert spec.SECRET_KEY not in u.flat_text(body)


def test_K2_改无关键_key原值保留且无关键已改(api, global_yml):
    raw = _raw(api)
    txt = raw["text"].replace("max_calls_per_5h: 100", "max_calls_per_5h: 120")
    code, body = _post(api, txt, raw["version"])
    assert code == 200, body
    on_disk = u.load(global_yml)
    assert on_disk["max_calls_per_5h"] == 120
    assert u.dig(on_disk, "jiwen.delta_llm.api_key") == spec.SECRET_KEY


def test_K3_占位符换成真新值_磁盘上是新值(api, global_yml):
    raw = _raw(api)
    txt = raw["text"].replace(f"api_key: {PH}", "api_key: sk-newvalue-9f8e7d6c5b4a")
    code, body = _post(api, txt, raw["version"])
    assert code == 200, body
    assert u.dig(u.load(global_yml), "jiwen.delta_llm.api_key") == "sk-newvalue-9f8e7d6c5b4a"


def test_K4_整块删掉含占位符的映射_200且不报未解占位符(api, global_yml):
    raw = _raw(api)
    assert DELTA_BLOCK in raw["text"], "样本结构变了，K4 的删除锚点失效"
    code, body = _post(api, raw["text"].replace(DELTA_BLOCK, ""), raw["version"])
    assert code == 200, body
    assert "delta_llm" not in u.load(global_yml)["jiwen"]


def test_K5_把敏感键改名后占位符解不掉_400且磁盘未改(api, global_yml):
    before = global_yml.read_bytes()
    raw = _raw(api)
    txt = raw["text"].replace("api_key: ", "api_key_new: ")
    code, body = _post(api, txt, raw["version"])
    assert code == 400, body
    assert body["error"] == "placeholder_unresolved"
    assert "jiwen.delta_llm.api_key_new" in body["detail"]
    assert global_yml.read_bytes() == before


def test_K6_非敏感键上的字面占位符_原样存成字符串(api, global_yml):
    raw = _raw(api)
    txt = raw["text"].replace("user_display_name: 哥哥\n",
                              f"user_display_name: 哥哥\nnote_field: {PH}\n")
    code, body = _post(api, txt, raw["version"])
    assert code == 200, body
    assert u.load(global_yml)["note_field"] == PH


def test_K7_坏yaml_不写盘且一个备份都没产生(api, global_yml, configs_dir):
    before = global_yml.read_bytes()
    code, body = _post(api, "jiwen: [oops\n  x: : :\n", u.version_of(global_yml))
    assert code == 400, body
    assert body["error"] == "bad_yaml"
    assert global_yml.read_bytes() == before
    assert list(configs_dir.glob("_global.yml.bak.*")) == []
    assert list(configs_dir.glob("_global.yml.pre-restore.*")) == []
    assert api.get("/hub/api/params/raw")[0] == 200, "失败的写不该把文件搞坏"


def test_K8_锚点别名mergekey块标量往返后原样(api, global_yml):
    raw = _raw(api)
    code, body = _post(api, raw["text"], raw["version"])
    assert code == 200, body
    txt = global_yml.read_text(encoding="utf-8")
    for token in ("_defaults: &base", "<<: *base", "prompt: |",
                  "    这是块标量，K8 要求不被展开、不被重排。", "notice: 0.20"):
        assert token in txt, f"往返把 {token} 弄没了"


def test_K9_过期版本_409且一个备份都没产生(api, global_yml, configs_dir):
    before = global_yml.read_bytes()
    raw = _raw(api)
    code, body = _post(api, raw["text"].replace("max_calls_per_5h: 100",
                                                "max_calls_per_5h: 120"), "1-1")
    assert code == 409, body
    assert body["error"] == "config_stale"
    assert global_yml.read_bytes() == before
    assert list(configs_dir.glob("_global.yml.bak.*")) == []
