"""命令行退出行为、尺寸优先级、非法值不得透传下游。"""
import pytest

from ip_cli import Stub, base_config, params_of, require_payload, run_cli

SIZE_ERR = "width/height must be multiples of 64"


def test_中间文件不存在_退出码为2():
    with Stub() as stub:
        rc, _, err = run_cli(_d(), stub_url=stub.url, write_intermediate=False)
        assert rc == 2, "期望退出码 2，实际 %s；stderr=%s" % (rc, err[-200:])
        assert err.strip(), "退出码 2 时 stderr 必须有中文说明"


def test_中间文件不存在_不向下游发任何请求():
    with Stub() as stub:
        run_cli(_d(), stub_url=stub.url, write_intermediate=False)
        assert stub.payloads == [], "前置校验失败却已经发出了生图请求"


@pytest.mark.parametrize("w", [100, 1000, 63], ids=["100", "1000", "63"])
def test_宽度不是64的倍数_非零退出且报明确原因(w):
    with Stub() as stub:
        rc, _, err = run_cli(_d(), prompt="a girl", stub_url=stub.url,
                             args=["--width", str(w), "--height", "1024"])
        assert rc != 0, "非法宽度 %s 却成功退出" % w
        assert SIZE_ERR in err, "stderr 未包含 %r，实际末尾=%s" % (SIZE_ERR, err[-200:])


def test_宽度为负数_非零退出且报明确原因():
    """-64 是 64 的倍数但不是正整数，必须照样拒绝。"""
    with Stub() as stub:
        rc, _, err = run_cli(_d(), prompt="a girl", stub_url=stub.url,
                             args=["--width", "-64", "--height", "1024"])
        assert rc != 0
        assert SIZE_ERR in err


def test_宽度为0_非零退出():
    """替用户想到的：0 是 64 的倍数，最容易漏掉的边界。"""
    with Stub() as stub:
        rc, _, err = run_cli(_d(), prompt="a girl", stub_url=stub.url,
                             args=["--width", "0", "--height", "1024"])
        assert rc != 0, "宽度 0 被接受了"


def test_非法尺寸_不得把请求发给下游():
    with Stub() as stub:
        run_cli(_d(), prompt="a girl", stub_url=stub.url,
                args=["--width", "100", "--height", "100"])
        assert stub.payloads == [], "非法尺寸被透传到了下游 API"


@pytest.mark.parametrize("ratio,expect", [("portrait", (832, 1216)),
                                          ("landscape", (1216, 832)),
                                          ("square", (1024, 1024))])
def test_ratio决定最终宽高(ratio, expect):
    with Stub() as stub:
        rc, _, err = run_cli(_d(), prompt="a girl", stub_url=stub.url,
                             config=base_config(width=512, height=512),
                             args=["--ratio", ratio])
        p = params_of(require_payload(stub, rc, err))
        assert (p.get("width"), p.get("height")) == expect


def test_显式宽高优先于ratio():
    with Stub() as stub:
        rc, _, err = run_cli(_d(), prompt="a girl", stub_url=stub.url,
                             args=["--ratio", "portrait", "--width", "512", "--height", "512"])
        p = params_of(require_payload(stub, rc, err))
        assert (p.get("width"), p.get("height")) == (512, 512)


def test_不传尺寸参数时_用全局配置的宽高():
    with Stub() as stub:
        rc, _, err = run_cli(_d(), prompt="a girl", stub_url=stub.url,
                             config=base_config(width=768, height=1216))
        p = params_of(require_payload(stub, rc, err))
        assert (p.get("width"), p.get("height")) == (768, 1216)


def test_模型取自全局配置():
    with Stub() as stub:
        rc, _, err = run_cli(_d(), prompt="a girl", stub_url=stub.url,
                             config=base_config(model="nai-diffusion-5-curated"))
        payload = require_payload(stub, rc, err)
        assert payload.get("model") == "nai-diffusion-5-curated"


def test_下游返回400_只请求一次不重试():
    """契约：4xx（429 除外）不重试。重试 4 次会白白拖慢每次失败。"""
    with Stub() as stub:
        rc, _, err = run_cli(_d(), prompt="a girl", stub_url=stub.url)
        require_payload(stub, rc, err)
        assert len(stub.payloads) == 1, "下游 400 却重试了 %d 次" % len(stub.payloads)
        assert rc != 0, "下游 400 却以 0 退出"


def test_下游失败时_stderr不泄露令牌明文():
    with Stub() as stub:
        rc, _, err = run_cli(_d(), prompt="a girl", stub_url=stub.url)
        require_payload(stub, rc, err)
        assert "TESTONLY" not in err, "stderr 里出现了完整 token"


def _d():
    import ip_cli
    return ip_cli.tmpdir()
