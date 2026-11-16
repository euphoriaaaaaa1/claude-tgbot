# -*- coding: utf-8 -*-
"""生成第 9、13 条用的金样本。只在"改动前"的脚本 / SKILL.md 上跑一次，产物提交进 golden/。

用法（工作树根目录）：python3 tests/acceptance/pub_comic_nai/make_golden.py

- 第 9 条：用本机假 NovelAI 服务（见 pcn_helpers.py 的接缝说明）跑 0 角色、1 角色两份固定中间稿、固定种子，
  存请求体、结果 JSON、last_request.json，按 pcn_helpers.normalize 的规则规整后落盘。
- 第 13 条：存 SKILL.md 里「**一轮要出多张图（mode=new）」那一段的原文。
"""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pcn_helpers as H  # noqa: E402


def main():
    H.GOLDEN_DIR.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        fake = H.FakeNovelAI(Path(tmp) / "fake")
        try:
            for name in H.GOLDEN_CASES:
                files = H.run_golden_case(Path(tmp) / name / "b", fake, name)
                for fn, data in files.items():
                    (H.GOLDEN_DIR / fn).write_bytes(data)
                    print("wrote", fn, len(data))
        finally:
            fake.close()
    block = H.skill_multi_image_block((H.SKILL_SRC / "SKILL.md").read_text(encoding="utf-8"))
    (H.GOLDEN_DIR / "skill_multi_image_block.md").write_text(block, encoding="utf-8")
    print("wrote skill_multi_image_block.md", len(block))


if __name__ == "__main__":
    main()
