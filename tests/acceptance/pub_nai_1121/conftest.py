# -*- coding: utf-8 -*-
"""pub-nai-1121 验收的公共夹具。只依据接口文件写，不读实现代码。

- fake：本机假 NovelAI 服务（127.0.0.1 随机端口 + 测试 CA，经 HTTPS_PROXY 截下请求），不连外网；
- sandbox：技能目录临时副本 + 临时 HOME + 状态/输出目录；
- _skills_before_snapshot：会话级自动夹具，为行为 23 在本目录首个用例之前记下 skills/ 状态。
"""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import pn_helpers as H  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _skills_before_snapshot():
    """行为 23：在本目录第一个用例开始前记下 skills/ 状态。
    用会话级自动夹具而不是 pytest_sessionstart：子目录 conftest 里的会话钩子，从上层目录
    （tests/acceptance、仓库根、门禁脚本）运行时不会触发；自动夹具只要本目录有用例被执行就会生效。"""
    H.SKILLS_BEFORE = H.skills_snapshot()
    yield H.SKILLS_BEFORE


@pytest.fixture
def fake(tmp_path):
    f = H.RejectingFake(tmp_path / "fake")
    yield f
    f.close()


@pytest.fixture
def fake_reject_first(tmp_path):
    """第一个生图请求回 400，之后恢复正常。"""
    f = H.RejectingFake(tmp_path / "fake", reject_first=1)
    yield f
    f.close()


@pytest.fixture
def sandbox(tmp_path, fake):
    return H.Sandbox(tmp_path / "b", fake)
