"""通过 claude --print 触发 novelai-skill 生图。

skill 自身负责构造 prompt + 调脚本 + 落盘到
~/resource/media/<agent>/<session>/...

我们解析 stdout 里的 MEDIA: 路径行。失败时返回 None，朋友圈仍发文字版。
"""
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from claude_cli import call_claude
import quota


NOVELAI_TRIGGER_PROMPT = """请用 novelai-skill 生成一张图，主题：

{scene_description}

要求：
- 场景按上面描述
- 不要解释过程，不要发我 prompt 文本
- 生成成功后，**最后一行严格输出**：MEDIA: <绝对路径>
- 如果生成失败，输出：FAIL: <原因>
"""


def generate_image(scene_description: str, bot_id: str) -> str | None:
    prompt = NOVELAI_TRIGGER_PROMPT.format(scene_description=scene_description)
    try:
        out = call_claude(prompt, timeout=180)
        quota.record_call("moment_image", quota.MOMENT_IMAGE_WEIGHT)
    except Exception as e:
        _log_fail(bot_id, f"调用失败: {type(e).__name__}: {e}")
        return None

    path = _extract_media_path(out)
    if not path:
        _log_fail(bot_id, f"未在 stdout 找到 MEDIA: 行。stdout 末 200 字: {out[-200:]}")
        return None
    if not os.path.exists(path):
        _log_fail(bot_id, f"MEDIA 路径文件不存在: {path}")
        return None
    return path


def _extract_media_path(claude_output: str) -> str | None:
    """从 'MEDIA: /path/to/image.jpg' 提取路径。"""
    m = re.search(r'MEDIA:\s*(/Users/[^\s]+\.(?:jpg|png|jpeg|webp))', claude_output)
    return m.group(1) if m else None


def _log_fail(bot_id: str, reason: str):
    with open("/tmp/claudebotlife-image-fail.log", "a") as f:
        f.write(f"[{datetime.now().isoformat()}] [{bot_id}] {reason}\n")
