#!/usr/bin/env python3
"""积温 cron 入口 — Phase 3。

每 5 分钟由 launchd 触发：
  1. 读 _global.yml.jiwen 配置
  2. 遍历 3 个 bot × 主 chat_id
  3. 加载状态文件 → 漂移 → 检测新对话 → 调 delta → 写回
  4. stderr 输出日志

**不破坏现有逻辑**：jiwen.enabled=false 时 tick 仍跑（更新状态文件备用），
但 judge 不会读这些状态——必须等 Phase 4 集成才生效。

跑：python3 jiwen/tick.py [--bot <name>]
"""
from __future__ import annotations
import json
import os
import sys
import time
import argparse
from pathlib import Path

# 让 jiwen 自身的导入工作
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))  # claudebotlife/

import engine
import deepseek_delta
import config_loader

DEFAULT_STATE_DIR = os.path.expanduser("~/.claude/dispatcher/.jiwen-state")


# ─── 配置加载 ────────────────────────────────────────────────

def load_jiwen_config():
    """从 _global.yml.jiwen 读全局配置。返回 dict 或 None（未配置）。"""
    g = config_loader.load_global()
    return (g.get("jiwen") or None) if isinstance(g, dict) else None


def get_bot_jiwen_overrides(bot_id: str) -> dict:
    """读 bot yml 的 jiwen 个性化覆盖（可选）。"""
    try:
        cfg = config_loader.load_bot(bot_id)
        return cfg.get("jiwen") or {}
    except Exception:
        return {}


def merge_rates(global_jiwen: dict, bot_overrides: dict) -> engine.Rates:
    """合并全局 rates + bot 覆盖 → engine.Rates。

    bot 级 override 路径：bot_yml.jiwen.rates_override（兼容老 jiwen.rates）
    """
    rates_dict = dict((global_jiwen or {}).get("rates", {}))
    bot_jw = bot_overrides or {}
    # 兼容两种写法：rates_override（推荐，更明确）或 rates（旧）
    rates_dict.update(bot_jw.get("rates_override", {}))
    rates_dict.update(bot_jw.get("rates", {}))

    # 默认 immersion_map（dict 类型不能用 float() 强转）
    default_immersion_map = {
        "reading": 0.6, "cooking": 0.5, "search": 0.4,
        "browse": 0.35, "observe": 0.15, "selfcare": 0.5,
    }
    immersion_map = rates_dict.get("immersion_map") or default_immersion_map

    return engine.Rates(
        connection_per_min=float(rates_dict.get("connection_per_min", 0.0007)),
        pride_decay_per_min=float(rates_dict.get("pride_decay_per_min", 0.003)),
        valence_decay_per_min=float(rates_dict.get("valence_decay_per_min", 0.005)),
        arousal_decay_per_min=float(rates_dict.get("arousal_decay_per_min", 0.01)),
        accel_threshold_min=float(rates_dict.get("accel_threshold_min", 5.0)),
        connection_accel=float(rates_dict.get("connection_accel", 1.0)),
        valence_severe_low=float(rates_dict.get("valence_severe_low", -0.5)),
        valence_severe_multiplier=float(rates_dict.get("valence_severe_multiplier", 0.3)),
        valence_mild_low=float(rates_dict.get("valence_mild_low", -0.2)),
        valence_mild_multiplier=float(rates_dict.get("valence_mild_multiplier", 1.5)),
        # 新增 11 项
        valence_lock_threshold=float(rates_dict.get("valence_lock_threshold", 0.5)),
        valence_lock_factor=float(rates_dict.get("valence_lock_factor", 0.3)),
        arousal_connection_rise_threshold=float(rates_dict.get("arousal_connection_rise_threshold", 0.5)),
        arousal_connection_rise_rate=float(rates_dict.get("arousal_connection_rise_rate", 0.002)),
        pride_defend_threshold=float(rates_dict.get("pride_defend_threshold", 0.4)),
        pride_defend_target=float(rates_dict.get("pride_defend_target", 0.6)),
        pride_defend_rate=float(rates_dict.get("pride_defend_rate", 0.003)),
        pride_arousal_conflict_rate=float(rates_dict.get("pride_arousal_conflict_rate", 0.001)),
        pride_erosion_rate=float(rates_dict.get("pride_erosion_rate", 0.002)),
        immersion_decay_per_min=float(rates_dict.get("immersion_decay_per_min", 0.010)),
        activity_connection_relief=float(rates_dict.get("activity_connection_relief", 0.0005)),
        immersion_map=dict(immersion_map),
    )


# ─── 状态文件读写（原子写）─────────────────────────────────

def state_path(state_dir: str, bot_id: str, chat_id: str) -> str:
    safe = f"{bot_id}-{chat_id}".replace("/", "_")
    return os.path.join(state_dir, f"{safe}.json")


def load_state(path: str) -> engine.State:
    if not os.path.exists(path):
        return engine.State(last_tick_ts=int(time.time()))
    try:
        with open(path) as f:
            d = json.load(f)
        return engine.state_from_dict(d)
    except Exception as e:
        print(f"[jiwen.tick] 加载状态失败 {path}: {e}", file=sys.stderr)
        return engine.State(last_tick_ts=int(time.time()))


def save_state(path: str, state: engine.State):
    """原子写：先写 .tmp 再 rename。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(engine.state_to_dict(state), f, ensure_ascii=False, indent=2)
    os.rename(tmp, path)


# ─── 对话扫描（找新 user 消息）─────────────────────────────

def find_recent_messages(channel_dir: str, since_ts: int, limit: int = 12) -> tuple[list[dict], int]:
    """从 worker session jsonl 找比 since_ts 新的对话片段。

    返回 (messages, max_ts)：
      messages: [{"role":"user"|"assistant", "content":str}] 最近 N 条
      max_ts: 最新一条 user 消息的 unix ts（用于下次 since_ts）
    """
    # bot session jsonl 在 ~/.claude/projects/-<home-path>--claude-channels-<name>/<uuid>.jsonl
    project_root = os.path.expanduser("~/.claude/projects")
    # channel_dir 形如 ~/.claude/channels/<name> → 反推 Claude Code 的 project 目录名
    p = Path(channel_dir)
    proj_name = str(p).replace("/", "-").replace(".", "-")
    proj_dir = os.path.join(project_root, proj_name)
    if not os.path.isdir(proj_dir):
        return [], since_ts

    # 找最近修改的 jsonl
    jsonls = sorted(
        [f for f in os.listdir(proj_dir) if f.endswith(".jsonl")],
        key=lambda x: os.path.getmtime(os.path.join(proj_dir, x)),
        reverse=True,
    )
    if not jsonls:
        return [], since_ts

    latest = os.path.join(proj_dir, jsonls[0])
    msgs = []
    max_user_ts = since_ts

    try:
        with open(latest) as f:
            lines = f.readlines()
    except Exception as e:
        print(f"[jiwen.tick] 读 jsonl 失败 {latest}: {e}", file=sys.stderr)
        return [], since_ts

    # 倒序扫，取最近 limit 条 user/assistant 文本
    for line in reversed(lines):
        if len(msgs) >= limit:
            break
        try:
            obj = json.loads(line)
        except Exception:
            continue
        msg = obj.get("message", {})
        role = msg.get("role")
        content = msg.get("content", "")
        # content 可能是 list，取 type=text 的部分
        if isinstance(content, list):
            text_parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            content = " ".join(text_parts).strip()
        if not isinstance(content, str) or not content.strip():
            continue
        if role not in ("user", "assistant"):
            continue
        # 时间戳
        ts_str = obj.get("timestamp") or obj.get("ts")
        ts = 0
        if ts_str:
            try:
                from datetime import datetime
                ts = int(datetime.fromisoformat(str(ts_str).replace("Z", "+00:00")).timestamp())
            except Exception:
                ts = 0
        if role == "user" and ts > max_user_ts:
            max_user_ts = ts
        # 剥掉 worker-plugin 注入的【当前关系状态】块——它是给 bot 看的元指令，会混进
        # session jsonl；judge 若把它当用户发言评 delta = 自污染 + 吃满截断预算。
        # 锚定到紧随其后的场景标签(【群聊】/【私聊】)剥，**不依赖 snippet 尾部文案**(尾部会改)。
        import re as _re
        content = _re.sub(r'【当前关系状态】.*?(?=【群聊】|【私聊】)', '', content, flags=_re.S)
        # 兜底：万一没场景标签，按 describe 收尾括号剥
        content = _re.sub(r'^【当前关系状态】.*?）\s*', '', content, flags=_re.S)
        msgs.append({"role": role, "content": content[:300], "ts": ts})

    msgs.reverse()  # 倒序变正序
    # 只保留 since_ts 之后的 user 消息相关上下文（包括之前的 assistant 回复）
    if since_ts > 0:
        # 至少保留最后 6 条以提供上下文
        msgs = msgs[-min(limit, max(6, len(msgs))):]
    # 去掉 ts 字段（deepseek_delta 不需要）
    return [{"role": m["role"], "content": m["content"]} for m in msgs], max_user_ts


def has_new_user_msg(msgs: list[dict], since_ts: int, max_user_ts: int) -> bool:
    """检测是否有新 user 消息触发 delta 计算。"""
    return max_user_ts > since_ts and any(m["role"] == "user" for m in msgs)


# ─── 单 bot tick ────────────────────────────────────────────

def apply_event_emotion(state: engine.State, ev: dict | None, prev_tick_ts: int):
    """M1 事件→情绪桥：本 tick 窗口内【新开始】的事件(started_at>prev_tick_ts)，把它的
    mood_delta 写进 valence(+少量 arousal 表强度)。这是**与用户无关**的情绪源——bot 因
    自己的事(汤糊了/追的剧完结)真的有情绪波动。只应用一次(靠 started_at>prev_tick_ts 去重)。
    返回 (新state, 应用的mood_delta 或 None)。"""
    if not ev or not ev.get("mood_delta"):
        return state, None
    try:
        if int(ev.get("started_at", 0)) <= int(prev_tick_ts):
            return state, None  # 不是本 tick 窗口内新开始的 → 已应用过，跳过
        md = float(ev["mood_delta"])
    except (TypeError, ValueError):
        return state, None
    new = engine.apply_delta(state, {"valence": md, "arousal": abs(md) * 0.3})
    return new, md


def tick_one_bot(bot_id: str, jiwen_cfg: dict, state_dir: str, dry_run: bool = False) -> dict:
    """对一个 bot 跑一次 tick。返回操作摘要。"""
    summary = {"bot": bot_id, "actions": []}

    try:
        bot_cfg = config_loader.load_bot(bot_id)
    except Exception as e:
        summary["error"] = f"load_bot: {e}"
        return summary

    chat_id = str(bot_cfg.get("chat_id", "")).strip()
    if not chat_id:
        summary["error"] = "no chat_id"
        return summary

    channel_dir = bot_cfg.get("bot_channel_path", "")
    if not channel_dir:
        summary["error"] = "no bot_channel_path"
        return summary

    bot_overrides = bot_cfg.get("jiwen") or {}
    rates = merge_rates(jiwen_cfg, bot_overrides)

    # 1. 加载状态
    sp = state_path(state_dir, bot_id, chat_id)
    state = load_state(sp)
    now = int(time.time())
    prev_tick_ts = state.last_tick_ts  # M1 事件去重用：记录漂移前的时间点

    # M2 作息冻结："想你"(connection)在上课/上班/睡觉时不涨(base_rate_override=0)，
    # 下班/醒来才恢复。三人作息不同 → 天然错峰，不再集体撑不住想你。
    _base_override = None
    _busy_reason = ""
    try:
        from generators import situation as _sit
        from datetime import datetime as _dtm
        _act = _sit.get_current_recurring(bot_cfg, _dtm.fromtimestamp(now))
        if _act.state.startswith("busy") or _act.state == "sleeping":
            _base_override = 0.0
            _busy_reason = _act.state
    except Exception as e:
        summary["actions"].append(f"作息冻结 skip: {e}")

    # 2. 漂移
    if state.last_tick_ts > 0:
        mins = (now - state.last_tick_ts) / 60.0
    else:
        mins = 0.0  # 首次跑
    if mins > 0.1:  # >6 秒才漂
        old_conn = state.connection
        state = engine.drift(state, rates, mins=mins,
                             since_last_msg_min=(
                                 (now - state.last_user_msg_ts) / 60.0
                                 if state.last_user_msg_ts > 0 else 0
                             ),
                             base_rate_override=_base_override)
        _froze = f" [作息冻结:{_busy_reason}]" if _base_override == 0.0 else ""
        summary["actions"].append(f"drift {mins:.1f}min: connection {old_conn:.3f}→{state.connection:.3f}{_froze}")

    # 2.5 M1 事件→情绪桥：本 tick 窗口内新开始的自己的事(events.yaml/db)→ 情绪波动(与用户无关)
    try:
        import db as _db
        from datetime import datetime as _dt
        _ev = _db.get_ongoing_event(bot_id, _dt.fromtimestamp(now))
        state, _md = apply_event_emotion(state, _ev, prev_tick_ts)
        if _md is not None:
            summary["actions"].append(
                f"事件[{_ev.get('name')}] mood_delta={_md:+.2f} → valence {state.valence:.3f}(与用户无关)")
    except Exception as e:
        summary["actions"].append(f"事件→情绪桥 skip: {e}")

    # 3. 扫新对话 → 调 delta
    _jiwen_delta = None  # 捕获本 tick 的 jiwen delta，下面映射到关系数值
    msgs, max_user_ts = find_recent_messages(channel_dir, state.last_user_msg_ts, limit=12)
    if msgs and has_new_user_msg(msgs, state.last_user_msg_ts, max_user_ts):
        delta_cfg = (jiwen_cfg or {}).get("delta_llm", {})
        api_key = delta_cfg.get("api_key", "")
        if not api_key:
            summary["actions"].append("skip delta: api_key 未配置")
        else:
            # DeepSeek-V4 1M 上下文，传完整 persona（不截断）
            persona = bot_cfg.get("persona_summary", "") or ""
            bot_jiwen = bot_cfg.get("jiwen") or {}
            delta_hints = bot_jiwen.get("delta_hints", "") or ""
            delta = deepseek_delta.compute_delta(
                persona=persona,
                messages=msgs,
                api_key=api_key,
                delta_hints=delta_hints,
                base_url=delta_cfg.get("base_url", deepseek_delta.DEFAULT_BASE_URL),
                model=delta_cfg.get("model", deepseek_delta.DEFAULT_MODEL),
                proxy=delta_cfg.get("proxy", deepseek_delta.DEFAULT_PROXY),
                timeout=delta_cfg.get("timeout", deepseek_delta.DEFAULT_TIMEOUT),
            )
            if delta is None:
                # API 失败：不 apply、不推进游标 → 下 tick 自动重试，这段对话不丢
                summary["actions"].append("delta 调用失败(None)→不推进游标，下tick重试")
            else:
                old = (state.connection, state.pride, state.valence, state.arousal)
                state = engine.apply_delta(state, delta)
                _jiwen_delta = delta
                new = (state.connection, state.pride, state.valence, state.arousal)
                summary["actions"].append(
                    f"delta applied {delta} → C{old[0]:.3f}->{new[0]:.3f} "
                    f"P{old[1]:.3f}->{new[1]:.3f} V{old[2]:.3f}->{new[2]:.3f} A{old[3]:.3f}->{new[3]:.3f}"
                )
                state.last_user_msg_ts = max_user_ts

    # 3.5 关系数值：energy 随时段漂移(治随叫随到) + 复用 jiwen delta 慢速推进 好感/信任/淫欲
    try:
        import relationship as _rel
        from datetime import datetime as _dt3
        _bot_dir = bot_id  # bot_id 即 channels 下的目录名
        # profile 只在 relationship.json 还不存在时决定初始值(陌生 or 已确立)；
        # 示例 bot 出厂已带 relationship.json，所以这里默认 established 即可。
        # ponytail: 想让某 bot 从陌生起步，出厂 relationship.json 用 stranger 默认值即可
        _prof = "established"
        _rstats = _rel.load(_bot_dir, _prof)
        # 当前活动/事件(上面 M1/M2 已取，取不到则 None)——喂给 energy/desire 让"当日行为"驱动
        _astate = getattr(_act, "state", None) if "_act" in dir() and _act else None
        _event = _ev if "_ev" in dir() else None
        if mins > 0.1:
            _now3 = _dt3.fromtimestamp(now)
            _rstats = _rel.drift_energy(_rstats, _now3, mins, activity_state=_astate,
                                        event=_event, salt=_bot_dir)  # salt→每bot各自随机
            _rstats = _rel.drift_desire(_rstats, _now3, mins,
                                        affection=_rstats.get("affection", 50), event=_event, profile=_prof)
        if _jiwen_delta:  # 有新对话 → 映射到关系数值(保守系数，慢速累积)
            _vd = float(_jiwen_delta.get("valence", 0))
            # 判官若给了专门的 desire(性张力)用它，否则退回 arousal*正向(见 deepseek_delta)
            _dd = float(_jiwen_delta.get("desire", _jiwen_delta.get("arousal", 0)))
            # 例假/生病在场：不涨 desire(否则热聊几句就把压制顶穿)
            _ev_suppress = bool(_event and any(k in (_event.get("name", "") or "")
                                               for k in ("例假", "生病", "感冒", "痛经", "姨妈")))
            _rstats = _rel.apply_delta(_rstats, {
                "affection": _vd * 8,                       # 聊得开心→更喜欢，惹到→掉
                "trust": _vd * 4 if _vd >= 0 else _vd * 6,  # 难建易毁：正向慢涨(×4)、被惹/冷暴力快掉(×6)
                "desire": (_dd * 10 if _vd >= 0 else 0) if not _ev_suppress else 0,
            })
        if not dry_run:
            _rel.save(_bot_dir, _rstats)
        summary["actions"].append(
            f"关系 好感{_rstats['affection']:.0f}/信任{_rstats['trust']:.0f}/淫欲{_rstats['desire']:.0f}/精力{_rstats['energy']:.0f}")
    except Exception as e:
        summary["actions"].append(f"关系数值 skip: {e}")

    # 4. 写回状态
    state.last_tick_ts = now  # 无条件推进(drift 在 mins<=0.1 时被跳过不更新 → 否则 M1 事件会重复应用)
    if not dry_run:
        save_state(sp, state)
        summary["actions"].append(f"saved → {sp}")
    else:
        summary["actions"].append("(dry-run, not saved)")

    summary["state"] = engine.state_to_dict(state)
    summary["triggers"] = engine.get_triggers(
        state,
        engine.Thresholds(
            notice=float((jiwen_cfg or {}).get("thresholds", {}).get("notice", 0.20)),
            consider=float((jiwen_cfg or {}).get("thresholds", {}).get("consider", 0.35)),
            forced=float((jiwen_cfg or {}).get("thresholds", {}).get("forced", 0.50)),
            pride_block=float((jiwen_cfg or {}).get("thresholds", {}).get("pride_block", 0.5)),
        ),
    )
    summary["description"] = engine.get_state_description(state)
    return summary


# ─── 主入口 ────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bot", help="只跑单个 bot（默认全跑）")
    p.add_argument("--dry-run", action="store_true", help="不写状态文件")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    jiwen_cfg = load_jiwen_config()
    if not jiwen_cfg:
        print("[jiwen.tick] _global.yml.jiwen 未配置，跳过", file=sys.stderr)
        return

    # enabled=false 时不烧 DeepSeek API（仍允许 --dry-run 强跑测试）
    if not jiwen_cfg.get("enabled") and not args.dry_run:
        print("[jiwen.tick] enabled=false，跳过（用 --dry-run 可强跑测试）", file=sys.stderr)
        return

    state_dir = jiwen_cfg.get("state_dir", DEFAULT_STATE_DIR)
    os.makedirs(state_dir, exist_ok=True)

    # 默认扫 channels 下所有带 access.json 的目录当 bot 列表；也可用 --bot 指定单个
    if args.bot:
        bots = [args.bot]
    else:
        bots = jiwen_cfg.get("bots") or []
        if not bots:
            _chdir = os.path.expanduser("~/.claude/channels")
            if os.path.isdir(_chdir):
                bots = sorted(d for d in os.listdir(_chdir)
                              if os.path.isfile(os.path.join(_chdir, d, "access.json")))
    print(f"[jiwen.tick] 开始 ts={int(time.time())} bots={bots} state_dir={state_dir}", file=sys.stderr)

    for bot_id in bots:
        try:
            summary = tick_one_bot(bot_id, jiwen_cfg, state_dir, dry_run=args.dry_run)
            print(f"[jiwen.tick] {bot_id}:", file=sys.stderr)
            for action in summary.get("actions", []):
                print(f"    {action}", file=sys.stderr)
            if summary.get("triggers"):
                print(f"    triggers: {summary['triggers']}", file=sys.stderr)
            if args.verbose:
                print(f"    state: {summary.get('state')}", file=sys.stderr)
                print(f"    desc: {summary.get('description')}", file=sys.stderr)
            if summary.get("error"):
                print(f"    ERROR: {summary['error']}", file=sys.stderr)
        except Exception as e:
            import traceback
            print(f"[jiwen.tick] {bot_id} 异常: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)


if __name__ == "__main__":
    main()
