# -*- coding: utf-8 -*-
"""pub-comic-nai 验收的公共夹具。只依据接口文件写，不读脚本源码。

- 每条用例一个沙箱：技能目录临时副本（NOVELAI_SKILL_ROOT 指过去）、临时 HOME、状态目录、输出目录；
- 本机假 NovelAI 服务只监听 127.0.0.1 端口 0，经 HTTPS_PROXY + 测试 CA 截下脚本的生图请求，不连外网；
- 令牌是明显的假值（pcn_helpers.FAKE_TOKEN）。
"""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import pcn_helpers as H  # noqa: E402


@pytest.fixture
def fake(tmp_path):
    f = H.FakeNovelAI(tmp_path / "fake")
    yield f
    f.close()


@pytest.fixture
def bench(tmp_path, fake):
    return H.Bench(tmp_path / "b", fake)
