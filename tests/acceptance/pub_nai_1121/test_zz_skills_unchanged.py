# -*- coding: utf-8 -*-
"""pub-nai-1121 · 行为 23：跑完本目录测试后，真实仓库 skills/ 没被改动。

测试会话开始时 conftest 记下快照（`git status --porcelain --ignored skills`、SKILL.md 与 assets/*.json 的哈希），
本文件按文件名排在最后，对比"现在"与"开始前"。单独运行时也成立（此时没有别的测试动过它）。
"""
import pn_helpers as H


def test_行为23_跑完全部测试后_仓库skills的忽略状态与关键文件哈希不变():
    assert H.SKILLS_BEFORE is not None, "会话开始时没有记下快照（conftest 没生效）"
    now = H.skills_snapshot()
    assert now["hashes"] == H.SKILLS_BEFORE["hashes"], "SKILL.md 或 assets/*.json 被改动了"
    assert now["status"] == H.SKILLS_BEFORE["status"], \
        "skills/ 的 git status --ignored 变了：\n前：%s\n后：%s" % (H.SKILLS_BEFORE["status"], now["status"])
