# -*- coding: utf-8 -*-
"""预设参数 → 生图请求的端到端生效。

styles.json 用 NOVELAI_SKILL_ROOT 指到临时目录，绝不读写本机真实的画风绑定。
"""
from ip_cli import (Stub, base_config, params_of, require_payload, run_cli, tmpdir,
                    write_styles_root)

BOT = "demo-bot"


def _run_with_params(params, args=(), config=None):
    tmp = tmpdir()
    root = write_styles_root(tmp, {
        "active": "",
        "active_by_bot": {BOT: "style_x"},
        "styles": [{"id": "style_x", "name": "测试预设", "positive_prefix": "",
                    "negative_prefix": "", "params": params}],
    })
    with Stub() as stub:
        rc, _, err = run_cli(tmp, prompt="a girl standing in the rain",
                             config=config or base_config(model="nai-diffusion-4-5-full",
                                                          steps=23, cfg_scale=5),
                             args=["--agent-name", BOT] + list(args),
                             stub_url=stub.url, styles_root=root)
        return require_payload(stub, rc, err)


def test_预设里的模型_覆盖全局默认模型():
    assert _run_with_params({"model": "nai-diffusion-5-curated"}).get("model") \
        == "nai-diffusion-5-curated"


def test_预设不配模型_跟随全局默认模型():
    assert _run_with_params({"steps": 20}).get("model") == "nai-diffusion-4-5-full"


def test_预设里的步数_覆盖全局默认步数():
    assert params_of(_run_with_params({"steps": 12})).get("steps") == 12


def test_预设里的cfg_覆盖全局默认cfg():
    p = params_of(_run_with_params({"cfg_scale": 8}))
    assert p.get("scale", p.get("cfg_scale")) == 8, "cfg 未生效，请求参数=%s" % sorted(p)


def test_预设里的采样器_覆盖全局默认采样器():
    assert params_of(_run_with_params({"sampler": "k_dpmpp_2m_sde"})).get("sampler") \
        == "k_dpmpp_2m_sde"


def test_预设里的ucPreset_覆盖全局默认():
    assert params_of(_run_with_params({"ucPreset": 3})).get("ucPreset") == 3


def test_非法参数被丢弃并退回全局默认():
    """styles.json 可能被手工编辑过；读取端是第二道防线，非法值不该进 payload。"""
    p = params_of(_run_with_params({"steps": 999, "cfg_scale": 0, "ucPreset": 9}))
    assert p.get("steps") == 23, "越界 steps 被透传了：%s" % p.get("steps")
    assert p.get("scale", p.get("cfg_scale")) == 5
    assert p.get("ucPreset") == 2


def test_表外参数不进请求():
    p = params_of(_run_with_params({"width": 4096, "steps": 20}))
    assert p.get("width") != 4096, "表外字段被塞进了请求"
    assert p.get("steps") == 20


def test_反向_预设参数不影响宽高_ratio仍然说了算():
    p = params_of(_run_with_params({"steps": 12, "model": "nai-diffusion-5-curated"},
                                   args=["--ratio", "portrait"]))
    assert (p.get("width"), p.get("height")) == (832, 1216)


def test_回归_不带agent_name时不受任何预设影响():
    """未指定 bot 的调用（手工/调试跑）应完全走全局默认，不套任何预设。"""
    tmp = tmpdir()
    root = write_styles_root(tmp, {
        "active": "style_x",
        "active_by_bot": {},
        "styles": [{"id": "style_x", "name": "全局预设", "positive_prefix": "",
                    "negative_prefix": "", "params": {"steps": 7}}],
    })
    with Stub() as stub:
        rc, _, err = run_cli(tmp, prompt="a girl standing in the rain",
                             config=base_config(model="nai-diffusion-4-5-full", steps=23),
                             stub_url=stub.url, styles_root=root)
        payload = require_payload(stub, rc, err)
        assert payload.get("model") == "nai-diffusion-4-5-full"
        assert params_of(payload).get("steps") == 23
