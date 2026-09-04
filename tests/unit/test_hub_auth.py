# -*- coding: utf-8 -*-
"""M0 单测：cookie 签名、回环判定、启动闸、爆破滑窗。

只测纯逻辑，不起 Flask（HTTP 层面的形状由 tests/acceptance/provider_hub/ 覆盖）。
"""
import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from moments import hub_auth as A  # noqa: E402

PW = "hub-access-pw-12345"


def test_cookie_签发校验往返():
    v = A.issue_cookie(PW, A.SALT_SESSION)
    assert A.verify_cookie(v, PW, A.SALT_SESSION)


def test_cookie_每次不同():
    assert A.issue_cookie(PW, A.SALT_SESSION) != A.issue_cookie(PW, A.SALT_SESSION)


def test_cookie_换口令即全体失效():
    v = A.issue_cookie(PW, A.SALT_SESSION)
    assert not A.verify_cookie(v, PW + "x", A.SALT_SESSION)


def test_cookie_盐串不同则不可互冒():
    """两把口令允许设同值 —— 这条是"两道锁没塌成一道"的唯一实现约束。"""
    v = A.issue_cookie(PW, A.SALT_SESSION)
    assert not A.verify_cookie(v, PW, A.SALT_ADMIN)
    assert not A.verify_cookie(A.issue_cookie(PW, A.SALT_ADMIN), PW, A.SALT_SESSION)


def test_cookie_过期即使签名正确也不过():
    exp = int(time.time()) - 60
    nonce = "0123456789abcdef"
    v = "%d.%s.%s" % (exp, nonce, A._sign(PW, A.SALT_SESSION, exp, nonce))
    assert not A.verify_cookie(v, PW, A.SALT_SESSION)


def test_cookie_畸形值一律不过():
    for bad in ("", None, "x", "a.b", "a.b.c.d", "notanint.aa.bb",
                "%d.aa.bb" % (int(time.time()) + 600)):
        assert not A.verify_cookie(bad, PW, A.SALT_SESSION), bad


def test_cookie_空口令不签也不过():
    assert not A.verify_cookie(A.issue_cookie(PW, A.SALT_SESSION), "", A.SALT_SESSION)


def test_回环判定_解析失败按非回环处理():
    for h in ("127.0.0.1", "127.5.6.7", "localhost", "LocalHost", "::1", "[::1]"):
        assert A.is_loopback(h), h
    for h in ("0.0.0.0", "::", "192.168.1.7", "not-a-real-host.invalid", "", None):
        assert not A.is_loopback(h), h


def _startup(monkeypatch, host, pw=None):
    monkeypatch.delenv("HUB_ACCESS_PASSWORD", raising=False)
    if pw is not None:
        monkeypatch.setenv("HUB_ACCESS_PASSWORD", pw)
    out, err = io.StringIO(), io.StringIO()
    rc = A.check_startup(host, out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


def test_启动闸_无口令非回环拒绝且不泄配置(monkeypatch):
    rc, out, err = _startup(monkeypatch, "0.0.0.0")
    assert rc == 2
    assert any(l.startswith("REFUSE:") for l in err.splitlines())


def test_启动闸_无口令回环放行(monkeypatch):
    assert _startup(monkeypatch, "127.0.0.1")[0] is None


def test_启动闸_有口令时非回环也放行(monkeypatch):
    assert _startup(monkeypatch, "0.0.0.0", PW)[0] is None


def test_启动闸_短口令只WARN不拒绝(monkeypatch):
    rc, out, err = _startup(monkeypatch, "127.0.0.1", "short")
    assert rc is None
    assert any(l.startswith("WARN:") for l in (out + err).splitlines())


def test_启动闸_口令不进日志(monkeypatch):
    rc, out, err = _startup(monkeypatch, "0.0.0.0", PW)
    assert PW not in out and PW not in err
    rc, out, err = _startup(monkeypatch, "127.0.0.1", "short")
    assert "short" not in out and "short" not in err


def test_爆破滑窗_计数与延迟():
    g = A.LoginGuard()
    assert not g.blocked()
    assert [g.record_failure() for _ in range(3)] == [1, 2, 3]
    assert g.delay_for(3) == 1.5
    assert g.delay_for(100) == g.MAX_DELAY      # 封顶，别把自己 sleep 死
    g.reset()
    assert g.record_failure() == 1


def test_爆破滑窗_达阈值后拦截且窗口过期自动恢复():
    g = A.LoginGuard()
    for _ in range(g.HARD_LIMIT):
        g.record_failure()
    assert g.blocked()
    g._fails = type(g._fails)(x - g.WINDOW - 1 for x in g._fails)   # 把失败时间挪到窗口外
    assert not g.blocked()


# ---------------- BUG-21 / 安审 F1：口令必须逐键回落 hub.env ----------------

def _hub_env(tmp_path, monkeypatch, text):
    f = tmp_path / "hub.env"
    f.write_text(text, encoding="utf-8")
    monkeypatch.setenv("HUB_ENV_FILE", str(f))
    for k in ("HUB_ACCESS_PASSWORD", "HUB_ADMIN_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    return f


def test_环境变量没有但hub_env里有口令时门是开的(tmp_path, monkeypatch):
    """三条自启路径都不 source hub.env —— 只读 os.environ 的话出货机器上整站零鉴权。"""
    _hub_env(tmp_path, monkeypatch, "# 注释\nCLIPROXY_PORT=8317\nHUB_ACCESS_PASSWORD=%s\n" % PW)
    assert A.access_password() == PW
    assert A.gate_open() is True


def test_hub_env里的管理口令同样回落(tmp_path, monkeypatch):
    _hub_env(tmp_path, monkeypatch, 'HUB_ADMIN_PASSWORD="quoted-admin-pw"\n')
    assert A.admin_password() == "quoted-admin-pw"      # 值两侧的引号要剥掉
    assert A.access_password() == ""                    # 没写的键仍然是空


def test_环境变量优先于hub_env(tmp_path, monkeypatch):
    _hub_env(tmp_path, monkeypatch, "HUB_ACCESS_PASSWORD=from-file\n")
    monkeypatch.setenv("HUB_ACCESS_PASSWORD", "from-env")
    assert A.access_password() == "from-env"


def test_hub_env不存在时不抛错(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_ENV_FILE", str(tmp_path / "根本没有这个文件.env"))
    monkeypatch.delenv("HUB_ACCESS_PASSWORD", raising=False)
    assert A.access_password() == "" and A.gate_open() is False


def test_回落到的口令能让启动闸放行非回环绑定(tmp_path, monkeypatch):
    """闸门读的也是同一个函数：回落没接上时这里会 REFUSE 掉一台配置正确的机器。"""
    _hub_env(tmp_path, monkeypatch, "HUB_ACCESS_PASSWORD=%s\n" % PW)
    out, err = io.StringIO(), io.StringIO()
    assert A.check_startup("0.0.0.0", out=out, err=err) is None
    assert PW not in out.getvalue() + err.getvalue(), "口令值不许出现在启动输出里"


def test_无口令绑回环仍放行但必须出声(tmp_path, monkeypatch):
    """隧道暴露前唯一的可发现路径 —— 静默放行等于把裸奔态藏起来。"""
    _hub_env(tmp_path, monkeypatch, "\n")
    out, err = io.StringIO(), io.StringIO()
    assert A.check_startup("127.0.0.1", out=out, err=err) is None
    assert "HUB_ACCESS_PASSWORD" in err.getvalue()


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
