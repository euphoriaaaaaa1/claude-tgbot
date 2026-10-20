#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""旧 provider 一次性迁移：旧 cliproxy ``config.yaml`` / 旧 Hub ``hub.env`` → 公开版 ``providers.json``。

用法：
    python3 scripts/migrate_legacy_providers.py --dry-run
    python3 scripts/migrate_legacy_providers.py --apply \\
      --from-cliproxy ~/.claude-tgbot/cliproxy/config.yaml \\
      --from-hub ~/.claude-tgbot/hub.env

契约（INTERFACE §3.6）：
- 默认 dry-run，只有显式 ``--apply`` 才写 ``providers.json``；
- 只读旧文件，绝不修改、绝不启动旧 cliproxy/Hub；
- 经 ``provider_config.save_providers`` 落盘（唯一写者），保留既有 ``bindings`` 与代理元数据；
- 输出只报供应商/模型/跳过数量，不打印任何密钥；
- 幂等：同来源重复执行不产生重复条目（相同 id/同名一律跳过，不覆盖现有）；
- 退出码：0 成功/无迁移项；1 解析/写入失败；2 参数错。

来源与冲突策略：
- cliproxy ``claude-api-key`` 块 → protocol=anthropic；``openai-compatibility`` 块 → protocol=openai；
  ``gemini-api-key`` 不在公开版协议范围内，进报告不写入。
- 同 id（由来源+协议+地址+名字确定性派生）已存在 → 跳过；同名已存在 → 跳过。
- 缺名字 → 用模型 display-name 或地址主机名降级；缺模型 → 空模型列表并在报告里标注；
  缺地址/缺密钥 → 无法识别，只进报告。
- 旧 Hub 的 ``hub.env`` 正常只含门户口令与 cliproxy 连接参数，不含供应商数据；若用户手工写过
  ``ANTHROPIC_BASE_URL``/``OPENAI_BASE_URL`` 或 ``HUB_PROVIDERS_JSON``，也按同一规则导入。
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_CLIPROXY_BLOCKS = (
    ("claude-api-key", "anthropic"),
    ("openai-compatibility", "openai"),
)
_UNSUPPORTED_BLOCKS = {"gemini-api-key": "gemini"}
_RESERVED_NAMES = frozenset(
    ("add", "refresh", "remove", "list", "global", "reset", "default", "help"))
_HUB_JSON_KEYS = ("HUB_PROVIDERS_JSON", "HUB_PROVIDERS", "PROVIDERS_JSON")
_NAME_BAD = re.compile(r"[\s\x00-\x1f\x7f-\x9f]+")


class MigrationError(Exception):
    """可预期的失败：只带安全文案，绝不携带密钥。"""


@dataclass
class Draft:
    source: str
    protocol: str
    name: str
    base_url: str
    api_key: str
    models: list = field(default_factory=list)
    default_model: str = ""
    legacy_block: str = ""
    legacy_ref: str = ""
    degraded: list = field(default_factory=list)

    def ident(self) -> str:
        raw = "\x1f".join((self.source, self.protocol, self.base_url, self.name))
        return "legacy_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


@dataclass
class ParseResult:
    drafts: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    notes: list = field(default_factory=list)


def _default_sources() -> tuple:
    home = os.path.expanduser(os.environ.get("CLAUDE_TGBOT_HOME") or "~/.claude-tgbot")
    return (os.path.join(home, "cliproxy", "config.yaml"),
            os.path.join(home, "hub.env"))


def _providers_path() -> str:
    """与 ``provider_config.PROVIDERS_PATH`` 同一口径（这里不 import，dry-run 也要能跑）。"""
    return os.environ.get("CLAUDEBOT_PROVIDER_PATH") or os.path.join(
        _REPO_ROOT, "configs", "providers.json")


def _valid_base_url(value) -> bool:
    if not isinstance(value, str) or not (0 < len(value) <= 500) or "@" in value:
        return False
    try:
        parsed = urlparse(value)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname)
    except ValueError:
        return False


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").strip()
    except ValueError:
        return ""


def _clean_name(raw, fallback: str) -> str:
    """把旧名字收成公开版可用的名字：无空白/控制字符、不以 -/_ 开头、≤32 字符。"""
    text = _NAME_BAD.sub("-", str(raw or "").strip())
    text = text.strip("-_")
    if not text:
        text = _NAME_BAD.sub("-", fallback or "legacy").strip("-_") or "legacy"
    text = text[:32]
    if text.lower() in _RESERVED_NAMES:
        text = (text + "-legacy")[:32]
    return text


def _model_list(raw) -> list:
    """旧 models 数组 → [{"id": 上游模型名, "label": 展示名}]；无法识别的元素丢弃。"""
    if not isinstance(raw, list):
        return []
    seen, out = set(), []
    for item in raw:
        if isinstance(item, str):
            mid, label = item.strip(), item.strip()
        elif isinstance(item, dict):
            mid = item.get("name") or item.get("id")
            # alias 是旧版客户端的调用名，不是上游模型的可读标签；展示名优先，其次模型名。
            label = (item.get("display-name") or item.get("display_name")
                     or item.get("label") or mid)
        else:
            continue
        if not isinstance(mid, str) or not mid.strip():
            continue
        mid = mid.strip()
        if mid in seen:
            continue
        seen.add(mid)
        out.append({"id": mid, "label": str(label).strip() or mid})
    return out


def _model_names_from_text(raw) -> list:
    if not isinstance(raw, str):
        return []
    out = []
    for part in re.split(r"[,;]", raw):
        part = part.strip()
        if part:
            out.append({"id": part, "label": part})
    return out


def _legacy_key(entry: dict, protocol: str) -> str:
    """旧块里的上游密钥；只返回值，绝不往外打印。"""
    if protocol == "openai":
        entries = entry.get("api-key-entries")
        if isinstance(entries, list):
            for item in entries:
                if isinstance(item, dict):
                    value = item.get("api-key") or item.get("api_key")
                    if isinstance(value, str) and value.strip():
                        return value.strip()
    value = entry.get("api-key") or entry.get("api_key")
    return value.strip() if isinstance(value, str) else ""


def _base_url(entry: dict) -> str:
    return entry.get("base-url") or entry.get("base_url") or ""


def _name_from_entry(entry: dict, protocol: str, models: list, base_url: str) -> str:
    raw = entry.get("label") or entry.get("name")
    if isinstance(raw, str) and raw.strip():
        # 旧门户内部名形如 hub-<8 位 hex>，对用户没有意义，改用模型展示名/主机名。
        if not re.fullmatch(r"hub-[0-9a-fA-F]{4,16}", raw.strip()):
            return _clean_name(raw, _host_of(base_url) or "legacy")
    for model in models:
        label = model.get("label")
        if isinstance(label, str) and label.strip() and label.strip() != model.get("id"):
            return _clean_name(label, _host_of(base_url) or "legacy")
    return _clean_name(_host_of(base_url) or ("legacy-" + protocol),
                       _host_of(base_url) or ("legacy-" + protocol))


def parse_cliproxy(path: str) -> ParseResult:
    """读旧 cliproxy ``config.yaml``。文件不存在/为空 → 空结果；结构损坏 → MigrationError。"""
    result = ParseResult()
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        result.notes.append("cliproxy 配置不存在，跳过")
        return result
    except (OSError, UnicodeError):
        raise MigrationError("cliproxy 配置读取失败")
    if not text.strip():
        result.notes.append("cliproxy 配置为空，无迁移项")
        return result

    try:
        import yaml
    except ImportError:
        raise MigrationError("缺少 PyYAML，无法解析 cliproxy 配置")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        # 绝不打 str(e)：YAML 错误片段可能带出配置里的密钥。
        raise MigrationError("cliproxy 配置解析失败（YAML 格式错误）")
    except (ValueError, TypeError):
        raise MigrationError("cliproxy 配置解析失败")
    if doc is None:
        result.notes.append("cliproxy 配置为空，无迁移项")
        return result
    if not isinstance(doc, dict):
        raise MigrationError("cliproxy 配置顶层不是映射")

    for block, protocol in _CLIPROXY_BLOCKS:
        entries = doc.get(block)
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise MigrationError("cliproxy 配置块 %s 格式损坏" % block)
        for index, entry in enumerate(entries):
            ref = "%s[%d]" % (block, index)
            if not isinstance(entry, dict):
                result.rejected.append("%s: 条目不是映射，跳过" % ref)
                continue
            base_url = _base_url(entry)
            if not _valid_base_url(base_url):
                result.rejected.append("%s: 地址缺失或不可用，跳过" % ref)
                continue
            api_key = _legacy_key(entry, protocol)
            if not api_key:
                result.rejected.append("%s: 缺少可用密钥，跳过" % ref)
                continue
            if len(api_key) > 500:
                result.rejected.append("%s: 密钥长度超限，跳过" % ref)
                continue

            degraded = []
            models = _model_list(entry.get("models"))
            if not models:
                degraded.append("无模型")
            name = _name_from_entry(entry, protocol, models, base_url)
            default_model = models[0]["id"] if models else ""
            result.drafts.append(Draft(
                source="cliproxy", protocol=protocol, name=name,
                base_url=base_url, api_key=api_key, models=models,
                default_model=default_model, legacy_block=block,
                legacy_ref=ref, degraded=degraded))
    for block in _UNSUPPORTED_BLOCKS:
        entries = doc.get(block)
        if entries:
            result.rejected.append("%s: 公开版不支持该协议，整块跳过" % block)
    return result


def _parse_env(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].lstrip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _draft_from_flat_env(env: dict, prefix: str, protocol: str) -> tuple:
    base_url = env.get(prefix + "_BASE_URL") or env.get(prefix + "_URL") or ""
    token = (env.get(prefix + "_AUTH_TOKEN") or env.get(prefix + "_API_KEY")
             or env.get(prefix + "_TOKEN") or "")
    model_raw = env.get(prefix + "_MODEL") or env.get(prefix + "_DEFAULT_MODEL") or ""
    if not base_url and not token:
        return None, ""
    if not _valid_base_url(base_url):
        return None, "%s: 地址缺失或不可用，跳过" % prefix
    if not token:
        return None, "%s: 缺少可用密钥，跳过" % prefix
    host = _host_of(base_url)
    if host in ("127.0.0.1", "localhost", "::1") or host.startswith("127."):
        return None, "%s: 指向本机地址（疑似旧代理），跳过" % prefix
    models = _model_names_from_text(model_raw)
    name = _clean_name(env.get("PROVIDER_NAME"), host or ("legacy-" + protocol))
    return Draft(
        source="hub", protocol=protocol, name=name, base_url=base_url,
        api_key=token.strip(), models=models,
        default_model=models[0]["id"] if models else "",
        legacy_block="hub.env", legacy_ref=prefix,
        degraded=[] if models else ["无模型"]), ""


def _draft_from_json_item(item, index: int) -> tuple:
    if not isinstance(item, dict):
        return None, "HUB_PROVIDERS_JSON[%d]: 条目不是对象，跳过" % index
    protocol = item.get("protocol") or item.get("kind") or ""
    if protocol not in ("anthropic", "openai"):
        return None, "HUB_PROVIDERS_JSON[%d]: 协议不支持，跳过" % index
    base_url = item.get("base_url") or item.get("base-url") or ""
    api_key = (item.get("api_key") or item.get("api-key")
               or item.get("key") or item.get("token") or "")
    if not _valid_base_url(base_url):
        return None, "HUB_PROVIDERS_JSON[%d]: 地址缺失或不可用，跳过" % index
    if not isinstance(api_key, str) or not api_key.strip():
        return None, "HUB_PROVIDERS_JSON[%d]: 缺少可用密钥，跳过" % index
    models = _model_list(item.get("models"))
    name = _clean_name(item.get("name") or item.get("label"),
                       _host_of(base_url) or ("legacy-" + protocol))
    return Draft(
        source="hub", protocol=protocol, name=name, base_url=base_url,
        api_key=api_key.strip(), models=models,
        default_model=item.get("default_model") or (models[0]["id"] if models else ""),
        legacy_block="hub.env", legacy_ref="HUB_PROVIDERS_JSON[%d]" % index,
        degraded=[] if models else ["无模型"]), ""


def parse_hub_env(path: str) -> ParseResult:
    """读旧 Hub ``hub.env``。正常只含门户/代理连接参数 → 无迁移项，不报错。"""
    result = ParseResult()
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        result.notes.append("Hub 配置不存在，跳过")
        return result
    except (OSError, UnicodeError):
        raise MigrationError("Hub 配置读取失败")
    if not text.strip():
        result.notes.append("Hub 配置为空，无迁移项")
        return result

    env = _parse_env(text)
    if any(k in env for k in ("CLIPROXY_PORT", "CLIPROXY_MGMT_KEY", "CLIPROXY_API_KEY")):
        result.notes.append("Hub 配置中的旧代理连接参数已忽略（不含供应商数据）")

    for key in _HUB_JSON_KEYS:
        raw = env.get(key)
        if not raw:
            continue
        try:
            blob = json.loads(raw)
        except ValueError:
            result.rejected.append("%s: JSON 无法解析，跳过" % key)
            continue
        items = blob.get("providers") if isinstance(blob, dict) else blob
        if isinstance(blob, dict) and isinstance(blob.get("bindings"), dict) and blob["bindings"]:
            result.notes.append(
                "%s: 旧绑定 %d 条不在迁移范围（公开版按 bot 单独绑定）"
                % (key, len(blob["bindings"])))
        if not isinstance(items, list):
            result.rejected.append("%s: 供应商列表格式不可识别，跳过" % key)
            continue
        for index, item in enumerate(items):
            draft, reason = _draft_from_json_item(item, index)
            if draft is not None:
                result.drafts.append(draft)
            elif reason:
                result.rejected.append(reason)

    for prefix, protocol in (("ANTHROPIC", "anthropic"), ("OPENAI", "openai")):
        draft, reason = _draft_from_flat_env(env, prefix, protocol)
        if draft is not None:
            result.drafts.append(draft)
        elif reason:
            result.rejected.append(reason)
    return result


def _read_target(path: str) -> dict:
    """读现有 providers.json（dry-run 用；写入前还会经 provider_config 再读一次）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return {"providers": [], "bindings": {}}
    except (OSError, UnicodeError):
        raise MigrationError("现有 providers.json 读取失败")
    if not raw.strip():
        return {"providers": [], "bindings": {}}
    try:
        data = json.loads(raw)
    except ValueError:
        raise MigrationError("现有 providers.json 解析失败")
    if not isinstance(data, dict):
        raise MigrationError("现有 providers.json 解析失败")
    if not isinstance(data.get("providers"), list):
        data["providers"] = []
    if not isinstance(data.get("bindings"), dict):
        data["bindings"] = {}
    return data


def _entry_for(draft: Draft, now: int) -> dict:
    return {
        "id": draft.ident(),
        "name": draft.name,
        "base_url": draft.base_url,
        "api_key": draft.api_key,
        "models": list(draft.models),
        "default_model": draft.default_model,
        "created_at": now,
        "updated_at": now,
        "proxy": False,
        "protocol": draft.protocol,
        "anthropic_base_url": "",
        "openai_base_url": "",
        "meta": {
            "manual_models": [m["id"] for m in draft.models],
            "owner_context": {},
            "guessed_context": {},
            "legacy_source": draft.source,
            "legacy_block": draft.legacy_block,
            "legacy_ref": draft.legacy_ref,
        },
    }


def merge_drafts(data: dict, drafts: list) -> tuple:
    """同 id/同名一律跳过（现有清单优先）；返回 (added_entries, skipped_reasons)。"""
    existing_ids = {p.get("id") for p in data.get("providers") or [] if isinstance(p, dict)}
    existing_names = {p.get("name") for p in data.get("providers") or []
                      if isinstance(p, dict) and isinstance(p.get("name"), str)}
    added, skipped = [], []
    now = int(time.time())
    for draft in drafts:
        pid = draft.ident()
        if pid in existing_ids:
            skipped.append("%s: 同 id 已存在，跳过" % draft.legacy_ref)
            continue
        if draft.name in existing_names:
            skipped.append("%s: 同名已存在，跳过" % draft.legacy_ref)
            continue
        entry = _entry_for(draft, now)
        data.setdefault("providers", []).append(entry)
        existing_ids.add(pid)
        existing_names.add(draft.name)
        added.append(entry)
    return added, skipped


def _apply(drafts: list) -> tuple:
    """经 provider_config 的唯一写者落盘；无新增时不写。"""
    try:
        import provider_config
    except Exception as exc:  # noqa: BLE001 —— 只报类名，避免带出任何配置内容
        raise MigrationError("无法加载写入模块（%s）" % type(exc).__name__)
    try:
        data = provider_config.load_providers()
    except Exception as exc:  # noqa: BLE001
        raise MigrationError("现有 providers.json 解析失败（%s）" % type(exc).__name__)
    added, skipped = merge_drafts(data, drafts)
    if not added:
        return added, skipped
    try:
        provider_config.save_providers(data)
    except Exception as exc:  # noqa: BLE001
        raise MigrationError("写入 providers.json 失败（%s）" % type(exc).__name__)
    return added, skipped


def _count_models(drafts: list) -> int:
    return sum(len(d.models) for d in drafts)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="旧 provider 一次性迁移（默认 dry-run，不写任何文件）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="只解析并报告，不写 providers.json（默认）")
    mode.add_argument("--apply", action="store_true",
                      help="把迁移结果写入 providers.json")
    parser.add_argument("--from-cliproxy", default=None,
                        help="旧 cliproxy config.yaml 路径（默认 ~/.claude-tgbot/cliproxy/config.yaml）")
    parser.add_argument("--from-hub", default=None,
                        help="旧 Hub hub.env 路径（默认 ~/.claude-tgbot/hub.env）")
    return parser


def _run(args) -> int:
    default_cliproxy, default_hub = _default_sources()
    cliproxy_path = os.path.expanduser(args.from_cliproxy or default_cliproxy)
    hub_path = os.path.expanduser(args.from_hub or default_hub)
    apply = bool(args.apply)

    result = ParseResult()
    for source_path, label, parser_fn in (
            (cliproxy_path, "cliproxy", parse_cliproxy),
            (hub_path, "Hub", parse_hub_env)):
        try:
            parsed = parser_fn(source_path)
        except MigrationError as exc:
            print("[迁移] 失败: %s" % exc, file=sys.stderr)
            return 1
        result.notes.extend(parsed.notes)
        result.rejected.extend(parsed.rejected)
        result.drafts.extend(parsed.drafts)

    lines = [
        "[迁移] cliproxy 来源: %s（%s）" % (cliproxy_path,
                                          "存在" if os.path.exists(cliproxy_path) else "不存在"),
        "[迁移] Hub 来源: %s（%s）" % (hub_path,
                                       "存在" if os.path.exists(hub_path) else "不存在"),
        "[迁移] 解析: 供应商 %d 个，模型 %d 个" % (len(result.drafts), _count_models(result.drafts)),
    ]
    degraded = sum(1 for d in result.drafts if d.degraded)
    if degraded:
        lines.append("[迁移] 降级: %d 个（缺字段按默认值处理）" % degraded)
    for note in result.notes:
        lines.append("[迁移] 备注: %s" % note)
    if result.rejected:
        lines.append("[迁移] 无法识别: %d 个（不写入）" % len(result.rejected))
        lines.extend("  - %s" % reason for reason in result.rejected)

    if apply:
        if not result.drafts:
            lines.append("[迁移] 模式: apply；无迁移项，未写入 providers.json")
        else:
            try:
                added, skipped = _apply(result.drafts)
            except MigrationError as exc:
                print("\n".join(lines), file=sys.stdout)
                print("[迁移] 失败: %s" % exc, file=sys.stderr)
                return 1
            lines.append("[迁移] 模式: apply；新增供应商 %d 个，模型 %d 个"
                         % (len(added), sum(len(e.get("models") or []) for e in added)))
            if skipped:
                lines.append("[迁移] 跳过: %d 个（同 id/同名，现有条目优先）" % len(skipped))
                lines.extend("  - %s" % reason for reason in skipped)
            if not added:
                lines.append("[迁移] providers.json 未改动（幂等）")
    else:
        try:
            data = _read_target(_providers_path())
        except MigrationError as exc:
            print("\n".join(lines), file=sys.stdout)
            print("[迁移] 失败: %s" % exc, file=sys.stderr)
            return 1
        would_add, skipped = merge_drafts(data, result.drafts)
        lines.append("[迁移] 模式: dry-run（未写入 providers.json）；将新增供应商 %d 个，模型 %d 个"
                     % (len(would_add),
                        sum(len(e.get("models") or []) for e in would_add)))
        if skipped:
            lines.append("[迁移] 将跳过: %d 个（同 id/同名，现有条目优先）" % len(skipped))
            lines.extend("  - %s" % reason for reason in skipped)

    print("\n".join(lines))
    return 0


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except MigrationError as exc:
        print("[迁移] 失败: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
