"""
voice-bridge HTTP Server
========================
常驻 FastAPI 服务，所有 bot 共享：
  - SenseVoice STT（模型只加载一次，~2GB 内存共享）
  - Fish Audio TTS（云端 API，零额外内存）

端点：
  POST /transcribe_file          { path }                                  → STT
  POST /transcribe_telegram      { file_id, bot_token? }                   → STT
  POST /synthesize_voice         { text, voice_id, emotion?, format? }     → OGG bytes
  GET  /health                                                              → { ok, model_loaded }

运行：./start.sh  或  uvicorn server_http:app --host 127.0.0.1 --port 7788

安全闸（见下方「安全闸」段）：吃路径的端点只读白名单目录内文件，且有大小上限；
可选 VOICE_BRIDGE_TOKEN 鉴权（设了才启用，见 README「八、安全边界」）。
"""
import os
import re
import sys
import json
import time
import secrets
import asyncio
import logging
from logging.handlers import RotatingFileHandler
import tempfile
import subprocess
from pathlib import Path
from typing import Any, Optional
from contextlib import asynccontextmanager

ROOT = Path(__file__).parent
MODEL_CACHE = ROOT / "models"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
MODEL_CACHE.mkdir(exist_ok=True)

os.environ["MODELSCOPE_CACHE"] = str(MODEL_CACHE)
os.environ["HF_HOME"] = str(MODEL_CACHE / "hf")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        # 5MB 一轮，保留 3 份（http_server.log + .1 + .2 + .3）→ 上限 ~20MB
        RotatingFileHandler(
            LOG_DIR / "http_server.log",
            maxBytes=5_000_000,
            backupCount=3,
            encoding="utf-8",
        ),
        logging.StreamHandler(sys.stderr),
    ],
)
log = logging.getLogger("voice-bridge-http")


def mask(s: str) -> str:
    if not s or len(s) < 12:
        return "***"
    return f"{s[:4]}...{s[-4:]}"


# ---------- 安全闸：路径白名单 / 大小上限 / 可选令牌 ----------
# /transcribe_file 和 /send_file 都吃路径，只查 exists 等于「本机任何进程都能
# 让本服务读任意文件」。所有吃路径的端点在读文件之前必须先过 check_file_path()。


def _default_allowed_roots() -> list[str]:
    """默认允许目录——覆盖作者本机三条真实流程 + 模块自身目录（examples/ 示例音频，README 的验证命令要用）"""
    return [
        str(ROOT),                                        # 模块自身目录（含 examples/）
        os.path.expanduser("~/.claude/channels/media"),   # dispatcher 收发 Telegram 媒体
        os.path.expanduser("~/resource/media"),           # 生图产物（bot 发图）
        os.path.expanduser("~/resource/workspace"),       # 生图中间稿
        tempfile.gettempdir(),                            # voicecall 录音 in.wav（mkdtemp 建的）
    ]


def _load_allowed_roots() -> list[Path]:
    raw = os.environ.get("VOICE_BRIDGE_ALLOWED_ROOTS", "").strip()
    items = raw.split(",") if raw else _default_allowed_roots()
    roots: list[Path] = []
    for it in items:
        it = it.strip()
        if not it:
            continue
        # 先解析成 realpath：软链接指向白名单外时，解析后自然落在白名单外
        try:
            roots.append(Path(it).expanduser().resolve())
        except (OSError, ValueError, RuntimeError):
            log.warning(f"忽略无法解析的白名单目录: {it!r}")
    return roots


ALLOWED_ROOTS = _load_allowed_roots()
log.info("路径白名单: " + ", ".join(str(r) for r in ALLOWED_ROOTS))
# Telegram Bot API 单文件上限 50MB；超过就别整块读进内存了
MAX_FILE_MB = int(os.environ.get("VOICE_BRIDGE_MAX_FILE_MB") or "50")
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
# 可选令牌：设了才启用鉴权，不设则与以前行为完全一致（作者本机没设）
BRIDGE_TOKEN = os.environ.get("VOICE_BRIDGE_TOKEN", "").strip()


def check_file_path(raw_path: str) -> Path:
    """路径白名单 + 大小上限，都在读文件之前。返回解析后的路径，调用方一律用它读。

    不在白名单 → 403（错误信息回显解析后路径便于排错，不泄露文件内容）；超限 → 413。
    """
    try:
        p = Path(raw_path).expanduser().resolve()
    except (OSError, ValueError, RuntimeError):
        raise HTTPException(403, f"路径无法解析，已拒绝: {raw_path!r}")
    if not any(p.is_relative_to(root) for root in ALLOWED_ROOTS):
        raise HTTPException(403, f"文件不在允许目录内: {p}")
    try:
        size = p.stat().st_size
    except OSError:
        return p  # 不存在/不可读 → 交给原有错误路径处理，不在这里改错误码
    if size > MAX_FILE_BYTES:
        raise HTTPException(
            413,
            f"文件 {size / 1024 / 1024:.1f}MB 超过上限 {MAX_FILE_MB}MB"
            f"（可用 VOICE_BRIDGE_MAX_FILE_MB 调整）",
        )
    return p


# ---------- SenseVoice ----------
EMOTION_TAGS = {"HAPPY", "SAD", "ANGRY", "NEUTRAL", "FEARFUL", "DISGUSTED", "SURPRISED"}
EVENT_TAGS = {"BGM", "Speech", "Applause", "Laughter", "Cry"}
LANG_TAGS = {"zh", "en", "ja", "ko", "yue", "auto", "nospeech"}
TAG_RE = re.compile(r"<\|([^|]+)\|>")


def parse_sensevoice_output(raw: str, duration: float) -> dict[str, Any]:
    tags = TAG_RE.findall(raw)
    text = TAG_RE.sub("", raw).strip()
    emotion = "UNKNOWN"
    events: list[str] = []
    language = "unknown"
    for t in tags:
        if t in EMOTION_TAGS:
            emotion = t
        elif t in EVENT_TAGS:
            events.append(t)
        elif t in LANG_TAGS:
            language = t
    if duration < 1.5:
        emotion = "UNKNOWN"
    return {"text": text, "emotion": emotion, "events": events, "language": language}


_model = None
_model_lock = asyncio.Lock()


async def get_model():
    global _model
    if _model is not None:
        return _model
    async with _model_lock:
        if _model is not None:
            return _model
        log.info("加载 SenseVoice-Small 模型...")
        t0 = time.time()
        loop = asyncio.get_event_loop()
        from funasr import AutoModel

        def _load():
            return AutoModel(
                model="iic/SenseVoiceSmall",
                trust_remote_code=False,
                disable_update=True,
                device="cpu",
            )

        _model = await loop.run_in_executor(None, _load)
        log.info(f"✓ 模型加载完成 {time.time() - t0:.1f}s")
        return _model


async def transcribe_file(audio_path: str) -> dict[str, Any]:
    if not os.path.exists(audio_path):
        raise FileNotFoundError(audio_path)

    import soundfile as sf
    try:
        info = sf.info(audio_path)
        duration = info.frames / info.samplerate
    except Exception:
        duration = 0.0

    model = await get_model()
    t0 = time.time()
    loop = asyncio.get_event_loop()

    def _infer():
        return model.generate(
            input=audio_path, cache={}, language="auto",
            use_itn=True, batch_size_s=60,
        )

    result = await loop.run_in_executor(None, _infer)
    latency = int((time.time() - t0) * 1000)
    raw = result[0]["text"] if result else ""
    parsed = parse_sensevoice_output(raw, duration)
    parsed["duration_sec"] = round(duration, 2)
    parsed["latency_ms"] = latency
    log.info(
        f"STT: {duration:.1f}s → {len(parsed['text'])}字, "
        f"情绪={parsed['emotion']}, 事件={parsed['events']}, {latency}ms"
    )
    return parsed


async def transcribe_telegram(file_id: str, bot_token: str = "") -> dict[str, Any]:
    import httpx
    if not bot_token or ":" not in bot_token or bot_token.startswith("$"):
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not bot_token or ":" not in bot_token:
        raise RuntimeError("bot_token 不可用")

    log.info(f"下载 Telegram 语音 file_id={file_id[:20]}... token={mask(bot_token)}")
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(
            f"https://api.telegram.org/bot{bot_token}/getFile",
            params={"file_id": file_id},
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"getFile 失败: {data}")
        file_path = data["result"]["file_path"]
        r2 = await client.get(
            f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
        )
        r2.raise_for_status()
        suffix = Path(file_path).suffix or ".ogg"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False, dir=LOG_DIR) as tmp:
            tmp.write(r2.content)
            tmp_path = tmp.name

    try:
        return await transcribe_file(tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---------- Fish Audio TTS ----------
FISH_API_BASE = "https://api.fish.audio/v1/tts"
# 默认走免费版 s2.1-pro-free（官方称免费到 2026-07 底）。一旦它失效（窗口到期返回 4xx 等），
# synthesize_voice 会自动降级到同价付费版 s2.1-pro 并粘住，语音不中断。两者音质相同。
FISH_MODEL_DEFAULT = os.environ.get("FISH_AUDIO_MODEL", "s2.1-pro")  # 2026-08-22 用户指定直接用付费版：免费档官方称仅到 2026-07 底，再默认走它等于每次白试一轮
FISH_MODEL_FALLBACK = os.environ.get("FISH_AUDIO_MODEL_FALLBACK", "s2.1-pro")

# Fish Audio 免费档 5 并发，付费 $100 档 15 并发。留一档冗余防 429。
FISH_CONCURRENCY = int(os.environ.get("FISH_AUDIO_CONCURRENCY", "4"))
_fish_sem = asyncio.Semaphore(FISH_CONCURRENCY)

# 持久 httpx client：复用 TLS 连接，避免每次请求冷握手（首轮实测 15s→稳态）。
_fish_client = None
_fish_active_model = None  # 免费档失效后粘住付费 fallback，避免之后每次都先白试一次免费


def _get_fish_client():
    global _fish_client
    import httpx
    if _fish_client is None or _fish_client.is_closed:
        _fish_client = httpx.AsyncClient(timeout=60.0)
    return _fish_client


def fish_api_key() -> str:
    key = os.environ.get("FISH_AUDIO_API_KEY", "").strip()
    if not key:
        raise RuntimeError("FISH_AUDIO_API_KEY 未设置")
    return key


# 情绪 → Fish Audio S2 行内方括号标签（官方推荐的 word-level voice direction）
# 参考 https://docs.fish.audio/developer-guide/getting-started/quickstart
# S2 能识别中文自然语言方括号指令，无需 SSML。AI 也可以直接在 text 里写 [声音颤抖]/[叹气] 等。
EMOTION_TAG = {
    "HAPPY":     "[开心地]",
    "SAD":       "[悲伤地]",
    "ANGRY":     "[愤怒地]",
    "FEARFUL":   "[害怕地，声音颤抖]",
    "SURPRISED": "[惊讶地]",
    "DISGUSTED": "[厌恶地]",
    "NEUTRAL":   "",  # 中性不加标签，避免干扰
}


async def synthesize_voice(
    text: str,
    voice_id: str,
    emotion: str = "NEUTRAL",
    instruct: str = "",
    model: str = FISH_MODEL_DEFAULT,
) -> bytes:
    """调用 Fish Audio 合成音频，返回 OGG/Opus bytes（Telegram sendVoice 可直用）"""
    import httpx
    import ormsgpack

    key = fish_api_key()

    # Fish S2 行内指令：优先 instruct（≤12 字转成 [xxx]），超长降级到 emotion 预设短标签
    # 原因：Fish S2 对长标签识别不稳定，超过某阈值会把 [xxx] 当字面量念出来（实测 38 字必念）。
    # AI 也可以直接在 text 里写 [标签]，此处前缀与之共存。
    MAX_INSTRUCT_TAG_LEN = 12
    prefix_parts: list[str] = []
    if instruct and len(instruct) <= MAX_INSTRUCT_TAG_LEN:
        prefix_parts.append(f"[{instruct}]")
    else:
        if instruct:
            log.warning(
                f"instruct 超过 {MAX_INSTRUCT_TAG_LEN} 字 ({len(instruct)} 字)，"
                f"降级到 emotion={emotion}（内容不写日志）"
            )
        tag = EMOTION_TAG.get(emotion, "")
        if tag:
            prefix_parts.append(tag)
    final_text = ("".join(prefix_parts) + text) if prefix_parts else text

    payload = {
        "text": final_text,
        "reference_id": voice_id,   # 空则下方剔除 → Fish 用模型默认音色（空串会被 Fish 400 拒掉）
        "format": "mp3",           # 先拿 mp3 再 ffmpeg 转 opus
        "mp3_bitrate": 128,
        "chunk_length": 100,       # 更小分片 → 首字节更快返回
        "normalize": True,
        "latency": "low",          # low 比 balanced 首块再快 ~0.3s；音质略降，不满意改回 balanced
    }
    if not (voice_id or "").strip():
        payload.pop("reference_id")

    log.info(
        f"TTS: voice_id={voice_id[:8]}..., 情绪={emotion}, "
        f"文本={len(text)}字, instruct={bool(instruct)}, "
        f"in-flight={FISH_CONCURRENCY - _fish_sem._value}/{FISH_CONCURRENCY}"
    )
    body = ormsgpack.packb(payload)

    def _headers(model_id: str) -> dict:
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/msgpack",
            "model": model_id,
        }

    async def _do_post(model_id: str) -> bytes:
        client = _get_fish_client()
        r = await client.post(FISH_API_BASE, headers=_headers(model_id), content=body)
        r.raise_for_status()
        return r.content

    global _fish_client, _fish_active_model
    effective = _fish_active_model or model  # 已降级过就直接用付费，不再白试免费
    t0 = time.time()
    async with _fish_sem:  # 并发限流：Fish Audio 免费档 5 并发上限
        try:
            mp3_bytes = await _do_post(effective)
        except httpx.TransportError:
            # 持久连接可能空闲后失效 → 重建一次重试（兜底，不影响正常路径）
            if _fish_client is not None:
                await _fish_client.aclose()
            _fish_client = None
            mp3_bytes = await _do_post(effective)
        except httpx.HTTPStatusError as e:
            # 免费模型返回 4xx（最可能是免费窗口 2026-07 底到期）→ 自动降级到同价付费版并粘住，
            # 语音不中断。付费版也失败才抛（说明是别的错，不是免费到期）。
            if effective != FISH_MODEL_FALLBACK:
                log.warning(
                    f"Fish 模型 {effective} 返回 {e.response.status_code} → 自动降级到付费模型 "
                    f"{FISH_MODEL_FALLBACK}（从现在起产生费用）"
                )
                _fish_active_model = FISH_MODEL_FALLBACK
                mp3_bytes = await _do_post(FISH_MODEL_FALLBACK)
            else:
                raise
    log.info(f"  Fish 返回 mp3 {len(mp3_bytes)} bytes, {int((time.time()-t0)*1000)}ms, model={effective if _fish_active_model is None else FISH_MODEL_FALLBACK}")

    # mp3 → ogg/opus (Telegram voice 格式)
    loop = asyncio.get_event_loop()

    def _convert():
        proc = subprocess.run(
            [
                "ffmpeg", "-loglevel", "error", "-f", "mp3", "-i", "pipe:0",
                "-c:a", "libopus", "-b:a", "48k", "-application", "voip",
                "-f", "ogg", "pipe:1",
            ],
            input=mp3_bytes,
            capture_output=True,
            check=True,
        )
        return proc.stdout

    ogg_bytes = await loop.run_in_executor(None, _convert)
    log.info(f"  ffmpeg mp3→ogg/opus {len(ogg_bytes)} bytes")
    return ogg_bytes


# ---------- FastAPI ----------
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel


async def _warmup_fish():
    """启动时建立 Fish TLS 连接，避免首通电话撞 15s 冷握手。请求结果无所谓，只要握手完成。失败静默。"""
    try:
        import ormsgpack
        client = _get_fish_client()
        await client.post(
            FISH_API_BASE,
            headers={"Authorization": f"Bearer {fish_api_key()}",
                     "Content-Type": "application/msgpack", "model": FISH_MODEL_DEFAULT},
            content=ormsgpack.packb({"text": "。", "reference_id": "", "format": "mp3"}),
            timeout=20.0,
        )
        log.info("Fish TLS 预热完成")
    except Exception as e:
        log.info(f"Fish 预热跳过（TLS 可能已建立）: {type(e).__name__}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("voice-bridge HTTP 启动，预热 SenseVoice + Fish...")
    asyncio.create_task(get_model())
    asyncio.create_task(_warmup_fish())
    yield
    global _fish_client
    if _fish_client is not None:
        await _fish_client.aclose()
    log.info("voice-bridge HTTP 关闭")


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    """VOICE_BRIDGE_TOKEN 设了才启用：除 /health 外所有端点要 Authorization: Bearer <token>。
    不设时直接放行——与加鉴权之前的行为完全一致。"""
    if BRIDGE_TOKEN and request.url.path != "/health":
        auth = request.headers.get("authorization", "")
        got = auth[7:] if auth.startswith("Bearer ") else ""
        # 常数时间比较，别用 == （防时序侧信道）；token 不落日志
        if not got or not secrets.compare_digest(got.encode(), BRIDGE_TOKEN.encode()):
            return JSONResponse(
                {"detail": "unauthorized: 需要 Authorization: Bearer <VOICE_BRIDGE_TOKEN>"},
                status_code=401,
            )
    return await call_next(request)


class TranscribeFileReq(BaseModel):
    path: str


class TranscribeTelegramReq(BaseModel):
    file_id: str
    bot_token: Optional[str] = ""


class SynthesizeReq(BaseModel):
    text: str
    voice_id: str
    emotion: Optional[str] = "NEUTRAL"
    instruct: Optional[str] = ""
    model: Optional[str] = FISH_MODEL_DEFAULT


class SendVoiceReq(BaseModel):
    """TTS + Telegram sendVoice 一体化端点 —— 绕开 Bun fetch 对 multipart+proxy 的 bug"""
    bot_token: str
    chat_id: str
    text: str
    voice_id: str
    emotion: Optional[str] = "NEUTRAL"
    instruct: Optional[str] = ""
    model: Optional[str] = FISH_MODEL_DEFAULT
    reply_to_message_id: Optional[int] = None


class SendFileReq(BaseModel):
    """Telegram sendPhoto / sendDocument —— 绕开 Bun fetch 对 multipart+proxy 的 bug"""
    bot_token: str
    chat_id: str
    file_path: str
    kind: str  # "photo" | "document"
    caption: Optional[str] = None
    reply_to_message_id: Optional[int] = None


@app.get("/health")
async def health():
    return {"ok": True, "model_loaded": _model is not None}


@app.post("/transcribe_file")
async def api_transcribe_file(req: TranscribeFileReq):
    path = check_file_path(req.path)  # 白名单 403 / 超限 413，都在读文件之前
    try:
        return await transcribe_file(str(path))
    except Exception as e:
        log.exception("transcribe_file 失败")
        raise HTTPException(500, str(e))


@app.post("/transcribe_telegram")
async def api_transcribe_telegram(req: TranscribeTelegramReq):
    try:
        return await transcribe_telegram(req.file_id, req.bot_token or "")
    except Exception as e:
        log.exception("transcribe_telegram 失败")
        raise HTTPException(500, str(e))


@app.post("/synthesize_voice")
async def api_synthesize(req: SynthesizeReq):
    try:
        audio = await synthesize_voice(
            req.text, req.voice_id, req.emotion or "NEUTRAL",
            req.instruct or "", req.model or FISH_MODEL_DEFAULT,
        )
        return Response(content=audio, media_type="audio/ogg")
    except Exception as e:
        log.exception("synthesize_voice 失败")
        raise HTTPException(500, str(e))



async def _tg_post_multipart(url, files, data, timeout):
    """发 Telegram 的 multipart POST，只对「连接根本没建立」类错误重试。
    经代理的 CONNECT/TLS 握手抖动（Clash 偶发）= 请求没出门，重发绝对安全；
    读超时/响应中断可能已送达，不重试——重发语音用户会收到两条一样的。"""
    import httpx
    last = None
    for attempt in (1, 2, 3):
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=True) as client:
                return await client.post(url, files=files, data=data)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError) as e:
            last = e
            log.warning(f"  Telegram POST 连接失败(第{attempt}次, {type(e).__name__})，重试")
            await asyncio.sleep(0.5 * attempt)
    raise last


@app.post("/send_voice")
async def api_send_voice(req: SendVoiceReq):
    """TTS + Telegram sendVoice（绕开 Bun fetch 的 multipart+proxy bug）"""
    import httpx
    try:
        ogg = await synthesize_voice(
            req.text, req.voice_id, req.emotion or "NEUTRAL",
            req.instruct or "", req.model or FISH_MODEL_DEFAULT,
        )
        t0 = time.time()
        # httpx 自动读取 HTTPS_PROXY / HTTP_PROXY / NO_PROXY 环境变量
        files = {"voice": ("voice.ogg", ogg, "audio/ogg")}
        data: dict[str, Any] = {"chat_id": str(req.chat_id)}
        if req.reply_to_message_id:
            data["reply_parameters"] = json.dumps({"message_id": req.reply_to_message_id})
        r = await _tg_post_multipart(
            f"https://api.telegram.org/bot{req.bot_token}/sendVoice",
            files, data, 30.0)
        tg_ms = int((time.time() - t0) * 1000)
        if r.status_code != 200:
            log.error(f"Telegram sendVoice {r.status_code}: {r.text[:200]}")
            raise HTTPException(502, f"Telegram {r.status_code}: {r.text[:200]}")
        resp = r.json()
        if not resp.get("ok"):
            raise HTTPException(502, f"Telegram: {resp}")
        msg_id = resp["result"]["message_id"]
        log.info(f"  Telegram sendVoice OK msg_id={msg_id}, {tg_ms}ms")
        return {"message_id": msg_id}
    except HTTPException:
        raise
    except Exception as e:
        log.exception("send_voice 失败")
        raise HTTPException(500, str(e))


@app.post("/send_file")
async def api_send_file(req: SendFileReq):
    """Telegram sendPhoto / sendDocument（绕开 Bun fetch multipart+proxy bug）"""
    import httpx
    if req.kind not in ("photo", "document"):
        raise HTTPException(400, f"kind must be photo or document, got {req.kind}")
    fp = check_file_path(req.file_path)  # 白名单 403 / 超限 413，都在读文件之前
    if not fp.exists():
        raise HTTPException(404, f"file not found: {req.file_path}")
    field = "photo" if req.kind == "photo" else "document"
    method = "sendPhoto" if req.kind == "photo" else "sendDocument"
    ext = fp.suffix.lower()
    mime_map = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".webp": "image/webp", ".mp4": "video/mp4",
        ".pdf": "application/pdf",
    }
    mime = mime_map.get(ext, "application/octet-stream")
    try:
        t0 = time.time()
        size = fp.stat().st_size
        with open(fp, "rb") as fh:
            payload = fh.read()
        # 大文件流式上传需要单独一条 Transfer-Encoding；Telegram 50MB 以内一次性上传足够
        files = {field: (fp.name, payload, mime)}
        data: dict[str, Any] = {"chat_id": str(req.chat_id)}
        if req.caption:
            data["caption"] = req.caption
        if req.reply_to_message_id:
            data["reply_parameters"] = json.dumps({"message_id": req.reply_to_message_id})
        r = await _tg_post_multipart(
            f"https://api.telegram.org/bot{req.bot_token}/{method}",
            files, data, 120.0)
        tg_ms = int((time.time() - t0) * 1000)
        if r.status_code != 200:
            log.error(f"Telegram {method} {r.status_code}: {r.text[:200]}")
            raise HTTPException(502, f"Telegram {r.status_code}: {r.text[:200]}")
        resp = r.json()
        if not resp.get("ok"):
            raise HTTPException(502, f"Telegram: {resp}")
        msg_id = resp["result"]["message_id"]
        log.info(f"  Telegram {method} OK msg_id={msg_id}, {size} bytes, {tg_ms}ms")
        return {"message_id": msg_id}
    except HTTPException:
        raise
    except Exception as e:
        log.exception("send_file 失败")
        raise HTTPException(500, str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=7788, log_level="warning")
