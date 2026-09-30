"""
voice-bridge MCP stdio thin proxy
===================================
保持原 .mcp.json 不变，内部改为 HTTP client 转发到 127.0.0.1:7788。
HTTP 服务由 server_http.py 常驻，多个 bot 共享同一份 SenseVoice 模型（省内存）。

如果 HTTP 服务未启动，会返回清晰错误提示用户启动。
"""
import os
import sys
import json
import asyncio
import logging
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

HTTP_BASE = os.environ.get("VOICE_BRIDGE_URL", "http://127.0.0.1:7788")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "proxy.log", encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ],
)
log = logging.getLogger("voice-bridge-proxy")


async def http_post(path: str, payload: dict, timeout: float = 60.0) -> Any:
    """POST 到 HTTP 服务；拿 JSON 或 raw bytes 都 ok"""
    import httpx
    url = f"{HTTP_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            ct = r.headers.get("content-type", "")
            if "application/json" in ct:
                return r.json()
            return r.content
    except httpx.ConnectError:
        raise RuntimeError(
            f"无法连接 voice-bridge HTTP 服务 ({HTTP_BASE})。"
            "请先在 voice-bridge 目录里运行 ./start.sh 把它启动起来。"
        )


# ---------- MCP Server ----------
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

app = Server("voice-bridge")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="transcribe_audio",
            description=(
                "转写本地音频文件（OGG/WAV/MP3/FLAC）为中文文本 + 情绪 + 事件标签。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "音频文件绝对路径"},
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="transcribe_telegram_voice",
            description=(
                "下载 Telegram 语音消息（通过 file_id）并转写为文字，同时返回说话人情绪和声学事件。"
                "**收到 voice attachment 时必须立即调用**，再根据 text 内容回复用户。\n\n"
                "返回字段：text / emotion / events / language / duration_sec / latency_ms\n"
                "  emotion: HAPPY/SAD/ANGRY/NEUTRAL/FEARFUL/DISGUSTED/SURPRISED/UNKNOWN\n"
                "  events: 如 ['Laughter', 'Cry']\n\n"
                "**只传 file_id 一个参数**，bot_token 会自动从环境变量读取。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_id": {
                        "type": "string",
                        "description": "Telegram voice attachment 的 file_id",
                    },
                },
                "required": ["file_id"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "transcribe_audio":
            result = await http_post(
                "/transcribe_file", {"path": arguments["path"]}
            )
        elif name == "transcribe_telegram_voice":
            result = await http_post(
                "/transcribe_telegram",
                {
                    "file_id": arguments["file_id"],
                    "bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
                },
            )
        else:
            raise ValueError(f"未知工具: {name}")

        return [TextContent(
            type="text",
            text=json.dumps(result, ensure_ascii=False, indent=2),
        )]
    except Exception as e:
        log.exception(f"工具 {name} 失败")
        return [TextContent(
            type="text",
            text=json.dumps({"error": str(e)}, ensure_ascii=False),
        )]


async def main():
    log.info(f"voice-bridge proxy 启动，转发到 {HTTP_BASE}")
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
