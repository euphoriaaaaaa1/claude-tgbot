"""暂停开关。

全局：~/.claudebotlife.pause 文件存在 → 所有 bot 静默
单 bot：/tmp/claudebotlife-pause-<bot_id> 文件存在 → 该 bot 静默
"""
import os

PAUSE_FILE_GLOBAL = os.path.expanduser("~/.claudebotlife.pause")


def _per_bot_path(bot_id: str) -> str:
    # 跨平台：放 home（与 dispatcher.ts isPaused 一致，不再用 /tmp）
    return os.path.expanduser(f"~/.claudebotlife.pause-{bot_id}")


def is_paused(bot_id: str) -> str | None:
    """返回 None 表示未暂停；返回字符串说明原因。"""
    if os.path.exists(PAUSE_FILE_GLOBAL):
        return "全局已暂停（移除 ~/.claudebotlife.pause 即可恢复）"
    p = _per_bot_path(bot_id)
    if os.path.exists(p):
        return f"bot {bot_id} 已单独暂停（移除 {p} 即可恢复）"
    return None


def pause_global():
    open(PAUSE_FILE_GLOBAL, "w", encoding="utf-8").close()


def resume_global():
    if os.path.exists(PAUSE_FILE_GLOBAL):
        os.remove(PAUSE_FILE_GLOBAL)


def pause_bot(bot_id: str):
    open(_per_bot_path(bot_id), "w", encoding="utf-8").close()


def resume_bot(bot_id: str):
    p = _per_bot_path(bot_id)
    if os.path.exists(p):
        os.remove(p)
