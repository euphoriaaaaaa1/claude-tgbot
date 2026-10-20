"""壳的请求体解析（PLAN-bot-features 2.1，r4 审查 S2）：大小上限 → 内容类型 → 是不是 JSON 对象。

功能开关蓝图（features_routes，第一批）、试听蓝图（voice_preview_routes，第二批）调它；console_routes._json_body
在第三批改 console_routes 时一并改为调它，几个蓝图对同一个请求体的结论相同。
失败抛 ``BodyError``，``code``、``http_status``、``message`` 即 INTERFACE 3.0 第 2 条失败形状里的那几项，由各蓝图按自己的出口回包。
只依赖 Flask 的 ``request``；请求体的内容不进日志、不进异常文本。
"""
import json

from flask import request

TOO_LARGE_MESSAGE = "请求体太大"
NOT_OBJECT_MESSAGE = "请求体必须是 JSON 对象"


class BodyError(Exception):
    """请求体不合规：``too_large``（413）或 ``invalid``（422）；message 是上面两句固定文案之一。"""

    def __init__(self, code: str, http_status: int, message: str):
        super().__init__(message)
        self.code, self.http_status, self.message = code, http_status, message


def json_object(limit: int) -> dict:
    """本次请求的 JSON 对象体。先看声明的长度（超过 ``limit`` 字节 → 413，不读体），再要求内容类型是 JSON，
    再最多读 ``limit + 1`` 字节（没声明长度的分块请求同样挡得住），超了 413；解析不了、嵌套太深、顶层不是对象 → 422。"""
    length = request.content_length
    if length is not None and length > limit:
        raise BodyError("too_large", 413, TOO_LARGE_MESSAGE)
    if not request.is_json:
        raise BodyError("invalid", 422, NOT_OBJECT_MESSAGE)
    raw = request.stream.read(limit + 1)   # 到上限多一个字节就停，不把超限的体整段读进内存
    if len(raw) > limit:
        raise BodyError("too_large", 413, TOO_LARGE_MESSAGE)
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):   # ValueError 含 UnicodeDecodeError；上限内也能嵌套到解析器递归爆掉
        body = None
    if not isinstance(body, dict):
        raise BodyError("invalid", 422, NOT_OBJECT_MESSAGE)
    return body
