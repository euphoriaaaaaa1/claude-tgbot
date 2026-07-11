#!/usr/bin/env python3
"""为 self-initiate.sh 打补丁。

用法: patch_self_initiate.py <path-to-self-initiate.sh> <project_dir>

修改两处：
1. 在 set -euo pipefail 后插入 marker 行
2. 删除原概率 skip 块（夜间 80% / 白天 50%）
3. 替换 text=...[self-initiate] hour=... 行为调用 life-context.py

幂等：如果 marker 已存在，不再修改。
"""
import sys
import re
import os
from pathlib import Path

MARKER = "# CLAUDEBOTLIFE_INSTALLED=v4"


def main():
    if len(sys.argv) != 3:
        print("Usage: patch_self_initiate.py <path> <project_dir>", file=sys.stderr)
        sys.exit(1)

    path = Path(sys.argv[1])
    project_dir = sys.argv[2]
    src = path.read_text()

    if MARKER in src:
        print("Marker already present, skipping.", file=sys.stderr)
        return

    # ─── 改动 1：在 set -euo pipefail 后插入 marker ─────────
    src = src.replace(
        "set -euo pipefail",
        f"set -euo pipefail\n{MARKER}",
        1,
    )

    # ─── 改动 2：删除原概率 skip 块 ──────────────────────────
    # 匹配：从 "# ─── 时段随机 skip ─" 到下一个 "# ───" 之前
    pattern_skip = re.compile(
        r"# ─── 时段随机 skip ─+.*?(?=# ───)", re.DOTALL
    )
    src = pattern_skip.sub(
        "# ─── 时段随机 skip ─── (已被 claudebotlife 框架替代为情境驱动 SKIP)\n\n",
        src,
        count=1,
    )

    # ─── 改动 3：替换 text 生成逻辑 ──────────────────────────
    # 找到形如:  text="[self-initiate] hour=${hour_int} since_last_user_msg_min=${since_min}"
    # 替换为完整的新逻辑（调 life-context.py + 解析 JSON）
    new_text_block = (
        f'# ─── claudebotlife: 调富情境生成器 ─────────────────────\n'
        f'LIFE_CTX_BIN="{project_dir}/life-context.py"\n'
        f'if [ ! -f "$LIFE_CTX_BIN" ]; then\n'
        f'  text="[self-initiate] hour=${{hour_int}} since_last_user_msg_min=${{since_min}}"\n'
        f'else\n'
        f'  output=$(python3 "$LIFE_CTX_BIN" "$bot" "$chat" 2>>/tmp/life-context.err) || {{\n'
        f'    echo "life-context exit=$? → skip" >&2; exit 0;\n'
        f'  }}\n'
        f'  action=$(echo "$output" | python3 -c "import sys,json; print(json.load(sys.stdin)[\\"action\\"])")\n'
        f'  case "$action" in\n'
        f'    SKIP)\n'
        f'      reason=$(echo "$output" | python3 -c "import sys,json; print(json.load(sys.stdin).get(\\"reason\\",\\"\\"))")\n'
        f'      echo "skip per life-context: $reason" >&2; exit 0;;\n'
        f'    FALLBACK)\n'
        f'      text="[self-initiate] hour=${{hour_int}} since_last_user_msg_min=${{since_min}}";;\n'
        f'    TEXT)\n'
        f'      text=$(echo "$output" | python3 -c "import sys,json; print(json.load(sys.stdin)[\\"text\\"])")\n'
        f'      if [ "${{#text}}" -gt 4000 ]; then\n'
        f'        text="${{text:0:3997}}..."; echo "text truncated to 4000" >&2;\n'
        f'      fi;;\n'
        f'    *) echo "unknown action: $action → skip" >&2; exit 0;;\n'
        f'  esac\n'
        f'fi\n'
    )

    # 用正则匹配旧的 text= 行
    pattern_text = re.compile(
        r'^text="\[self-initiate\] hour=\$\{hour_int\} since_last_user_msg_min=\$\{since_min\}"\s*$',
        re.MULTILINE,
    )
    if not pattern_text.search(src):
        print("ERROR: 找不到原 text= 行，patch 失败", file=sys.stderr)
        sys.exit(1)
    src = pattern_text.sub(new_text_block, src, count=1)

    path.write_text(src)
    print("Patch applied successfully.", file=sys.stderr)


if __name__ == "__main__":
    main()
