"""desync 验收：夹具注册 + 生产护栏。

- 进程入口即设 CLAUDEBOTLIFE_TEST=1 / CLAUDEBOTLIFE_TEST_ROOT / HOME（§11.1）；iso 夹具再按用例覆盖到 tmp_path。
- 会话前后被动记录生产状态（tmux tg-* 会话数、17801-17804 是否在听，用 lsof 只读，不连接）；不一致 → 整批判失败。
"""
import atexit
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import tempfile

import pytest

_ROOT = tempfile.mkdtemp(prefix="desync-session-")
atexit.register(shutil.rmtree, _ROOT, ignore_errors=True)  # --co 等不跑夹具的场合也清理
os.environ["CLAUDEBOTLIFE_TEST"] = "1"
os.environ["CLAUDEBOTLIFE_TEST_ROOT"] = _ROOT
os.makedirs(os.path.join(_ROOT, "home"), exist_ok=True)
os.environ["HOME"] = os.path.join(_ROOT, "home")

_TMUX = shutil.which("tmux")
_LSOF = shutil.which("lsof")

# ---- 会话级网络安全网（§11.2(3) 之外的纵深）：socket 层拦截出站连接，覆盖 urllib/requests/http.client 等一切走 socket 的库 ----
# 放行：回环地址上的非生产端口（测试进程内的假服务）、测试根之下的 unix socket。
# 拦截并抛 RuntimeError：回环地址的生产端口（dispatcher 17801-17804 / 语音桥 7788 / 管理台 8765 / cliproxy 8317 / 代理 7897 与系统代理端口）、任何非回环地址。
# 被测代码吞掉异常也逃不掉：每次拦截记入 NET_BLOCKS，该用例在 call 阶段强制失败，会话结束逐条打印。
# 人工跑外网实验：CLAUDEBOTLIFE_NET_GUARD=0 关闭（默认开启）。必须装在 import _helpers/director 之前。
NET_GUARD = os.environ.get("CLAUDEBOTLIFE_NET_GUARD", "1") != "0"
# 代理端口也拦（INTERFACE §11.2(3) 的 7897 + scutil --proxy 报告的系统代理端口）：回环上的代理能转发到任何地方，等于外网/生产的后门；
# 同时清掉 *_proxy 环境变量并设 no_proxy=*，让 urllib/requests 不再自动走系统代理。
NET_BLOCKED_PORTS = {17801, 17802, 17803, 17804, 7788, 8765, 8317, 7897}
try:
    NET_BLOCKED_PORTS |= {int(p) for p in re.findall(r"Port\s*:\s*(\d+)", subprocess.run(
        ["/usr/sbin/scutil", "--proxy"], capture_output=True, text=True, timeout=5).stdout)}
except Exception:  # scutil 不存在/超时：只用静态表
    pass
for _k in list(os.environ):
    if _k.lower().endswith("_proxy"):
        del os.environ[_k]
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "*"
NET_BLOCKS: list = []  # [(用例 nodeid, 被拦目标)]


def _net_check(addr):
    if isinstance(addr, (str, bytes, os.PathLike)):  # AF_UNIX
        path = os.fsdecode(addr)
        if path.startswith(_ROOT):
            return
        why = f"unix socket {path!r}"
    elif isinstance(addr, tuple) and len(addr) >= 2:
        host, port = str(addr[0]), addr[1]
        try:
            ip = ipaddress.ip_address(host.split("%")[0])
            loop = (getattr(ip, "ipv4_mapped", None) or ip).is_loopback or ip.is_unspecified
        except ValueError:
            loop = host.lower() in ("localhost", "")
        if loop and port not in NET_BLOCKED_PORTS:
            return
        why = f"{host}:{port}（{'生产/代理端口' if loop else '非回环地址'}）"
    else:
        return
    NET_BLOCKS.append((os.environ.get("PYTEST_CURRENT_TEST", "?"), why))
    raise RuntimeError(f"test_mode: net_guard 拦截出站连接 {why}；人工外网实验请设 CLAUDEBOTLIFE_NET_GUARD=0")


def _net_guard(name, real=None):
    real = real or getattr(socket.socket, name)

    def f(self, *a, **k):
        _net_check(a[-1] if name == "sendto" else a[0])  # sendto(data[, flags], addr)：地址在最后
        return real(self, *a, **k)
    f.__name__ = name
    return f


if NET_GUARD:
    for _n in ("connect", "connect_ex", "sendto"):
        setattr(socket.socket, _n, _net_guard(_n))


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    item._net_n = len(NET_BLOCKS)


@pytest.hookimpl(trylast=True)
def pytest_runtest_call(item):
    blocks = NET_BLOCKS[getattr(item, "_net_n", 0):]
    if blocks:
        pytest.fail("net_guard 拦截（被测代码吞掉了异常）: " + "; ".join(w for _, w in blocks), pytrace=False)


def pytest_sessionfinish(session, exitstatus):
    print(f"\nnet_guard={'on' if NET_GUARD else 'OFF'}  net_blocks={len(NET_BLOCKS)}")
    for _t, _w in NET_BLOCKS:
        print(f"  net_guard 拦截: {_t} -> {_w}")


from _helpers import iso, registry, director  # noqa: E402,F401  夹具注册


def _prod_state():
    n = -1
    if _TMUX:
        r = subprocess.run([_TMUX, "ls", "-F", "#S"], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
        n = sum(1 for l in r.stdout.splitlines() if l.startswith("tg-"))
    listening = ()
    if _LSOF:
        r = subprocess.run([_LSOF, "-nP", "-iTCP", "-sTCP:LISTEN"], capture_output=True, text=True)
        listening = tuple(p for p in (17801, 17802, 17803, 17804) if f":{p} (LISTEN)" in r.stdout)
    return n, listening


@pytest.fixture(scope="session", autouse=True)
def _production_guard():
    before = _prod_state()
    yield
    after = _prod_state()
    shutil.rmtree(_ROOT, ignore_errors=True)
    assert before == after, f"生产状态在测试期间改变: before={before} after={after}"
