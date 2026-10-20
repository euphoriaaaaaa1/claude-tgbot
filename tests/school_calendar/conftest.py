# -*- coding: utf-8 -*-
"""让测试无论从哪个目录跑都能 import 到仓库根的模块（school_calendar 等）。"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "bug_repro: 复现本次要修的 bug，改动前必红、改动后必绿")
