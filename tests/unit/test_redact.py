# -*- coding: utf-8 -*-
"""M0 单测：脱敏三件套（掩码 / 白名单投影 / 出站正则兜底）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from moments import redact as R  # noqa: E402


def test_掩码是四星加末四位():
    assert R.mask("sk-test-abcdefghijkl9x7Q") == "****9x7Q"
    assert len(R.mask("x" * 200)) == 8
    assert R.mask("ab") == "****"       # 太短就整个掩掉，不留末位
    assert R.mask("") == "" and R.mask(None) == ""


def test_凭据字段名匹配含连字符与大小写变体():
    for name in ("api-key", "API_KEY", "secret-key", "Authorization", "access_token",
                 "password", "credential"):
        assert R.is_secret_field(name), name
    for name in ("name", "base-url", "weight", "models", "prefix"):
        assert not R.is_secret_field(name), name


def test_白名单投影_丢弃未列入字段并递归models():
    blk = {"name": "p1", "base-url": "https://x/v1", "api-key": "PLAINTEXT-KEY-1234",
           "secret-key": "s3cr3t", "internal-note": "内部字段",
           "models": [{"name": "m", "alias": "a", "junk": 1}]}
    out = R.project(blk)
    assert out == {"name": "p1", "base-url": "https://x/v1",
                   "models": [{"name": "m", "alias": "a"}]}
    assert "PLAINTEXT-KEY-1234" not in str(out) and "内部字段" not in str(out)


def test_白名单投影_上游形状变了也不把原对象带出去():
    assert R.project(["not", "a", "dict"]) == {}
    assert R.project({"models": "不是列表"}) == {"models": []}


def test_出站正则_盖住老的三条与新增几条():
    # 老 _SECRET_RE 的三条（迁移前后必须一模一样地抹掉）
    assert "pst-" not in R.scrub_text("err pst-abc123 tail")
    assert R.scrub_text("Bearer abcdefg") == "***"
    assert R.scrub_text("Authorization: xx") == "***"
    for s in ("sk-ant-abc123456", "sk-abcdefghij", "AIza" + "b" * 24, "ghp_abc123",
              "x-api-key: zzz", "1234567890:" + "a" * 35):
        assert R.scrub_text("head %s tail" % s).count("***") == 1, s
        assert s not in R.scrub_text("head %s tail" % s), s


def test_出站正则_无前缀随机串挡不住_所以正确性靠第一层():
    """§0.2 写死的已知边界：正则黑名单挡不住智谱/Azure 那类无前缀随机串。"""
    random_key = "a3f9c2e1b7d4508a6c1f2e9d7b3a4c5e6f8091a2"
    assert random_key in R.scrub_text(random_key)
    assert random_key not in str(R.project({"name": "x", "api-key": random_key}))


def test_自由文本字段递归脱敏_其余字段不动():
    body = {"error": "cliproxy_reject", "detail": "上游 401 Bearer sk-leak123456",
            "items": [{"tail": "x-api-key: leak"}], "label": "sk-not-a-field-we-touch"}
    out, changed = R.scrub_fields(body)
    assert changed
    assert out["detail"] == "上游 401 ***"
    assert out["items"][0]["tail"] == "***"
    assert out["label"] == body["label"], "非自由文本字段不该被改写"
    assert R.scrub_fields({"error": "x"}) == ({"error": "x"}, False)


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
