"""提示词输出保证：景别/角度/动感三个守卫的可机器验证部分。

断言的是"发给下游的请求里有没有指定的词"，不断言图好不好看。
"""
import pytest

from ip_cli import (ANGLE_WORDS, CLOSEUP_WORDS, DYNAMIC_WORDS, EXPLICIT_LOCAL_WORDS,
                    FULL_BODY_WORDS, NON_FULL_BODY_WORDS, SHOT_WORDS, Stub, base_config,
                    hits, params_of, positive_prompt, require_payload, run_cli, tmpdir)


def _prompt_sent(prompt, config=None):
    with Stub() as stub:
        rc, _, err = run_cli(tmpdir(), prompt=prompt, config=config, stub_url=stub.url)
        return positive_prompt(require_payload(stub, rc, err)), rc, err


def test_正文没写镜头_注入的默认景别是全身():
    sent, _, _ = _prompt_sent("a girl standing in the rain")
    assert hits(sent, FULL_BODY_WORDS), "未注入全身景别，最终提示词=%r" % sent


def test_正文没写镜头_最终提示词不含特写词():
    sent, _, _ = _prompt_sent("a girl standing in the rain")
    assert not hits(sent, CLOSEUP_WORDS), "默认竟然注入了特写：%r" % sent


def test_正文没写镜头_最终同时具备景别和角度和动态感():
    """兜底补词必须三类齐全，不能像旧 _CAMERA_POOL 那样只有角度。"""
    sent, _, _ = _prompt_sent("a girl standing in the rain")
    assert hits(sent, SHOT_WORDS), "缺景别词：%r" % sent
    assert hits(sent, ANGLE_WORDS), "缺机位/角度词：%r" % sent
    assert hits(sent, DYNAMIC_WORDS), "缺动态感词：%r" % sent


def test_正文已写特写_特写被原样保留():
    sent, _, _ = _prompt_sent("close-up of her face, blushing")
    assert hits(sent, CLOSEUP_WORDS), "用户明确要的特写被覆盖了：%r" % sent


def test_正文已写特写_不再注入全身景别():
    sent, _, _ = _prompt_sent("close-up of her face, blushing")
    assert not hits(sent, FULL_BODY_WORDS), "用户要特写却被塞了全身：%r" % sent


def test_正文已写upper_body_仍注入全身且剥离非全身词():
    """BRIEF 2026-08-23：upper body / medium shot 不再是用户明确局部。"""
    sent, _, _ = _prompt_sent("upper body, she is smiling")
    assert hits(sent, FULL_BODY_WORDS), "upper body 不应放行，必须强制全身：%r" % sent
    assert not hits(sent, NON_FULL_BODY_WORDS), "非全身词应从最终提示词剥离：%r" % sent


@pytest.mark.parametrize("prompt", [
    "close-up of her face",
    "face focus of her face",
    "headshot of a girl",
    "portrait of a girl",
])
def test_明确局部词_不注入全身(prompt):
    """只有明确局部词才允许不注入 full body。"""
    sent, _, _ = _prompt_sent(prompt)
    assert hits(sent, EXPLICIT_LOCAL_WORDS), "明确局部词应原样保留：%r" % sent
    assert not hits(sent, FULL_BODY_WORDS), "明确局部词不应再被强制全身：%r" % sent


@pytest.mark.parametrize("prompt", [
    "Upper-body selfie",
    "Medium shot, she smiles",
])
def test_常见中景写法_仍强制全身并剥离非全身词(prompt):
    """即使正文写了 upper body / medium shot，默认仍要全身。"""
    sent, _, _ = _prompt_sent(prompt)
    assert hits(sent, FULL_BODY_WORDS), "非全身词不应放行，必须注入 full body：%r" % sent
    assert not hits(sent, NON_FULL_BODY_WORDS), "非全身词应被处理/剥离：%r" % sent


def test_中文特写_保留局部且不注入全身():
    sent, _, _ = _prompt_sent("给我脸部特写，她微微一笑")
    assert hits(sent, EXPLICIT_LOCAL_WORDS), "中文明确局部词应保留：%r" % sent
    assert not hits(sent, FULL_BODY_WORDS), "中文特写不应再被强制全身：%r" % sent


def test_中文上半身_仍强制全身并剥离非全身词():
    sent, _, _ = _prompt_sent("上半身，她正在微笑")
    assert hits(sent, FULL_BODY_WORDS), "‘上半身’不是明确局部，应强制全身：%r" % sent
    assert not hits(sent, NON_FULL_BODY_WORDS), "中文非全身词应被处理/剥离：%r" % sent


def test_正文已写角度_不再注入别的角度词():
    sent, _, _ = _prompt_sent("from above, a girl lying on grass, full body")
    assert set(hits(sent, ANGLE_WORDS)) == {"from above"}, \
        "角度被叠加了：%s" % hits(sent, ANGLE_WORDS)


def test_正文写了pov_不再注入别的角度词():
    sent, _, _ = _prompt_sent("pov, she leans close, full body")
    assert set(hits(sent, ANGLE_WORDS)) == {"pov"}, "角度被叠加了：%s" % hits(sent, ANGLE_WORDS)


def test_前缀里的特写不参与判定_全身仍被注入且排在最前():
    """契约第 5 条：判定域只看当轮正文，前缀里的镜头词不算数。"""
    cfg = base_config(positive_prefix="close-up, masterpiece")
    sent, _, _ = _prompt_sent("a girl standing in the rain", config=cfg)
    low = sent.lower()
    assert hits(sent, FULL_BODY_WORDS), "前缀里的特写误判成了正文景别：%r" % sent
    assert "close-up" in low, "前缀内容被吞掉了：%r" % sent
    assert min(low.index(w) for w in hits(sent, FULL_BODY_WORDS)) < low.index("close-up"), \
        "注入词没有排在最前：%r" % sent


def test_注入只影响正向提示词_负向提示词不被塞镜头词():
    cfg = base_config(negative_prefix="lowres, bad hands")
    with Stub() as stub:
        rc, _, err = run_cli(tmpdir(), prompt="a girl standing in the rain",
                             config=cfg, stub_url=stub.url)
        payload = require_payload(stub, rc, err)
        neg = (params_of(payload).get("negative_prompt") or "").lower()
        assert not hits(neg, SHOT_WORDS + ANGLE_WORDS), "负向提示词被注入了镜头词：%r" % neg


def test_正文已含全身_全身词不被重复堆叠():
    """替用户想到的：重复词会挤占 token 预算。"""
    sent, _, _ = _prompt_sent("full body, a girl standing in the rain")
    assert sent.lower().count("full body") == 1, "全身词出现了 %d 次：%r" % (
        sent.lower().count("full body"), sent)


def test_中文正文没写镜头_同样注入全身():
    """替用户想到的：正文是中文时判定不能失灵。"""
    sent, _, _ = _prompt_sent("一个女孩站在雨里，穿着校服")
    assert hits(sent, FULL_BODY_WORDS), "中文正文未注入全身：%r" % sent


def test_空正文_不崩溃不抛栈():
    """边界：正文为空串时要么正常带默认镜头词，要么明确报错，但不能抛 traceback。"""
    with Stub() as stub:
        rc, _, err = run_cli(tmpdir(), prompt="", stub_url=stub.url)
        assert "Traceback" not in err, "空正文触发未捕获异常：%s" % err[-400:]
        if stub.payloads:
            assert hits(positive_prompt(stub.payloads[0]), FULL_BODY_WORDS)
