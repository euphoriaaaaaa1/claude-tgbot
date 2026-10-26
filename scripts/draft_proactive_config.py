#!/usr/bin/env python3
"""新建 bot 后起草主动消息相关的配置，用户过目后再写入（pub-draft 接口）。

claude 后端（缺省，我们自己的 bot）起草四项：人设摘要 persona_summary、主动说话条件 speaking_threshold、
思念值变化规则 jiwen.delta_hints、睡觉时段 sleep_hours。dsh 后端只起草 sleep_hours：人设经 dsh 适配器取，写入也经它。

用法：
  python3 scripts/draft_proactive_config.py <bot_id> [--backend claude|dsh] [--out 路径] [--force]
      按人设请 DeepSeek 起草，存成草稿（0600）；落点已有文件时要带 --force 才覆盖
  python3 scripts/draft_proactive_config.py <bot_id> [--backend claude|dsh] --apply <草稿路径>
      过目同意后把草稿写进这只 bot 的配置

起草会把这只 bot 的人设全文发给配置的模型地址（claude：<bot_channel_path>/CLAUDE.md，连同配置里的几项基本
信息；dsh：适配器 GET /bots/<id>/persona 回的 text）。地址与模型取 _global.yml 的 jiwen.delta_llm，环境变量
PROACTIVE_DRAFT_BASE_URL 可覆盖地址（只许 https 或回环地址上的 http，不许含反斜杠、@、空白）；请求不跟随
重定向，http 地址一律直连，https 地址按配置的 proxy 走。DeepSeek 密钥只经 config_loader.get_deepseek_api_key
取、只进请求头；dsh 适配器的地址与令牌只经 adapters.dsh_http.DshHttpAdapter 取用。屏幕上只有状态、字数、段数
与错误类别，不出现密钥、令牌、人设原文、草稿正文与服务端回包。只有 --apply 才写配置：claude 走生活设定同一条
写入路径（锁、备份、原子写；jiwen 下只改 delta_hints），dsh 经适配器 PUT /bots/<id>/life 只写 sleep_hours。

退出码（接口第三节「错误契约」是唯一权威）：
  0 成功；
  2 用法错误，或不发请求就能判定的问题：找不到配置或人设、人设为空、未配置密钥、地址不合规、人设超过 256 KiB；
    落点是目录、所在目录不存在、在配置目录/全局配置目录/密钥目录之内、已存在而没带 --force；草稿读不出来；
    dsh 没配令牌、找不到这只 bot、这只 bot 没有生活设定文件；
  3 请求模型地址失败（对方回 30x 也算）；
  4 回包或草稿不合规，配置不改：回包缺项、某项为空、sleep_hours 不合规、被截断（原文写进落点），回包没有任何
    正文（不写落点）；--apply 的草稿缺项、为空、不是 YAML、超长或含控制字符、sleep_hours 不合规；dsh 对
    --apply 回 422；
  5 读写 dsh 失败：连不上、超时、5xx、回包形状不对；
  1 其他意外（只报异常类名）。
"""
import argparse
import contextlib
import io
import ipaddress
import json
import os
import re
import shlex
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

import config_loader
import deepseek_client
import life_settings
from adapters.base import AdapterError, AdapterUnavailable, valid_bot_id
from adapters.dsh_http import DshHttpAdapter, ServerError
from jiwen import deepseek_delta

BASE_URL_ENV = "PROACTIVE_DRAFT_BASE_URL"
BACKENDS = ("claude", "dsh")
MAX_TOKENS = 8000      # 2026-10-25 主会话一次性起草实测够用（深度思考也算在输出里）
TIMEOUT_FLOOR = 180    # 配置里的超时是给积温一句 JSON 调的；起草要写几段长文，至少给这么久
PERSONA_MAX_BYTES = 256 * 1024   # 公开版加固：单份人设超过这个大小就不外发
# 草稿里的三段文字：(草稿里的写法, 生活设定的字段键)
ITEMS = (("persona_summary", "persona_summary"), ("speaking_threshold", "speaking_threshold"),
         ("jiwen.delta_hints", "jiwen_delta_hints"))
SLEEP_KEY = "sleep_hours"
SLEEP_MAX_ITEMS = 6    # 接口：0 到 6 项；生活设定字段表与 dsh 的上限都是 6
# 随人设一起发过去的本 bot 基本信息（只取这几项，不带注册用的键）
BASICS_KEYS = ("display_name", "user_name", "user_address", "bio", "city", "sleep_hours", "speaking_threshold")
_FENCE_RE = re.compile(r"```[ \t]*(?:ya?ml)?[ \t]*\n(.*?)```", re.S | re.I)
# 地址的 netloc 里出现方括号时只许这一种形状：方括号恰好包着 IPv6 字面量，后面只许跟端口
_BRACKETED_NETLOC_RE = re.compile(r"\[([0-9A-Fa-f:.]+)\](?::[0-9]+)?")

PROMPT = """你在给一个 Telegram 陪聊角色写四项配置。第 1、2、4 项给「要不要主动找对方说话」的判官用（角色睡着时不主动开口），第 3 项给判断角色情绪变化（思念值）的模型用。
后面附有这个角色的基本信息与人设全文。除第 4 项在人设没写作息时可以推断外，只用人设里有的信息，不编造；前三项都不超过 4000 字。

1. persona_summary：200 到 400 字，概括性格、背景、与对方的关系、说话方式。
2. speaking_threshold：判官可以直接使用的开口准则，写成一段完整的文字，不要只写一句，也不要写成列表。写清什么情况下主动找对方、什么时候别打扰（作息、对方在忙、刚聊过不久、对方一直没回时如何收敛），按真人的逻辑判断。书面化表述，不写示例句。
3. jiwen.delta_hints：这个角色的情绪变化判断准则。第一段用一两句话说明角色身份与和对方的关系，并写明「user 是内部角色标识，不得在最终台词中原样说出」；空一行后逐条列出情形，每条一行、以「- 」开头，覆盖对方长时间不回、冷淡敷衍、问候或关心、情绪低落或身体不适、主动亲昵、正常道晚安，写明每种情形下 connection（想找对方说话的需求）、valence（对对方的情绪好坏）、pride（倔强、要面子的程度）、arousal（情绪的激动程度）各自升高还是回落、幅度大小，要符合这个角色的性格。
4. sleep_hours：这个角色平时的睡觉时段，写成 YAML 列表，每项是 HH:MM-HH:MM，小时与分钟都写两位数，可以跨过午夜，一般一项。人设写了作息就照人设；没写就按角色的身份与生活方式推断一个合理的时段。

称呼对方用人设里的称呼；人设里若用 {{user}} 指代对方，就照样写 {{user}}。
只输出一段 YAML：顶层键 persona_summary、speaking_threshold、jiwen（jiwen 下只有 delta_hints）、sleep_hours；前三项的值都是字符串，多行文字用 | 块；sleep_hours 的值是列表。不要代码块标记，也不要任何解释文字。
"""

# dsh 的 bot 只起草睡觉时段：单独的短提示词，不带其他三项
SLEEP_PROMPT = """你在给一个 Telegram 陪聊角色写睡觉时段配置，给「要不要主动找对方说话」的判官用：角色睡着时不主动开口。
后面附有这个角色的人设全文。

写这个角色平时的睡觉时段：人设写了作息就照人设；没写就按角色的身份与生活方式推断一个合理的时段。每项是 HH:MM-HH:MM，小时与分钟都写两位数，可以跨过午夜，一般一项。
只输出一段 YAML：顶层只有一个键 sleep_hours，值是列表。不要代码块标记，也不要任何解释文字。
"""


class _Fail(Exception):
    """按退出码收尾。message 只含状态与错误类别，不含密钥、令牌、人设、草稿与回包的内容。"""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _bot_config(bot_id: str) -> dict:
    """这只 bot 的配置（解析后的键值表）；配置目录与 bot_id 校验同生活设定。"""
    try:
        _text, cfg = life_settings._read(life_settings._config_path(bot_id))
    except life_settings.LifeSettingsError as e:
        if e.code == "unknown_bot":
            raise _Fail(2, "找不到这只 bot 的配置")
        raise _Fail(2, "这只 bot 的配置读不出来：" + e.message)
    return cfg


def _load_yaml(text):
    """解析成键值表；不是 YAML、不是键值表都返回 None。"""
    try:
        data = yaml.safe_load(text)
    except Exception:   # PyYAML 对坏输入不只抛 YAMLError（如不存在的日期会抛 ValueError）
        return None
    return data if isinstance(data, dict) else None


def _texts(data: dict):
    """三段文字（按 ITEMS 的顺序）都是去掉首尾空白后不为空的字符串才返回它们，否则 None。"""
    jiwen = data.get("jiwen")
    values = (data.get("persona_summary"), data.get("speaking_threshold"),
              jiwen.get("delta_hints") if isinstance(jiwen, dict) else None)
    return values if all(isinstance(v, str) and v.strip() for v in values) else None


def _sleep_ok(value) -> bool:
    """0 到 6 项的列表，每项整串是 HH:MM-HH:MM（小时两位，可跨午夜）：生活设定与 dsh 都收这种写法。
    用 fullmatch：_HHMM_RANGE_RE 以 $ 结尾，单用 match 会放过尾随的换行；再要求 ASCII：它的数字类也认 Unicode 数字。"""
    return (isinstance(value, list) and len(value) <= SLEEP_MAX_ITEMS
            and all(isinstance(v, str) and v.isascii() and life_settings._HHMM_RANGE_RE.fullmatch(v) for v in value))


def _read_draft(path: str):
    """草稿解析成键值表；读不出来按退出码 2 收尾，不是 UTF-8、不是 YAML 或不是键值表返回 None（调用方按 4 收尾）。"""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except UnicodeDecodeError:
        return None
    except OSError as e:
        raise _Fail(2, "草稿读不出来：%s" % type(e).__name__)
    return _load_yaml(text)


_EXISTS_MESSAGE = "落点在起草期间被建出了文件，没有覆盖；确认要覆盖就加 --force 重跑"


def _write_private(path: str, text: str, force: bool) -> None:
    """写成只有自己能读写的文件：同目录临时文件（mkstemp 建出来就是 0600）写好再落地。带 --force 用改名把原有同名
    文件连同权限整个换掉；没带就用硬链接落地：起草要等几分钟，这期间若有人在落点建了文件，link 原子地失败，不会顶掉它。
    盘不支持硬链接（link 报 EPERM、ENOTSUP 等）时退到 O_CREAT|O_EXCL 直接建落点再写，同样绝不覆盖已有文件。"""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".proactive-draft-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        if force:
            os.replace(tmp, path)
        else:
            try:
                os.link(tmp, path)
            except FileExistsError:
                raise _Fail(2, _EXISTS_MESSAGE) from None
            except OSError:
                _create_exclusive(path, text)
    finally:
        with contextlib.suppress(OSError):   # 改名之后它已不在；硬链接落地之后它是多出来的那个名字
            os.unlink(tmp)


def _create_exclusive(path: str, text: str) -> None:
    """落点不存在才建（O_EXCL，0600；落点是符号链接也算已存在，不跟过去）再写入；已存在按退出码 2 收尾。
    这条路不是原子落地：写到一半出错就删掉这个自己刚建的文件，免得留下半份草稿挡住重跑。"""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise _Fail(2, _EXISTS_MESSAGE) from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise


def _protected_dirs() -> list:
    """不许放草稿的目录：配置目录（那里的 .yml 会被当成 bot 配置读）、全局配置文件与密钥文件所在的目录
    （两者都可经环境变量指到配置目录以外），以及本仓库约定的密钥目录 ``~/.claude/secrets``（面板口令、语音桥
    令牌都放那里）。路径取自 config_loader 现有的取路径函数。"""
    return [config_loader.config_dir(), os.path.dirname(config_loader.global_cfg_path()) or ".",
            os.path.dirname(config_loader.secrets_path()) or ".",
            os.path.expanduser(os.path.join("~", ".claude", "secrets"))]


def _in_protected_dir(out: str) -> bool:
    """落点实际要建在的那个目录及它的每一级上级，有没有哪一个就是受保护的目录。
    实际要建在的目录 = realpath(dirname(out))：系统解析父路径时先跟符号链接、再处理 ..，realpath 也是这样。
    不用 realpath(out)：落点本身若是指向别处的符号链接，os.replace 换掉的是链接本身，文件仍建在链接所在的目录；
    也不先 abspath：它按字面消掉 ..，链接后面跟 .. 时与系统解析的结果不同。
    比文件身份（同 os.path.samefile：设备号加 inode），不比路径字符串：realpath 不规范大小写，macOS 默认卷上
    Configs 就是 configs。"""
    protected = []
    for d in _protected_dirs():
        with contextlib.suppress(OSError):   # 不存在的目录里放不进文件，不必比
            protected.append(os.stat(d))
    d = os.path.realpath(os.path.dirname(out))   # dirname 为空串时 realpath 给当前目录
    while True:
        try:
            st = os.stat(d)
        except OSError:
            st = None
        if st is not None and any(os.path.samestat(st, p) for p in protected):
            return True
        parent = os.path.dirname(d)
        if parent == d:
            return False
        d = parent


def _check_out(out: str, force: bool) -> None:
    """起草的落点：要指向一个文件、所在目录已存在、不在受保护的目录之内，已有文件时要带 --force。
    都在发请求之前判定，不动任何文件。"""
    if os.path.isdir(out) or not os.path.isdir(os.path.dirname(os.path.abspath(out))):
        raise _Fail(2, "--out 要指向一个文件，而且它所在的目录要已经存在")
    if _in_protected_dir(out):
        raise _Fail(2, "草稿不能放在配置目录、全局配置或密钥文件所在的目录里")
    if os.path.lexists(out) and not force:
        raise _Fail(2, "落点已经有文件了，没有覆盖；确认要覆盖就加 --force")


def _base_url_ok(url: str) -> bool:
    """只许 https，或回环地址上的 http（本机中转、测试用的假服务）。含反斜杠、@、空白或控制字符的一律不收：
    各家解析器对它们的理解不一致（反斜杠当路径分隔、@ 前面当用户信息），同一串可能连到另一台主机去。"""
    if "\\" in url or "@" in url or any(c.isspace() or not c.isprintable() for c in url):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        host, _port = parts.hostname or "", parts.port   # 端口写错时 .port 抛 ValueError
    except ValueError:
        return False
    # urlsplit 只取方括号里那段当主机，http.client 却把 [::1].example 整串当主机名去解析：
    # 方括号必须恰好包着一个 IPv6 字面量（后面只许跟端口），两边认的才是同一台主机
    if "[" in parts.netloc or "]" in parts.netloc:
        m = _BRACKETED_NETLOC_RE.fullmatch(parts.netloc)
        try:
            ipaddress.IPv6Address(m.group(1) if m else "")
        except ValueError:
            return False
    if parts.scheme == "https":
        return bool(host)
    if parts.scheme != "http":
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _deepseek_key(delta_cfg) -> str:
    """取出 DeepSeek 密钥；取不到按退出码 2 收尾。config_loader 会在 stderr 写 [secrets] 提示（权限过宽、旧写法
    等）：本脚本的 stdout/stderr 有固定形状（成功与请求失败都按整行断言），成功时不给这些提示让路；确实取不到
    密钥时才把它们附在错误里，让人知道该改哪里。提示里只有路径与权限，不含密钥。"""
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        key = config_loader.get_deepseek_api_key(delta_cfg)
    if key:
        return key
    detail = " ".join(line.strip() for line in buf.getvalue().splitlines() if line.strip())
    raise _Fail(2, "未配置 DeepSeek 密钥（configs/_secrets.yml 的 deepseek.api_key）" + ("；" + detail if detail else ""))


def _llm_settings():
    """DeepSeek 的 (地址, 模型, 密钥, 代理, 超时)；配置不全或地址不合规按退出码 2 收尾（都不发请求）。"""
    try:
        g = config_loader.load_global()
    except Exception as e:
        raise _Fail(2, "全局配置读不出来：%s" % type(e).__name__)
    jiwen = g.get("jiwen") if isinstance(g, dict) else None
    delta_cfg = jiwen.get("delta_llm") if isinstance(jiwen, dict) else None
    delta_cfg = delta_cfg if isinstance(delta_cfg, dict) else {}
    model = delta_cfg.get("model") or deepseek_delta.DEFAULT_MODEL
    if not isinstance(model, str):
        raise _Fail(2, "全局配置里 jiwen.delta_llm.model 不是文字")
    key = _deepseek_key(delta_cfg)
    base_url = os.environ.get(BASE_URL_ENV) or delta_cfg.get("base_url") or deepseek_delta.DEFAULT_BASE_URL
    if not isinstance(base_url, str) or not _base_url_ok(base_url):
        raise _Fail(2, "DeepSeek 地址不合规：只许 https，或回环地址上的 http，且不能含反斜杠、@ 或空白")
    proxy = delta_cfg.get("proxy", deepseek_delta.DEFAULT_PROXY) or None   # 缺省同积温（jiwen/tick.py）
    try:
        timeout = max(float(delta_cfg.get("timeout", deepseek_delta.DEFAULT_TIMEOUT)), TIMEOUT_FLOOR)
    except (TypeError, ValueError):
        timeout = TIMEOUT_FLOOR
    return base_url, model, key, proxy, timeout


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随重定向：30x 交给默认的错误处理，原样抛 HTTPError。跟随会把带密钥的请求重发到别处。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post_json(url: str, headers: dict, body: dict, proxy, timeout: float) -> dict:
    """POST 一份 JSON、读回 JSON。用自建的 opener：不跟随重定向；http 地址（前面已限定只许回环）一律直连，
    不看配置与环境变量里的代理；https 照旧，配了 proxy 走它，没配按环境变量。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    if urllib.parse.urlsplit(url).scheme == "http":
        proxy_handler = urllib.request.ProxyHandler({})
    elif proxy:
        proxy_handler = urllib.request.ProxyHandler({"https": proxy})
    else:
        proxy_handler = urllib.request.ProxyHandler()
    opener = urllib.request.build_opener(_NoRedirect, proxy_handler)
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _ask(prompt: str, base_url: str, model: str, key: str, proxy, timeout: float):
    """发一次请求，返回 (正文, stop_reason)。请求失败按退出码 3；回包没有任何正文按 4（不写落点）。"""
    body = deepseek_client.build_body(model, prompt, MAX_TOKENS)
    headers = {"Content-Type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"}
    try:
        resp = _post_json(base_url.rstrip("/") + "/v1/messages", headers, body, proxy, timeout)
    except urllib.error.HTTPError as e:
        raise _Fail(3, "请求 DeepSeek 失败：HTTP %d" % e.code)
    except Exception as e:   # 只报类名：异常文字可能带出请求或回包的内容
        raise _Fail(3, "请求 DeepSeek 失败：%s" % type(e).__name__)
    try:
        return deepseek_client.extract_text(resp)
    except deepseek_client.LLMError:
        raise _Fail(4, "DeepSeek 回包里没有任何正文，没有写草稿，配置没有改动")


def _reply_values(reply: str, backend: str):
    """回包里要求的几项都合规才返回 (三段文字, sleep_hours)，dsh 没有三段文字（None）；否则返回 None。"""
    m = _FENCE_RE.search(reply)   # 回包若用代码块包着 YAML，取代码块里面的
    data = _load_yaml(m.group(1) if m else reply)
    if data is None or not _sleep_ok(data.get(SLEEP_KEY)):
        return None
    if backend == "dsh":
        return None, data[SLEEP_KEY]
    texts = _texts(data)
    return (texts, data[SLEEP_KEY]) if texts is not None else None


def _basics(cfg: dict) -> str:
    picked = {k: cfg[k] for k in BASICS_KEYS if cfg.get(k) not in (None, "", [], {})}
    return yaml.safe_dump(picked, allow_unicode=True, sort_keys=False) if picked else "（配置里没有）\n"


def _claude_persona(cfg: dict) -> str:
    channel = cfg.get("bot_channel_path")
    path = os.path.join(os.path.expanduser(channel), "CLAUDE.md") if isinstance(channel, str) and channel else ""
    try:
        with open(path, encoding="utf-8") as f:
            persona = f.read()
    except (OSError, UnicodeDecodeError):
        raise _Fail(2, "找不到这只 bot 的人设文件（<bot_channel_path>/CLAUDE.md）")
    if not persona.strip():
        raise _Fail(2, "这只 bot 的人设文件是空的")
    return persona


def _check_persona_size(persona: str, what: str) -> None:
    """公开版加固：单份人设超过 256 KiB 不发出去，也不写草稿。"""
    if len(persona.encode("utf-8")) > PERSONA_MAX_BYTES:
        raise _Fail(2, "%s超过 %d KiB，没有发出去" % (what, PERSONA_MAX_BYTES // 1024))


# ── dsh 后端：只经 DshHttpAdapter（回环地址、令牌现读只进请求头、不跟随重定向、异常文案固定）──────────────

def _dsh_adapter() -> DshHttpAdapter:
    """地址取 CONSOLE_DSH_ADAPTER_URL、令牌文件取 CONSOLE_ADAPTER_TOKEN_FILE（缺省同管理台）；构造期就拒绝非回环地址。"""
    try:
        return DshHttpAdapter()
    except ValueError:
        raise _Fail(2, "dsh 适配器地址不合规：只许回环地址上的 http")


def _dsh_fail(e: Exception, writing: bool) -> _Fail:
    """适配器的错误 → 退出码：没配令牌、找不到这只 bot、这只 bot 没有生活设定文件 → 2；写入时对方回 422 → 4；
    其他（连不上、超时、5xx、回包形状不对）→ 5。AdapterError 的 message 是对方回的文字，不用；
    AdapterUnavailable 的 message 是 dsh_http 自己写死的文案（不含令牌与回包），可以照用。"""
    if isinstance(e, AdapterError):
        if e.code == "unsupported" or e.http_status == 404:
            return _Fail(2, "dsh 那边找不到这只 bot 或它没有生活设定文件")
        if writing and e.http_status == 422:
            return _Fail(4, "dsh 没收下这份草稿（不合规），配置没有改动")
        return _Fail(5, "读写 dsh 失败：HTTP %d" % e.http_status)
    if e.status == "unconfigured":
        return _Fail(2, "没配 dsh 适配器令牌（CONSOLE_ADAPTER_TOKEN_FILE 指向的文件）")
    if isinstance(e, ServerError):
        return _Fail(5, "读写 dsh 失败：对方回了 5xx（%s）" % e.message)
    return _Fail(5, "读写 dsh 失败：%s" % e.message)


def _dsh_ready(adapter: DshHttpAdapter, bot_id: str) -> None:
    """起草前确认 dsh 那边这只 bot 存在、且有生活设定文件（没有生活设定的 bot 写完也无处可存）。
    GET /bots/<id> 是对方最小的 bot 形状；回体不是键值表按「形状不对」→ 5。"""
    try:
        adapter._request("GET", adapter._bot_path(bot_id, ""))
    except (AdapterError, AdapterUnavailable) as e:
        raise _dsh_fail(e, writing=False)
    try:
        adapter.get_life(bot_id)
    except AdapterError as e:
        if e.http_status == 404:
            raise _Fail(2, "dsh 说这只 bot 没有生活设定文件")
        raise _dsh_fail(e, writing=False)
    except AdapterUnavailable as e:
        raise _dsh_fail(e, writing=False)


def _dsh_persona(adapter: DshHttpAdapter, bot_id: str) -> str:
    """人设全文：适配器 GET /bots/<id>/persona 回包的 text。用 get_persona_text 而不是 get_persona：
    后者还要求回包带 version，这里用不到，接口第 4 条也只约定了 text。text 不是字符串由适配器按形状不对抛出（→ 5）。"""
    try:
        text = adapter.get_persona_text(bot_id)
    except (AdapterError, AdapterUnavailable) as e:
        raise _dsh_fail(e, writing=False)
    if not text.strip():
        raise _Fail(2, "这只 bot 的人设是空的")
    return text


# ── 起草与写入 ────────────────────────────────────────────────────────────────

def _abs_out(path: str) -> str:
    """落点一律折成绝对路径：缺省落点相对当前目录（--out 的相对路径也按当前目录解析），屏幕上也打绝对路径。"""
    return os.path.abspath(os.path.expanduser(path))


def draft(bot_id: str, backend: str, out, force: bool) -> None:
    if backend == "dsh":
        if not valid_bot_id(bot_id):
            raise _Fail(2, "bot_id 不合规：只能用字母、数字、点、下划线、连字符")
        out = _abs_out(out or "dsh-%s-proactive-draft.yml" % bot_id)
        _check_out(out, force)
        adapter = _dsh_adapter()
        _dsh_ready(adapter, bot_id)
        persona = _dsh_persona(adapter, bot_id)
        _check_persona_size(persona, "这只 bot 的人设")
        llm = _llm_settings()
        prompt = SLEEP_PROMPT + "\n【这个角色的人设全文】\n" + persona
    else:
        cfg = _bot_config(bot_id)
        out = _abs_out(out or "%s-proactive-draft.yml" % bot_id)   # bot_id 已过校验，只含字母、数字、_、-
        _check_out(out, force)
        persona = _claude_persona(cfg)
        _check_persona_size(persona, "这只 bot 的人设文件")
        llm = _llm_settings()
        prompt = PROMPT + "\n【这个角色的基本信息】\n" + _basics(cfg) + "\n【这个角色的人设全文】\n" + persona
    reply, stop_reason = _ask(prompt, *llm)

    parsed = None if stop_reason == "max_tokens" else _reply_values(reply, backend)
    if parsed is None:
        _write_private(out, reply + "\n", force)
        why = "被截断了" if stop_reason == "max_tokens" else "不是要求的几项都合规的 YAML"
        raise _Fail(4, "DeepSeek 回包%s，原文已写到 %s，配置没有改动" % (why, out))
    texts, sleep = parsed
    if texts is None:
        _write_private(out, "# dsh/%s 的睡觉时段草稿：过目、改好后用 --backend dsh --apply 写入\n" % bot_id
                       + life_settings._render_block(SLEEP_KEY, sleep), force)
    else:
        summary, threshold, hints = texts
        _write_private(out, "# %s 的主动消息四项草稿：过目、改好后用 --apply 写入\n" % bot_id
                       + life_settings._render_block("persona_summary", summary)
                       + life_settings._render_block("speaking_threshold", threshold)
                       + life_settings._render_block("jiwen", {"delta_hints": hints})
                       + life_settings._render_block(SLEEP_KEY, sleep), force)
        limits = {f["key"]: f.get("max_len") for f in life_settings.field_table()}
        for (name, field_key), value in zip(ITEMS, texts):
            over = limits.get(field_key) and len(value) > limits[field_key]
            print("%s：%d 字%s" % (name, len(value), "（超过上限，写入前要删短）" if over else ""))
    print("%s：%d 段" % (SLEEP_KEY, len(sleep)))
    if texts is None:   # dsh：只写 --apply 会走缺省的 claude 分支报错，给出带 --backend dsh、可以照抄的完整写法
        usage = "python3 scripts/draft_proactive_config.py %s --backend dsh --apply %s" % (shlex.quote(bot_id),
                                                                                         shlex.quote(out))
        print("草稿已写到 %s，过目同意后这样写入：%s" % (out, usage), file=sys.stderr)
    else:
        print("草稿已写到 %s，过目同意后再用 --apply 写入" % out, file=sys.stderr)


def apply_claude(bot_id: str, path: str) -> None:
    _bot_config(bot_id)
    data = _read_draft(path)
    values = _texts(data) if data is not None else None
    if values is None:
        raise _Fail(4, "草稿不是三项（persona_summary、speaking_threshold、jiwen.delta_hints）都有内容的 YAML，配置没有改动")
    changes = {field_key: v for (_name, field_key), v in zip(ITEMS, values)}
    if SLEEP_KEY in data:   # sleep_hours 可选：草稿里有就校验后一起写，没有就不动配置里的
        if not _sleep_ok(data[SLEEP_KEY]):
            raise _Fail(4, "草稿里的 sleep_hours 不合规（要 0 到 6 项，每项 HH:MM-HH:MM），配置没有改动")
        changes[SLEEP_KEY] = data[SLEEP_KEY]
    try:
        life_settings.set_life(bot_id, changes)
    except life_settings.LifeSettingsError as e:
        if e.code == "unknown_bot":
            raise _Fail(2, "找不到这只 bot 的配置")
        if e.code == "invalid":   # 校验的原因只说哪里不对（如超长、控制字符），不带值
            names = {field_key: name for name, field_key in ITEMS}
            detail = "；".join("%s %s" % (names.get(k, k), why) for k, why in sorted(e.fields.items()))
            raise _Fail(4, "草稿没通过校验（%s），配置没有改动" % detail)
        raise _Fail(1, "写入失败：%s" % e.message)
    print("已写入 %s" % bot_id)


def apply_dsh(bot_id: str, path: str) -> None:
    if not valid_bot_id(bot_id):
        raise _Fail(2, "bot_id 不合规：只能用字母、数字、点、下划线、连字符")
    data = _read_draft(path)
    sleep = data.get(SLEEP_KEY) if data is not None else None
    if not _sleep_ok(sleep):
        raise _Fail(4, "草稿不是含合规 sleep_hours 的 YAML（要 0 到 6 项，每项 HH:MM-HH:MM），没有写入")
    adapter = _dsh_adapter()
    try:
        adapter.set_life(bot_id, {SLEEP_KEY: sleep})   # 只写这一项
    except (AdapterError, AdapterUnavailable) as e:
        raise _dsh_fail(e, writing=True)
    print("已写入 dsh/%s" % bot_id)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="新建 bot 后起草主动消息相关配置（claude：人设摘要、主动说话条件、"
                                            "思念值变化规则、睡觉时段；dsh：睡觉时段）")
    p.add_argument("bot_id")
    p.add_argument("--backend", choices=BACKENDS, default="claude", help="bot 属于哪个底层（缺省 claude）")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--out", metavar="路径",
                      help="草稿写到哪（缺省：当前目录下 <bot_id>-proactive-draft.yml，dsh 为 dsh-<bot_id>-proactive-draft.yml）")
    mode.add_argument("--apply", metavar="草稿路径", help="把过目同意的草稿写进这只 bot 的配置")
    p.add_argument("--force", action="store_true", help="起草时落点已有文件也覆盖")
    args = p.parse_args(argv)   # 用法错误由 argparse 以退出码 2 结束
    try:
        if args.apply is not None:
            (apply_dsh if args.backend == "dsh" else apply_claude)(args.bot_id, args.apply)
        else:
            draft(args.bot_id, args.backend, args.out, args.force)
    except _Fail as e:
        print(e.message, file=sys.stderr)
        return e.code
    except Exception as e:   # 兜底只报类名：异常文字与回溯可能带出人设、草稿或回包的内容
        print("出错了：%s" % type(e).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
