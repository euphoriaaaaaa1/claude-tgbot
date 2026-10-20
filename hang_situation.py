#!/usr/bin/env python3
"""被晾感知（hang）的活动查询桥：吐一行 JSON
{"name","state","interruptible","term_label"}。

只读：加载 configs/<bot>.yml 的作息表，问 generators.situation "她此刻应该在干什么"。
不写库、不发消息、不回显配置内容（configs 里有 token 和 chat_id，出错时只报异常类名，
绝不带配置值）。

用法：python3 hang_situation.py <bot>   （cwd 需为本仓根目录）
"""
import json
import os
import sys
from datetime import datetime

# 本文件就躺在仓根，与 configs/ generators/ school_calendar.py 同级；
# 装到别处时用 BOTLIFE_DIR 指过去。
REPO_DIR = os.environ.get("BOTLIFE_DIR") or os.path.dirname(os.path.abspath(__file__))


def _term_label(act) -> str:
    """中文学期标签。取 act.term——它就是 school_calendar.term_state 在这同一时刻
    算出来的值（generators.situation 已经调过），再调一遍既多算一次又可能跨天不一致。

    "在校"是常态不用讲，映射成空串（口径与 formatter.py 的学期状态行一致）；
    没配 school_calendar 的 bot 判定恒为 in_session，同样得空串。
    取不到一律空串，绝不连累主判定。
    """
    try:
        from school_calendar import IN_SESSION, TERM_LABEL_ZH
        term = getattr(act, "term", IN_SESSION)
        return "" if term == IN_SESSION else TERM_LABEL_ZH.get(term, "")
    except Exception as e:
        sys.stderr.write("hang_situation: term_label lookup failed (%s)\n"
                         % type(e).__name__)
        return ""


def query(bot: str) -> dict:
    sys.path.insert(0, REPO_DIR)
    import config_loader
    from generators import situation

    cfg = config_loader.load_bot(bot)
    now = datetime.now()
    act = situation.get_current_recurring(cfg, now)
    # interruptible 是本功能新加的可选字段，situation.Activity 不带它 → 拿它同一张
    # 作息表（学期/节假日判定完全一致）再找一次命中项。缺省可打断；
    # 兜底档（睡眠/自由时间）在表里找不到命中项，也就保持缺省。
    interruptible = True
    try:
        term = situation._current_term(cfg, now)
        for item in situation._pick_schedule(cfg, now, term):
            if isinstance(item, dict) and situation._time_in_range(
                    now.time(), item.get("when", "")):
                interruptible = item.get("interruptible", True) is not False
                break
    except Exception as e:            # 私有函数哪天改名也不能连累主判定
        sys.stderr.write("hang_situation: interruptible lookup failed (%s)\n"
                         % type(e).__name__)
    # term_label 是后加的字段，hang_runtime 只读前三个键 → 向后兼容
    return {"name": act.name, "state": act.state, "interruptible": interruptible,
            "term_label": _term_label(act)}


def main() -> int:
    bot = sys.argv[1] if len(sys.argv) > 1 else ""
    if not bot:
        sys.stderr.write("hang_situation: missing bot id\n")
        return 2
    try:
        out = query(bot)
    except Exception as e:
        # 只报类名：异常正文可能带配置路径/内容
        sys.stderr.write("hang_situation: %s failed (%s)\n" % (bot, type(e).__name__))
        return 1
    json.dump(out, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
