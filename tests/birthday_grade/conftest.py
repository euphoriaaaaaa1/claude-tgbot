# -*- coding: utf-8 -*-
"""让测试无论从哪个目录跑都能 import 到仓库根的模块（persona_clock / config_loader 等）。"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
