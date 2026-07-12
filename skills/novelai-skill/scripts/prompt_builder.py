#!/usr/bin/env python3
"""Build final NovelAI prompts from structured intermediate JSON."""

from __future__ import annotations

import argparse
import json
import re
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
NSFW_KEYWORDS = (
    "nsfw",
    "nude",
    "naked",
    "nipples",
    "breasts",
    "cleavage",
    "pussy",
    "penis",
    "vaginal",
    "fellatio",
    "cum",
    "orgasm",
    "自慰",
    "裸体",
    "裸",
    "乳头",
    "胸",
    "阴部",
    "阴蒂",
    "阴唇",
    "肉棒",
    "鸡巴",
    "插入",
    "口交",
    "做爱",
    "性交",
    "骚",
    "发情",
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


def detect_nsfw(normalized: dict[str, Any], previous_state: dict[str, Any] | None = None) -> bool:
    previous_state = previous_state or {}
    explicit = normalized.get("nsfw")
    if isinstance(explicit, bool):
        return explicit

    rating = normalize_text(str(normalized.get("rating", ""))).lower()
    if rating in {"nsfw", "explicit", "adult", "r18", "r-18"}:
        return True

    mode = normalize_text(str(normalized.get("mode", ""))).lower()
    if mode in {"nsfw", "adult", "explicit", "ero"}:
        return True

    corpus = " ".join(
        normalize_text(str(value))
        for value in (
            normalized.get("intent", ""),
            normalized.get("reply_text", ""),
            normalized.get("prompt", ""),
            normalized.get("revision_instruction", ""),
            previous_state.get("prompt_body_used", ""),
            previous_state.get("reply_text", ""),
        )
        if str(value).strip()
    ).lower()
    return any(keyword in corpus for keyword in NSFW_KEYWORDS)


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
            remainder = remainder.lstrip("，,。.!！?？:： ")
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


def normalize_intermediate(intermediate: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(intermediate, str):
        mode, phrase, revision = infer_mode_and_revision({"prompt": intermediate})
        prompt_text = normalize_text(intermediate if mode == "new" else "")
        return {
            "prompt": prompt_text,
            "reply_text": "",
            "mode": mode,
            "revision_trigger": phrase,
            "revision_instruction": revision,
            "override_full_prompt": False,
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
    if mode == "revise" and not normalize_text(str(intermediate.get("prompt", ""))):
        prompt = ""

    normalized["prompt"] = normalize_text(str(prompt))
    normalized["reply_text"] = normalize_text(str(intermediate.get("reply_text", "")))
    normalized["mode"] = mode
    normalized["revision_trigger"] = phrase
    normalized["revision_instruction"] = revision
    normalized["override_full_prompt"] = bool(intermediate.get("override_full_prompt", False))
    normalized["intent"] = normalize_text(str(intermediate.get("intent", "")))
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
    normalized = normalize_intermediate(intermediate)
    previous_state = previous_state or {}

    positive_prefix = normalize_text(str(config.get("positive_prefix", "")))
    negative_prefix = normalize_text(str(config.get("negative_prefix", "")))
    nsfw_prefix = normalize_text(str(config.get("nsfw_prefix", "nsfw")))

    prompt_body = normalize_text(str(normalized.get("prompt", "")))
    previous_prompt_body = normalize_text(str(previous_state.get("prompt_body_used", "")))
    prompt_body_used = ""
    nsfw_enabled = detect_nsfw(normalized, previous_state=previous_state)

    if normalized.get("mode") == "revise":
        revision_instruction = normalize_text(str(normalized.get("revision_instruction", "")))
        if previous_prompt_body and revision_instruction:
            prompt_body = ", ".join(
                dedupe_keep_order([previous_prompt_body, revision_instruction])
            )
        elif previous_prompt_body:
            prompt_body = previous_prompt_body
        elif revision_instruction:
            prompt_body = revision_instruction

    if normalized.get("override_full_prompt"):
        final_positive = prompt_body
        prompt_body_used = prompt_body
        positive_prefix_used = ""
    else:
        if not prompt_body:
            raise ValueError("intermediate 缺少 prompt")
        prompt_body_used = prompt_body
        combined = dedupe_keep_order(
            [positive_prefix, nsfw_prefix if nsfw_enabled else "", prompt_body_used]
        )
        final_positive = ", ".join(combined)
        positive_prefix_used = positive_prefix

    if not final_positive:
        raise ValueError("intermediate 缺少 prompt")

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
