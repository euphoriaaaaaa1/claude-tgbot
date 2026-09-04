# -*- coding: utf-8 -*-
"""`moments/hub_config.py` 纯函数层单测（不起 HTTP，验收那层管接口形状）。

盯的是**验收用例看不见的地方**：schema 漂移 assert 会不会真的炸、行级编辑器的字节保真、
占位符四条边界、备份同秒冲突与轮转时机、两把锁。
"""
import os

import pytest
import yaml
from flask import Flask

from moments import hub_config as hc
from moments.provider_model import HubError

SRC = """\
# 头注释
user_display_name: 哥哥
max_calls_per_5h: 100
webhook_secret: s3cr3t

_defaults: &base
  retry: 3

moments:
  enabled: true
  silence_threshold_minutes: 30   # 行尾注释
jiwen:
  delta_llm:
    <<: *base
    api_key: real-key-value
  prompt: |
    块标量
  thresholds:
    notice: 0.20
  rates:
    immersion_map:
      reading: 0.6
"""


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """把配置根钉在 tmp，并写好样本 `_global.yml`。"""
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(tmp_path))
    p = tmp_path / hc.GLOBAL_YML
    p.write_text(SRC, encoding="utf-8")
    return p


# ---- schema：三个方向的漂移都要在导入期炸 -------------------------------

def test_schema_engine新增字段而文案没跟上_直接assert失败(monkeypatch):
    meta = dict(hc._RATE_META)
    meta.pop("connection_per_min")
    monkeypatch.setattr(hc, "_RATE_META", meta)
    with pytest.raises(AssertionError, match="FIELD_META 没有文案"):
        hc._build_schema()


def test_schema_文案里的字段engine已删_直接assert失败(monkeypatch):
    monkeypatch.setattr(hc, "_RATE_META", dict(hc._RATE_META, ghost_knob=(0.0, 1.0, "幽灵", "")))
    with pytest.raises(AssertionError, match="已删/改名"):
        hc._build_schema()


def test_schema_默认值越出手写范围_直接assert失败(monkeypatch):
    """R7：valence_mild_multiplier 默认 1.5，划进 [0,1] 就是这条抓的。"""
    meta = dict(hc._RATE_META)
    meta["valence_mild_multiplier"] = (0.0, 1.0, "轻度低落倍率", "")
    monkeypatch.setattr(hc, "_RATE_META", meta)
    with pytest.raises(AssertionError, match="落在 FIELD_META 给的范围之外"):
        hc._build_schema()


def test_schema_三档之外的thresholds字段不进表单():
    for k in ("pride_block", "valence_activity", "arousal_agitation", "immersion_block"):
        assert "jiwen.thresholds." + k not in hc.FIELD_META


# ---- 行级编辑器：改一行，其余逐字节原样 ---------------------------------

def _diff_lines(a, b):
    la, lb = a.splitlines(True), b.splitlines(True)
    return [i for i in range(max(len(la), len(lb)))
            if (la[i] if i < len(la) else None) != (lb[i] if i < len(lb) else None)]


def test_行级编辑_只动目标那一行且行尾注释还在():
    out = hc.set_scalar(SRC, hc.compose(SRC), "moments.silence_threshold_minutes", 45, "int")
    assert _diff_lines(SRC, out) == [SRC.splitlines().index("  silence_threshold_minutes: 30   # 行尾注释")]
    assert "# 行尾注释" in out
    assert yaml.safe_load(out)["moments"]["silence_threshold_minutes"] == 45


def test_行级编辑_锚点别名mergekey块标量与数值书写格式全保真():
    out = hc.set_scalar(SRC, hc.compose(SRC), "jiwen.rates.immersion_map.reading", 0.55, "float")
    for token in ("&base", "<<: *base", "prompt: |", "notice: 0.20", "# 头注释"):
        assert token in out, token


def test_行级编辑_叶子不在但父在_按父缩进插一行():
    src = SRC.replace("      reading: 0.6\n", "      cooking: 0.5\n")
    out = hc.set_scalar(src, hc.compose(src), "jiwen.rates.immersion_map.reading", 0.7, "float")
    assert "      reading: 0.7\n" in out
    assert yaml.safe_load(out)["jiwen"]["rates"]["immersion_map"] == {"reading": 0.7, "cooking": 0.5}


def test_行级编辑_父映射也不在_409_key_absent():
    src = SRC.replace("    immersion_map:\n      reading: 0.6\n", "")
    with pytest.raises(HubError) as e:
        hc.set_scalar(src, hc.compose(src), "jiwen.rates.immersion_map.reading", 0.7, "float")
    assert e.value.error == "key_absent" and e.value.status == 409


def test_行级编辑_float给整数写成浮点_int不写成浮点():
    assert hc._literal(5, "float") == "5.0"
    assert hc._literal(5, "int") == "5"
    assert hc._literal(False, "bool") == "false"


def test_遍历_跳过mergekey不重复报锚点里的键():
    paths = [p for p, _, _ in hc.scalars(hc.compose(SRC))]
    assert "_defaults.retry" in paths
    assert not [p for p in paths if "<<" in p], "merge key 被跟进去了，同一段字节会被改两次"


# ---- 占位符：D2 的四条边界 ----------------------------------------------

def test_遮蔽_两个敏感标量都被换掉且不改行数(cfg):
    masked, paths = hc.mask_secrets(SRC, hc.compose(SRC))
    assert set(paths) == {"webhook_secret", "jiwen.delta_llm.api_key"}
    assert "real-key-value" not in masked and "s3cr3t" not in masked
    assert masked.count("\n") == SRC.count("\n")


def test_回填_原样回写后与磁盘原文逐字节相同(cfg):
    masked, _ = hc.mask_secrets(SRC, hc.compose(SRC))
    back, unresolved = hc.resolve_placeholders(masked, hc.compose(masked))
    assert unresolved == []
    assert back == SRC, "往返不是字节级相同"


def test_回填_敏感键被改名_解不掉且原文不动(cfg):
    masked, _ = hc.mask_secrets(SRC, hc.compose(SRC))
    renamed = masked.replace("api_key: ", "api_key_new: ")
    out, unresolved = hc.resolve_placeholders(renamed, hc.compose(renamed))
    assert unresolved == ["jiwen.delta_llm.api_key_new"]
    assert out == renamed, "解不掉时不许做任何替换"


def test_回填_非敏感键上的占位符按字面量处理(cfg):
    txt = SRC.replace("user_display_name: 哥哥\n",
                      "user_display_name: 哥哥\nnote: %s\n" % hc.PLACEHOLDER)
    out, unresolved = hc.resolve_placeholders(txt, hc.compose(txt))
    assert unresolved == [] and yaml.safe_load(out)["note"] == hc.PLACEHOLDER


def test_回填_敏感键整块被删_没有要解的东西(cfg):
    masked, _ = hc.mask_secrets(SRC, hc.compose(SRC))
    cut = masked.replace("    api_key: %s\n" % hc.PLACEHOLDER, "")
    out, unresolved = hc.resolve_placeholders(cut, hc.compose(cut))
    assert unresolved == [], "没有占位符残留就没有要解的东西"
    assert "api_key" not in out                      # 删掉的就是删掉了
    assert "webhook_secret: s3cr3t" in out           # 另一个占位符照常回填


def test_解析错误的detail只给行号不回显原文():
    bad = "user_display_name: 哥哥\nsecret_marker: 1\njiwen: [oops\n  x: : :\n"
    try:
        hc.compose(bad)
        raise AssertionError("这段该解析失败")
    except yaml.YAMLError as e:
        detail = hc.yaml_error_detail(e, "YAML 解析失败")
    assert any(ch.isdigit() for ch in detail)
    assert "secret_marker" not in detail and "oops" not in detail


# ---- 备份：同秒冲突、权限、轮转时机 -------------------------------------

def test_备份_同秒连开多份不覆盖且都是600(cfg, tmp_path):
    ids = [hc.make_backup("save", "第 %d 份\n" % i) for i in range(3)]
    assert len(set(ids)) == 3, "同秒冲突把前一份覆盖了：%s" % ids
    for i, bid in enumerate(ids):
        p = tmp_path / (hc.SAVE_PREFIX + bid)
        assert p.read_text(encoding="utf-8") == "第 %d 份\n" % i
        assert oct(os.stat(p).st_mode)[-3:] == "600"


def test_轮转_只留5份save且删的是最老的(cfg, tmp_path):
    ids = [hc.make_backup("save", "x") for _ in range(6)]
    hc.rotate_saves()
    left = [b["id"] for b in hc.list_backups() if b["kind"] == "save"]
    assert len(left) == 5 and ids[0] not in left
    assert not (tmp_path / (hc.SAVE_PREFIX + ids[0])).exists()


def test_轮转_restore类不挤占save的配额(cfg, tmp_path):
    keep = hc.make_backup("save", "要保住的那份")
    for _ in range(6):
        hc.make_backup("restore", "x")
        hc.rotate_saves()
    listing = hc.list_backups()
    assert keep in [b["id"] for b in listing if b["kind"] == "save"]
    assert len([b for b in listing if b["kind"] == "restore"]) == 6


def test_写盘失败时不动备份集合(cfg, tmp_path):
    """R3-1：轮转排在写盘成功之后 —— 写失败却先删了最老的备份，用户净亏一份历史。"""
    for _ in range(6):
        hc.make_backup("save", "x")
    before = sorted(p.name for p in tmp_path.iterdir())
    tmp_path.chmod(0o500)
    try:
        with pytest.raises(HubError) as e:
            hc.commit("user_display_name: x\n")
    finally:
        tmp_path.chmod(0o700)
    assert e.value.error == "config_write_failed" and e.value.status == 500
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_备份列表_新到旧且不含内容(cfg):
    for _ in range(3):
        hc.make_backup("save", "user_display_name: 哥哥\n")
    items = hc.list_backups()
    assert [b["id"] for b in items] == sorted([b["id"] for b in items], reverse=True)
    assert all(set(b) == {"id", "kind", "size", "ts"} for b in items)


def test_备份列表_目录不存在时是空列表不是异常(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(tmp_path / "nope"))
    assert hc.list_backups() == []


@pytest.mark.parametrize("bad", ["..", "%2e%2e", "_global.yml", "2020-01-01",
                                 "20200101-000000/../x", "", None])
def test_备份id_形状不对一律解析不出路径(bad):
    """`id` 会拼进文件名，是信任边界：只认 `\\d{8}-\\d{6}(-\\d{2})?` 一个形状。"""
    assert hc.backup_path(bad) is None


def test_备份id_合法形状落在配置目录下(cfg, tmp_path):
    assert hc.backup_path("20200101-000000") == str(tmp_path / (hc.SAVE_PREFIX + "20200101-000000"))


# ---- 两把锁 + 校验 ------------------------------------------------------

def test_进程内锁_抢不到就409_config_busy():
    app = Flask(__name__)
    with app.app_context():
        with hc.hold_config_lock():
            with pytest.raises(HubError) as e:
                with hc.hold_config_lock():
                    pass
        assert e.value.error == "config_busy" and e.value.status == 409
        with hc.hold_config_lock():         # 上一次已释放，还能再抢到
            pass


def test_版本乐观锁_对不上就409_config_stale且此前无副作用(cfg, tmp_path):
    with pytest.raises(HubError) as e:
        hc.check_version("1-1")
    assert e.value.error == "config_stale" and e.value.status == 409
    assert list(tmp_path.glob(hc.SAVE_PREFIX + "*")) == []
    hc.check_version(hc.version_of())       # 现值就该过


def test_版本_文件不存在时是absent(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(tmp_path))
    assert hc.version_of() == "absent"


@pytest.mark.parametrize("key,good,bad", [
    ("jiwen.rates.connection_per_min", 0.0, -0.1),
    ("jiwen.rates.valence_mild_multiplier", 5.0, 5.1),
    ("jiwen.rates.valence_severe_low", -1.0, -1.1),
    ("jiwen.tick_interval_min", 60, 61),
    ("moments.daily_post_limit_per_bot", 0, -1),
    ("max_calls_per_5h", 1000, 1001),
])
def test_校验_边界值本身合法越一点就bad_field(key, good, bad):
    hc.validate_values({key: good})
    with pytest.raises(HubError) as e:
        hc.validate_values({key: bad})
    assert e.value.error == "bad_field" and e.value.status == 400
    assert key.split(".")[-1] in e.value.detail
    assert str(hc.FIELD_META[key]["min"]) in e.value.detail


@pytest.mark.parametrize("values", [
    {"moments.enabled": 1}, {"moments.enabled": "true"}, {"moments.enabled": None},
    {"jiwen.tick_interval_min": 5.0}, {"jiwen.tick_interval_min": True},
    {"jiwen.rates.connection_per_min": "0.5"},
    {"jiwen.rates.connection_per_min": float("nan")},
    {"jiwen.rates.connection_per_min": float("inf")},
    {"jiwen.thresholds.pride_block": 0.4},      # §2.2：不进表单的字段无权改
    {"no.such.knob": 1},
])
def test_校验_类型与未知键一律bad_field(values):
    with pytest.raises(HubError) as e:
        hc.validate_values(values)
    assert e.value.error == "bad_field"


def test_算改动_同值不算改动缺键算改动(cfg):
    data, text = hc.load_global()
    changed, out = hc.apply_form({"moments.enabled": True}, data, text)
    assert changed == [] and out == text
    changed, out = hc.apply_form({"jiwen.tick_interval_min": 9}, data, text)
    assert changed == ["jiwen.tick_interval_min"], "yml 里没有这个键，该算改动"
    assert yaml.safe_load(out)["jiwen"]["tick_interval_min"] == 9


def test_生效提示_逐键求值后按位或(cfg):
    assert hc.restart_hint(["jiwen.tick_interval_min"]) == {"bot": True, "moments_web": False}
    assert hc.restart_hint(["max_calls_per_5h"]) == {"bot": True, "moments_web": False}
    assert hc.restart_hint(["moments.enabled"]) == {"bot": False, "moments_web": True}
    assert hc.restart_hint(["moments.enabled", "jiwen.x"]) == {"bot": True, "moments_web": True}
    assert hc.restart_hint([]) == {"bot": False, "moments_web": False}


def test_关键顶层键缺失能被点名():
    root = hc.compose("moments: {}\njiwen: {}\n")
    assert hc.missing_top_keys(root) == ["user_display_name", "max_calls_per_5h"]
