# -*- coding: utf-8 -*-
"""pub-nai-1121 · 行为 23：跑完本目录测试后，真实仓库 skills/ 没被改动。

本目录首个用例开始前，conftest 里的会话级自动夹具记下快照（`git status --porcelain --ignored skills`、
SKILL.md 与 assets/*.json 的哈希）；本文件按文件名排在最后，对比"现在"与"开始前"。
自动夹具不依赖启动目录，所以单跑本目录、从 tests/acceptance 或仓库根一起跑、走门禁脚本都成立。
"""
import pn_helpers as H


def test_行为23_跑完全部测试后_仓库skills的忽略状态与关键文件哈希不变():
    assert H.SKILLS_BEFORE is not None, "本目录首个用例前没有记下快照（conftest 的会话级自动夹具没生效）"
    now = H.skills_snapshot()
    assert now["hashes"] == H.SKILLS_BEFORE["hashes"], "SKILL.md 或 assets/*.json 被改动了"
    assert now["status"] == H.SKILLS_BEFORE["status"], \
        "skills/ 的 git status --ignored 变了：\n前：%s\n后：%s" % (H.SKILLS_BEFORE["status"], now["status"])
