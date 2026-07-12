"""读 bot 的 MEMORY.md 给 self-initiate prompt 注入摘要。

实际位置：~/.claude/projects/<slug>/memory/MEMORY.md
（这是 Claude Code 内置 auto-memory 路径，bot 在对话中也会自己更新它）

slug 算法见 chat_history._project_slug_for。
"""
import os


def _project_slug_for(bot_channel_path: str) -> str:
    abs_dir = os.path.abspath(bot_channel_path)
    return abs_dir.replace("/", "-").replace(".", "-")


def memory_path(bot_channel_path: str) -> str:
    slug = _project_slug_for(bot_channel_path)
    return os.path.expanduser(f"~/.claude/projects/{slug}/memory/MEMORY.md")


def read_memory_brief(bot_channel_path: str, max_chars: int = 800) -> str:
    """返回 memory.md 的精简摘要（去掉规则注释，截断到 max_chars）。"""
    p = memory_path(bot_channel_path)
    if not os.path.exists(p):
        return ""
    try:
        with open(p, encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return ""

    # 去掉 HTML 注释（规则块 + USER-MANUAL-LOCK 注释包装）
    import re
    cleaned = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)
    cleaned = cleaned.strip()

    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + "...[省略]"
    return cleaned


def _extract_life_facts(content: str) -> list[str]:
    """从 MEMORY.md 抽"关于用户/我和用户之间"这类生活事实节的 bullet 条目，
    跳过"生图规则/回复要求"等几千字样板（那些不是能聊的话题）。"""
    import re
    content = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)
    facts = []
    in_life = False
    for line in content.splitlines():
        s = line.strip()
        if s.startswith("## ") or s.startswith("# "):
            in_life = any(k in s for k in ("关于用户", "关于你", "关系", "我和用户", "我们", "之间"))
            continue
        if in_life and s.startswith("- ") and len(s) > 4:
            fact = s[2:].strip()
            # 跳过空占位（如"重要日期："后面没填）、纯标题、括号说明
            if fact and not fact.endswith("：") and not fact.endswith(":") and not fact.startswith("（"):
                # 跳过不适合当聊天话题的静态标签（外貌/生图参数/名字/称呼/身份）
                if any(skip in fact[:12] for skip in ("外貌", "入镜", "生图", "名字", "称呼", "身份")):
                    continue
                facts.append(fact)
    return facts


def pick_memory_hook(bot_channel_path: str, chat_id: str = "") -> str:
    """从 MEMORY.md 的生活事实里挑 1 条当主动话题钩子，去重轮转（避免每次提同一件）。
    没有可挑的就返回空串。"""
    p = memory_path(bot_channel_path)
    if not os.path.exists(p):
        return ""
    try:
        content = open(p, encoding="utf-8").read()
    except Exception:
        return ""
    facts = _extract_life_facts(content)
    if not facts:
        return ""
    import hashlib, json, random
    def _h(x: str) -> str:
        return hashlib.md5(x.encode("utf-8")).hexdigest()[:8]
    state_dir = os.path.expanduser("~/.claude/dispatcher/.self-initiate-state")
    os.makedirs(state_dir, exist_ok=True)
    bot = os.path.basename(os.path.abspath(bot_channel_path))
    state_f = os.path.join(state_dir, f"{bot}-{chat_id}.mem-recall")
    try:
        used = json.load(open(state_f, encoding="utf-8"))
    except Exception:
        used = []
    fresh = [f for f in facts if _h(f) not in used]
    pool = fresh if fresh else facts  # 都提过了就重新轮
    pick = random.SystemRandom().choice(pool)
    # 记住最近用过的（保留 max(3, 总数-1) 条，保证轮转不会立刻重复）
    used = ([_h(pick)] + [u for u in used if u != _h(pick)])[: max(3, len(facts) - 1)]
    try:
        json.dump(used, open(state_f, "w", encoding="utf-8"))
    except Exception:
        pass
    return pick


def ensure_memory_exists(bot_channel_path: str, display_name: str):
    """新接入 bot 时调用。

    重要：MEMORY.md 是 Claude Code 内置 auto-memory 文件，bot 自己也在维护。
    - 已存在（任何内容）→ 不动，让 bot 自己继续维护
    - 不存在 → 创建空目录 + 模板（避免 compactor 第一次跑时无文件可改）
    """
    p = memory_path(bot_channel_path)
    if os.path.exists(p):
        return
    # 父目录可能不存在（bot 还没跑过任何 session）
    os.makedirs(os.path.dirname(p), exist_ok=True)
    template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory_template.md")
    with open(template_path, encoding="utf-8") as f:
        tmpl = f.read()
    content = tmpl.replace("{DISPLAY_NAME}", display_name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
