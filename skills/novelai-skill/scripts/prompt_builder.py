#!/usr/bin/env python3
"""Build final NovelAI prompts from structured intermediate JSON."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

WEIGHTED_TAG_PATTERN = re.compile(r"^-?\d+(?:\.\d+)?::.+::$")
MULTISPACE_PATTERN = re.compile(r"\s+")
MULTICOMMA_PATTERN = re.compile(r"(?:\s*,\s*)+")
SCRIPT_DIR = Path(__file__).resolve().parent
CHAT_MAPPINGS_PATH = SCRIPT_DIR.parent / "assets" / "chat_mappings.json"
DEFAULT_REVISION_INSTRUCTION = (
    "different camera angle, different viewpoint, different composition, "
    "keep same character, same outfit, same scene, same lighting"
)
# nsfw 标签解析（INTERFACE-imagegen-nsfw §4）：只认"标签恰好是 nsfw"，不做子串匹配。
# 分隔符只有英文逗号、中文逗号、竖线（多角色分段）；空格不是分隔符。
TAG_SEPARATORS = ",，|"
BLOCK_OPEN_PATTERN = re.compile(r"\s*(-?(?:\d+(?:\.\d*)?|\.\d+))::")
SD_WEIGHT_PATTERN = re.compile(r":(-?(?:\d+(?:\.\d*)?|\.\d+))$")
TAG_BRACKETS = "()[]{}"
# 这两个键以前能强制色图；用户裁定后只看正文里的 nsfw 标签，出现时 stderr 留一行线索。
RETIRED_NSFW_KEYS = ("nsfw", "rating")
# 去掉触发词后再去掉的开头标点与空白（检出修改指令、判断新提示词共用一份）。
REVISION_LEADING_PUNCTUATION = "，,。.!！?？:： "
# 修改模式下"本次写了新提示词"只看这三个字段，结构化字段不算（INTERFACE-imagegen-nsfw §1.2 r2）。
NEW_PROMPT_FIELDS = ("prompt", "prompt_body", "positive_prompt_body")
ASCII_LETTER_PATTERN = re.compile(r"[A-Za-z]")
# 中日韩文字：汉字（含扩展区、部首、〇々）、日文假名（含半角）、韩文字母与音节、注音。标点不算文字。
CJK_CHAR_PATTERN = re.compile(
    "[ᄀ-ᇿ⺀-⿟々-〇぀-ヿ㄀-㆏ㆠ-ㇿ"
    "㐀-䶿一-鿿ꥠ-꥿가-퟿豈-﫿ｦ-ￜ"
    "\U00020000-\U0003134f]"
)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_chat_mappings() -> dict[str, Any]:
    if CHAT_MAPPINGS_PATH.exists():
        return load_json(CHAT_MAPPINGS_PATH)
    return {"revision_triggers": []}


def normalize_text(value: str) -> str:
    value = MULTISPACE_PATTERN.sub(" ", value.strip())
    value = value.strip(", ")
    return value


def normalize_tag(tag: str) -> str:
    tag = normalize_text(tag)
    if not tag:
        return ""
    if WEIGHTED_TAG_PATTERN.match(tag):
        return tag
    tag = tag.replace("，", ",")
    tag = MULTICOMMA_PATTERN.sub(", ", tag)
    return tag.strip(", ")


def dedupe_keep_order(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for tag in tags:
        clean = normalize_tag(tag)
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(clean)
    return result


def listify(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _normalize_tag_name(raw: str) -> tuple[str, float | None]:
    """单个标签归一：去空白、两端括号（可多层）、末尾 SD 权重 `:<数字>`，返回 (小写名, SD 权重)。"""
    sd_weight: float | None = None
    previous = None
    tag = raw
    while tag != previous:
        previous = tag
        tag = tag.strip().strip(TAG_BRACKETS).strip()
        match = SD_WEIGHT_PATTERN.search(tag)
        if match and sd_weight is None:
            sd_weight = float(match.group(1))
            tag = tag[: match.start()]
    return tag.lower(), sd_weight


def parse_prompt_tags(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把提示词解析成标签列表与 NAI 权重块列表（计数、去重、新提示词判定共用这一份解析）。

    标签：{start, end, name, weight, sd_weight, block}，start/end 是原文位置（不含块头 `<数字>::`
    与块尾 `::`），block 是所在块的下标或 None。块：{start, end, weight}，start 是块头起点，
    end 是块尾 `::` 之后；未闭合的块 end 为 None。空标签不记录。
    """
    tags: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    open_block: int | None = None

    def add_tag(start: int, end: int) -> None:
        name, sd_weight = _normalize_tag_name(text[start:end])
        if name:
            weight = blocks[open_block]["weight"] if open_block is not None else 1.0
            tags.append({"start": start, "end": end, "name": name, "weight": weight,
                         "sd_weight": sd_weight, "block": open_block})

    position = 0
    while position <= len(text):
        segment_end = position
        while segment_end < len(text) and text[segment_end] not in TAG_SEPARATORS:
            segment_end += 1
        cursor = position
        while True:
            # 段首的 `<数字>::` 开新块；上一块若未闭合，视为到此结束。
            opener = BLOCK_OPEN_PATTERN.match(text, cursor, segment_end)
            if opener:
                blocks.append({"start": opener.start(1), "end": None,
                               "weight": float(opener.group(1))})
                open_block = len(blocks) - 1
                cursor = opener.end()
            # 只有块打开时 `::` 才是块尾；否则它是普通字符（`nsfw::` 不是 nsfw 标签）。
            closer = text.find("::", cursor, segment_end) if open_block is not None else -1
            if closer == -1:
                add_tag(cursor, segment_end)
                break
            add_tag(cursor, closer)
            blocks[open_block]["end"] = closer + 2
            open_block = None
            cursor = closer + 2
            if not text[cursor:segment_end].strip():
                break
        position = segment_end + 1
    return tags, blocks


def _has_positive_weight(tag: dict[str, Any]) -> bool:
    # 权重 ≤ 0（块权重或 SD 写法）表达的是"压制"，不算"写了"这个标签。
    sd_weight = tag["sd_weight"]
    return tag["weight"] > 0 and (sd_weight is None or sd_weight > 0)


def _is_effective_nsfw(tag: dict[str, Any]) -> bool:
    return tag["name"] == "nsfw" and _has_positive_weight(tag)


def count_nsfw_tags(text: str) -> int:
    tags, _blocks = parse_prompt_tags(text)
    return sum(1 for tag in tags if _is_effective_nsfw(tag))


def _tag_cut_span(text: str, start: int, end: int) -> tuple[int, int]:
    """被删标签的删除范围：去掉首尾空白；跨标签的 SD 括号（如 `(nsfw, 1girl:1.2)` 的左括号）不配对的部分留下。"""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    lead = 0
    while start + lead < end and text[start + lead] in "([{":
        lead += 1
    trail = 0
    while end - trail - 1 >= start + lead and text[end - trail - 1] in ")]}":
        trail += 1
    return start + max(0, lead - trail), end - max(0, trail - lead)


def _separator_cut(text: str, start: int, end: int) -> tuple[int, int]:
    """把 [start, end) 连同相邻的一个分隔符一起删：优先删逗号（先右后左），没有逗号才删一个 `|`。"""
    right = end
    while right < len(text) and text[right].isspace():
        right += 1
    left = start - 1
    while left >= 0 and text[left].isspace():
        left -= 1
    right_sep = text[right] if right < len(text) and text[right] in TAG_SEPARATORS else ""
    left_sep = text[left] if left >= 0 and text[left] in TAG_SEPARATORS else ""
    # 两侧没有逗号时，它独占一个多角色段，删一个 `|` 让这一段整体消失，其余段仍被 `|` 分开。
    for wanted in (",，", "|"):
        if right_sep and right_sep in wanted:
            right += 1
            while right < len(text) and text[right].isspace():
                right += 1
            return start, right
        if left_sep and left_sep in wanted:
            return left, end
    return start, end


def dedupe_nsfw_tags(text: str) -> str:
    """有效 nsfw 标签 ≥2 个时只留最先出现的一个，其余连同一个相邻分隔符删掉；< 2 个时原样返回。

    权重块里被删的标签保留块头块尾（块里只剩它时整块删），避免孤立的 `::` 改掉后面所有标签的权重。
    改写后再做一次与最终拼接相同的 normalize_tag，保证最终正向里能原样找到这段正文（景别守卫靠它定位）。
    """
    tags, blocks = parse_prompt_tags(text)
    hits = [tag for tag in tags if _is_effective_nsfw(tag)]
    if len(hits) < 2:
        return text
    removed = {id(tag) for tag in hits[1:]}
    spans: list[tuple[int, int]] = []
    whole_blocks: set[int] = set()
    for index, block in enumerate(blocks):
        members = [tag for tag in tags if tag["block"] == index]
        if members and all(id(tag) in removed for tag in members):
            whole_blocks.add(index)
            block_end = block["end"] if block["end"] is not None else members[-1]["end"]
            spans.append(_separator_cut(text, block["start"], block_end))
    for tag in hits[1:]:
        if tag["block"] not in whole_blocks:
            spans.append(_separator_cut(text, *_tag_cut_span(text, tag["start"], tag["end"])))
    result = text
    for start, end in sorted(spans, reverse=True):
        result = result[:start] + result[end:]
    return normalize_tag(result)


def build_prompt_from_parts(intermediate: dict[str, Any]) -> str:
    summary = (
        intermediate.get("summary")
        or intermediate.get("shot_summary")
        or intermediate.get("prompt_summary")
        or ""
    )
    tags: list[str] = []
    if summary:
        tags.append(str(summary))
    tags.extend(listify(intermediate.get("character_count_tags") or intermediate.get("count_tags")))
    tags.extend(listify(intermediate.get("style_tags") or intermediate.get("style")))
    tags.extend(listify(intermediate.get("scene_tags") or intermediate.get("scene")))
    tags.extend(listify(intermediate.get("camera_tags") or intermediate.get("camera")))
    tags.extend(listify(intermediate.get("mood_tags") or intermediate.get("mood")))

    characters = intermediate.get("characters") or intermediate.get("people") or []
    if isinstance(characters, list):
        for character in characters:
            if isinstance(character, dict):
                tags.extend(listify(character.get("tags") or character.get("traits")))
            elif isinstance(character, str):
                tags.append(character)

    tags.extend(listify(intermediate.get("extra_tags") or intermediate.get("details")))
    return ", ".join(dedupe_keep_order([str(tag) for tag in tags]))


def detect_revision_intent(text: str) -> tuple[bool, str, str]:
    normalized = normalize_text(text)
    lowered = normalized.lower()
    mappings = load_chat_mappings()
    revision_patterns = tuple(mappings.get("revision_triggers", []))
    for phrase in revision_patterns:
        if phrase in lowered:
            remainder = normalize_text(lowered.replace(phrase, "", 1))
            remainder = remainder.lstrip(REVISION_LEADING_PUNCTUATION)
            return True, phrase, remainder
    return False, "", normalized


def infer_mode_and_revision(intermediate: dict[str, Any]) -> tuple[str, str, str]:
    explicit_mode = normalize_text(str(intermediate.get("mode", ""))).lower()
    explicit_revision = normalize_text(str(intermediate.get("revision_instruction", "")))
    if explicit_mode in {"new", "revise"}:
        return explicit_mode, "", explicit_revision

    for field in ("reply_text", "prompt", "prompt_body", "positive_prompt_body"):
        raw_value = intermediate.get(field)
        if not isinstance(raw_value, str) or not raw_value.strip():
            continue
        matched, phrase, remainder = detect_revision_intent(raw_value)
        if matched:
            if not remainder:
                remainder = DEFAULT_REVISION_INSTRUCTION
            return "revise", phrase, remainder
    return "new", "", explicit_revision


def extract_new_prompt(intermediate: dict[str, Any]) -> str:
    """修改模式下判断"本次有没有写新提示词"（INTERFACE-imagegen-nsfw §1.2 r2）：写了返回新提示词，没写返回空串。

    候选 = prompt / prompt_body / positive_prompt_body 中第一个去空白后非空的字符串；含触发词时去掉
    第一处触发词和开头标点（保留原大小写）作为新提示词。候选里至少有一个英文标签才算写了（r2.4：正权重、
    去掉权重/括号记号后不含中日韩文字且至少含一个英文字母）：只剩中文指令（"再来一张，换个角度"）、
    夹着英文词的中文指令（"换个pose"）、标点、数字、压制写法都不算，照旧沿用上一张。
    """
    candidate = next(
        (value for value in (intermediate.get(field) for field in NEW_PROMPT_FIELDS)
         if isinstance(value, str) and value.strip()),
        "",
    )
    candidate = normalize_text(candidate)
    remainder = candidate
    for phrase in load_chat_mappings().get("revision_triggers", []):
        if phrase and phrase in remainder:
            remainder = normalize_text(remainder.replace(phrase, "", 1))
            remainder = remainder.lstrip(REVISION_LEADING_PUNCTUATION)
            break
    # 在去掉触发词之前切标签（契约 r2.4）：与触发词同在一个标签里的英文词（"再来一张pose"、
    # "再来一张 pose"）属于中文指令，不算英文标签；有标点隔开的（"再来一张, pose"）不受影响。
    tags, _blocks = parse_prompt_tags(candidate)
    written = any(
        _has_positive_weight(tag)
        and ASCII_LETTER_PATTERN.search(tag["name"])
        and not CJK_CHAR_PATTERN.search(tag["name"])
        for tag in tags
    )
    return remainder if written else ""


def normalize_intermediate(intermediate: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(intermediate, str):
        mode, phrase, revision = infer_mode_and_revision({"prompt": intermediate})
        new_prompt = extract_new_prompt({"prompt": intermediate}) if mode == "revise" else ""
        prompt_text = normalize_text(intermediate if mode == "new" else new_prompt)
        return {
            "prompt": prompt_text,
            "reply_text": "",
            "mode": mode,
            "revision_trigger": phrase,
            "revision_instruction": revision,
            "override_full_prompt": False,
            "new_prompt_written": bool(new_prompt),
        }

    normalized = dict(intermediate)
    prompt = (
        intermediate.get("prompt")
        or intermediate.get("prompt_body")
        or intermediate.get("positive_prompt_body")
        or ""
    )
    if not prompt:
        prompt = build_prompt_from_parts(intermediate)

    mode, phrase, revision = infer_mode_and_revision(intermediate)
    # 修改模式下 AI 写了新提示词就只用新提示词（不沿用上一张，见 build_prompts）；没写才走原来的沿用逻辑。
    new_prompt = extract_new_prompt(intermediate) if mode == "revise" else ""
    if new_prompt:
        prompt = new_prompt
    elif mode == "revise" and not normalize_text(str(intermediate.get("prompt", ""))):
        prompt = ""

    normalized["prompt"] = normalize_text(str(prompt))
    normalized["reply_text"] = normalize_text(str(intermediate.get("reply_text", "")))
    normalized["mode"] = mode
    normalized["revision_trigger"] = phrase
    normalized["revision_instruction"] = revision
    normalized["override_full_prompt"] = bool(intermediate.get("override_full_prompt", False))
    normalized["intent"] = normalize_text(str(intermediate.get("intent", "")))
    normalized["new_prompt_written"] = bool(new_prompt)
    return normalized


def build_reply_text(intermediate: dict[str, Any]) -> str:
    reply_text = normalize_text(str(intermediate.get("reply_text", "")))
    if reply_text:
        return reply_text
    if normalize_text(str(intermediate.get("mode", ""))) == "revise":
        return "这次我按上一张的路子继续给你来一张。"
    return ""


def build_prompts(
    config: dict[str, Any],
    intermediate: dict[str, Any] | str,
    previous_state: dict[str, Any] | None = None,
) -> dict[str, str]:
    # 放在最开头：之后无论正文为空、缺令牌还是上游报错，这行线索都已经输出。
    if isinstance(intermediate, dict):
        for key in RETIRED_NSFW_KEYS:
            if key in intermediate:
                sys.stderr.write(
                    f"[novelai] 提示：intermediate 里的 {key} 字段已不参与判定，"
                    "是否色图只看 prompt 里有没有 nsfw 标签\n"
                )
    normalized = normalize_intermediate(intermediate)
    # 写了新提示词时 revision_instruction 不拼进正文（否则它也参与色图判定，违背"只看新提示词"），
    # 留一行线索免得静默丢内容。触发词在 reply_text 里剩下的文字同样不用，但不提示。
    revision_field = intermediate.get("revision_instruction") if isinstance(intermediate, dict) else None
    if normalized.get("new_prompt_written") and isinstance(revision_field, str) and revision_field.strip():
        sys.stderr.write(
            "[novelai] 提示：本次写了新提示词，revision_instruction 未使用，"
            "修改模式下写了新提示词只按新提示词出图\n"
        )
    previous_state = previous_state or {}

    positive_prefix = normalize_text(str(config.get("positive_prefix", "")))
    negative_prefix = normalize_text(str(config.get("negative_prefix", "")))
    # 配置键 nsfw_prefix 已废弃、不再生效：脚本不再往正向里插 nsfw，是否色图只看当轮正文。

    prompt_body = normalize_text(str(normalized.get("prompt", "")))
    previous_prompt_body = normalize_text(str(previous_state.get("prompt_body_used", "")))
    prompt_body_used = ""

    # 只有没写新提示词才沿用上一张（+ 修改指令）；写了就只用新提示词，上一张的内容一概不带。
    if normalized.get("mode") == "revise" and not normalized.get("new_prompt_written"):
        revision_instruction = normalize_text(str(normalized.get("revision_instruction", "")))
        if previous_prompt_body and revision_instruction:
            prompt_body = ", ".join(
                dedupe_keep_order([previous_prompt_body, revision_instruction])
            )
        elif previous_prompt_body:
            prompt_body = previous_prompt_body
        elif revision_instruction:
            prompt_body = revision_instruction

    # 判定域 = 当轮正文（修改模式是合并之后的结果）：先去掉重复的 nsfw，再看还有没有 nsfw 标签。
    # intent、reply_text、上一张的回复原文、画风前缀都不参与；"上一张是色图"本身也不沿用。
    prompt_body = dedupe_nsfw_tags(prompt_body)
    nsfw_enabled = count_nsfw_tags(prompt_body) > 0

    if normalized.get("override_full_prompt"):
        final_positive = prompt_body
        prompt_body_used = prompt_body
        positive_prefix_used = ""
    else:
        if not prompt_body:
            raise ValueError("intermediate 缺少 prompt")
        prompt_body_used = prompt_body
        final_positive = ", ".join(dedupe_keep_order([positive_prefix, prompt_body_used]))
        positive_prefix_used = positive_prefix

    if not final_positive:
        raise ValueError("intermediate 缺少 prompt")

    # r3：反向恒等于基础反向词，不加不删 nsfw（脚本两边都不猜）。
    final_negative = ", ".join(dedupe_keep_order([negative_prefix]))
    return {
        "positive_prefix_used": positive_prefix_used,
        "prompt_body_used": prompt_body_used,
        "final_positive_prompt": final_positive,
        "negative_prefix_used": negative_prefix,
        "final_negative_prompt": final_negative,
        "reply_text": build_reply_text(normalized),
        "mode": normalized.get("mode", "new"),
        "nsfw_enabled": "true" if nsfw_enabled else "false",
        "normalized_intermediate": normalized,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build final NovelAI prompts.")
    parser.add_argument("--intermediate", required=True, help="Path to structured JSON")
    parser.add_argument("--config", required=True, help="Path to config JSON")
    parser.add_argument("--previous-state", help="Optional path to last_request.json")
    parser.add_argument("--output", help="Optional path to write prompt JSON")
    args = parser.parse_args()

    path = Path(args.intermediate)
    if path.suffix.lower() == ".json":
        intermediate: dict[str, Any] | str = load_json(path)
    else:
        intermediate = path.read_text(encoding="utf-8")
    config = load_json(Path(args.config))
    previous_state = load_json(Path(args.previous_state)) if args.previous_state else None
    prompts = build_prompts(config, intermediate, previous_state=previous_state)

    if args.output:
        output_path = Path(args.output)
        output_path.write_text(
            json.dumps(prompts, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    else:
        print(json.dumps(prompts, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
