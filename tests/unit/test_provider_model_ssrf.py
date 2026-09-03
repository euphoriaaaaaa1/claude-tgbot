# -*- coding: utf-8 -*-
"""INTERFACE §3.3 SSRF 收口。全部注入假解析器，不发一次真 DNS。"""
import pytest

from conftest import payload, no_dns, dns_to
from moments.provider_model import HubError, validate_provider_payload


def code(url, resolve=no_dns, allow_private=False):
    try:
        validate_provider_payload(payload(base_url=url), allow_private=allow_private,
                                  resolve=resolve)
        return "ok"
    except HubError as e:
        return e.error


@pytest.mark.parametrize("url", ["http://127.0.0.1:8317/v1", "http://192.168.1.1/",
                                 "http://169.254.169.254/", "http://10.0.0.5/v1",
                                 "http://172.16.0.1/", "http://[::1]:8317/v1",
                                 "http://0.0.0.0:8080/", "http://224.0.0.1/",
                                 "http://240.0.0.1/", "http://[fe80::1]/",
                                 "http://[fc00::1]/"])
def test_IP字面量内网一律400(url):
    assert code(url) == "bad_base_url_private", url


def test_ipv4_mapped_ipv6不能绕过():
    """::ffff:127.0.0.1 的 is_loopback 是 False，直接判等于留后门。"""
    assert code("http://[::ffff:127.0.0.1]:8317/v1") == "bad_base_url_private"


def test_域名解析到回环要拦_localhost():
    assert code("http://localhost:17801/v1", resolve=dns_to("127.0.0.1")) == \
        "bad_base_url_private"


def test_域名解析到内网要拦_自建网关():
    assert code("https://gw.internal/v1", resolve=dns_to("10.1.2.3")) == \
        "bad_base_url_private"


def test_多条A记录里有一条内网就拦():
    assert code("https://mixed.example.com/v1",
                resolve=dns_to("93.184.216.34", "192.168.0.9")) == "bad_base_url_private"


@pytest.mark.parametrize("url", ["https://api.deepseek.com/v1", "http://93.184.216.34/v1",
                                 "https://gw.example.com", "https://[2606:4700::1]/v1"])
def test_公网地址放行(url):
    assert code(url, resolve=dns_to("93.184.216.34")) == "ok"


def test_解析不出来的域名放行():
    """DNS 挂了不该把用户的合法配置判成非法（挡不掉就放行，只挡直接利用）。"""
    assert code("https://api.deepseek.com/v1", resolve=no_dns) == "ok"


def test_解析器抛异常不会冒泡成500():
    """默认解析器自己吞掉 gaierror；这里验注入的解析器返回垃圾也不炸。"""
    assert code("https://x.example.com/v1", resolve=lambda h: ["不是IP", ""]) == "ok"


@pytest.mark.parametrize("url", ["http://127.0.0.1:8317/v1", "http://192.168.1.1/"])
def test_逃生门开启后同样输入放行(url):
    assert code(url, allow_private=True) == "ok"


def test_逃生门不放行畸形url():
    """逃生门只跳过内网判定，不跳过"必须是 http(s) 绝对地址"。"""
    assert code("不是url", allow_private=True) == "bad_base_url"


def test_十进制IP形态由解析器兜住():
    """http://2130706433/ 在多数解析器上就是 127.0.0.1；ipaddress 认不出它，靠 DNS 那一跳挡。"""
    assert code("http://2130706433/", resolve=dns_to("127.0.0.1")) == "bad_base_url_private"


def test_带用户名密码的netloc取的是host不是userinfo():
    """http://evil.com@127.0.0.1/ 的真实 host 是 127.0.0.1，取错就等于被绕过。"""
    assert code("http://evil.com@127.0.0.1:8317/v1") == "bad_base_url_private"


def test_默认解析器吞掉解析异常不冒泡():
    """空 host 会让 getaddrinfo 立刻抛 gaierror（不发包），验证它被吞成空列表。"""
    from moments.provider_model import resolve_host
    assert resolve_host("") == []


# ---------------- BUG-05：urlsplit 自己会抛，必须 400 不是 500 ----------------

@pytest.mark.parametrize("url", ["http://[::1", "http://[fe80::1/v1",
                                 "http://127.0.0.1:99999/v1", "http://127.0.0.1:-1/v1",
                                 "http://127.0.0.1:abc/v1"])
def test_畸形URL一律bad_base_url而不是冒泡成500(url):
    assert code(url) == "bad_base_url", url


def test_多余右括号被urlsplit读成回环_照样拦住():
    """`http://[::1]]:80/` 不抛异常，hostname 是 ::1 —— 走内网判定这条路拦下。"""
    assert code("http://[::1]]:80/") == "bad_base_url_private"


# ---------------- BUG-14：方括号里只能是 IP 字面量 ----------------

@pytest.mark.parametrize("url", ["http://[gggg::1]/v1", "http://[:::::1]/",
                                 "http://[v1.fe80::a]/v1", "http://[127.0.0.1x]/",
                                 "http://[]/v1", "http://[example.com]/v1"])
def test_方括号里不是合法IPv6一律400(url):
    """解析不出地址就会落进域名分支（解析不出 → 放行），成了内网判定的旁路。"""
    assert code(url) == "bad_base_url", url


@pytest.mark.parametrize("url", ["http://[gggg::1]/v1", "http://[v1.fe80::a]/v1"])
def test_逃生门也不放行畸形方括号(url):
    assert code(url, allow_private=True) == "bad_base_url"


@pytest.mark.parametrize("url", ["http://[2606:4700::1]/v1", "http://[::ffff:1.2.3.4]/"])
def test_合法IPv6公网字面量照常放行(url):
    assert code(url) == "ok", url


# ---------------- §12.6 ㉟：Clash TUN 的 fake-IP 假阳性（T4-T6） ----------------

def test_T4_fake_IP段的字面量仍然400():
    """例外只许作用在第 2 层。滥用到字面量这层，等于把整个 SSRF 判定拆了。"""
    assert code("http://198.18.0.1/") == "bad_base_url_private"


@pytest.mark.parametrize("url", ["http://198.18.0.1/", "http://198.19.255.254:8317/v1",
                                 "http://[::ffff:198.18.0.1]/"])
def test_T4_fake_IP段字面量各形态一律400(url):
    """IPv4-mapped 形态同样是字面量，同样走第 1 层。"""
    assert code(url) == "bad_base_url_private", url


def test_T5_域名解析到fake_IP要放行():
    """TUN 事故场景：解析结果被代理接管、不携带真实目的地信息 → 不据它判。
    这条红了 = 开着 Clash TUN 的用户一个 provider 都建不了。"""
    assert code("https://api.deepseek.com/v1", resolve=dns_to("198.18.0.121")) == "ok"


def test_T6_域名解析到真内网仍然400():
    """证明第 2 层没被整层删掉 —— 只是不拿 fake-IP 当证据，别的解析结果照判。"""
    assert code("https://gw.example.com/v1", resolve=dns_to("10.0.0.5")) == \
        "bad_base_url_private"


def test_混合解析里有真内网仍然400():
    """198.18 与 10.x 并存：fake 的那条丢掉，真内网那条留着照拦。"""
    assert code("https://mixed.example.com/v1",
                resolve=dns_to("198.18.0.121", "10.0.0.5")) == "bad_base_url_private"
    assert code("https://mixed.example.com/v1",
                resolve=dns_to("10.0.0.5", "198.18.42.7")) == "bad_base_url_private"


def test_fake_IP与公网并存时按公网判_放行():
    """fake 段那条按噪声丢掉，剩下的公网地址照常放行。
    （部分代理栈只 fake 掉 A 记录、AAAA 直通，那种环境不该被判成内网。）"""
    assert code("https://api.deepseek.com/v1",
                resolve=dns_to("198.18.0.121", "93.184.216.34")) == "ok"


def test_T7_逃生门开启后真内网解析也放行():
    assert code("https://gw.example.com/v1", resolve=dns_to("10.0.0.5"),
                allow_private=True) == "ok"


def test_默认解析器路径也吃这条例外(monkeypatch):
    """路由层调 validate 时**不传** resolve，走的是模块里的 resolve_host。
    只测注入版会漏掉真正跑在生产里的那条路。"""
    from moments import provider_model
    monkeypatch.setattr(provider_model, "resolve_host", lambda _h: ["198.18.0.121"])
    assert provider_model.validate_provider_payload(
        payload(base_url="https://api.deepseek.com/v1"))["base_url"] == \
        "https://api.deepseek.com/v1"
    monkeypatch.setattr(provider_model, "resolve_host", lambda _h: ["10.0.0.5"])
    with pytest.raises(HubError) as ei:
        provider_model.validate_provider_payload(payload(base_url="https://gw.example.com/v1"))
    assert ei.value.error == "bad_base_url_private"


def test_内网文案给出可执行出路():
    """§12.6：只说"不允许"等于把用户堵死在门口，必须写清逃生门怎么开。"""
    with pytest.raises(HubError) as ei:
        validate_provider_payload(payload(base_url="http://127.0.0.1:8317/v1"),
                                  resolve=no_dns)
    assert "HUB_ALLOW_PRIVATE_BASE_URL=1" in ei.value.detail
