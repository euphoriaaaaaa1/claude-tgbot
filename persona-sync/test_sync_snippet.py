#!/usr/bin/env python3
"""sync_snippet.py 的自检。

直接跑：python3 test_sync_snippet.py
pytest 也能收集：pytest test_sync_snippet.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sync_snippet import ConfigError, SyncError, load_config, plan_change  # noqa: E402

START, END = "A-START", "A-END"


def test_append_to_empty_file():
    action, new = plan_change("", "<!-- A-START -->x<!-- A-END -->", START, END)
    assert action == "append"
    assert new == "<!-- A-START -->x<!-- A-END -->\n"


def test_append_after_existing_content():
    action, new = plan_change("# 人设\n", "S", START, END)
    assert action == "append"
    assert new == "# 人设\n\nS\n"


def test_replace_keeps_surroundings_and_is_idempotent():
    old = "# 人设\n\n<!-- A-START\n旧\n-->\n旧正文\n<!-- A-END -->\n\noverride\n"
    snippet = "<!-- A-START · v2 -->\n新正文\n<!-- A-END -->"
    action, new = plan_change(old, snippet, START, END)
    assert action == "replace"
    assert new == "# 人设\n\n" + snippet + "\n\noverride\n"
    # 再算一次：内容一致应 skip（幂等）
    action2, _ = plan_change(new, snippet, START, END)
    assert action2 == "skip"


def test_literal_backslash_kept_intact():
    # snippet 里字面 \n 应按原样写入，不能被当成转义
    snippet = '{"text": "a\\n\\nb"}'
    _, new = plan_change("", snippet, START, END)
    assert 'a\\n\\nb' in new


def test_unpaired_marker_raises():
    for bad in ("<!-- A-START\n没有 END", "<!-- A-END -->\n有 END 没 START"):
        try:
            plan_change(bad, "S", START, END)
        except SyncError:
            continue
        raise AssertionError(f"应报 SyncError: {bad!r}")


def test_duplicated_marker_raises():
    dup = "<!-- A-START -->x<!-- A-END -->\n\n<!-- A-START -->y<!-- A-END -->"
    try:
        plan_change(dup, "S", START, END)
    except SyncError:
        return
    raise AssertionError("重复标记应报 SyncError")


def test_load_config_paths_and_ignored_underscore():
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "snippets.json"
        cfg.write_text(
            '{"_说明": ["注释字段，应被忽略"],'
            ' "targets": ["sub/CLAUDE.md", "~/x/CLAUDE.md"],'
            ' "snippets": [{"source": "s.md", "start": "S", "end": "E"}]}',
            encoding="utf-8",
        )
        targets, snippets = load_config(cfg)
        base = Path(d).resolve()
        assert targets[0] == base / "sub/CLAUDE.md"
        assert str(targets[1]).startswith(str(Path.home()))
        assert snippets == [(base / "s.md", "S", "E")]


def test_load_config_rejects_broken():
    for bad in ('{"targets": ["a.md"], "snippets": []}',
                '{"targets": [], "snippets": [{"source": "s", "start": "S", "end": "E"}]}',
                'not json',
                '[1, 2]'):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "snippets.json"
            cfg.write_text(bad, encoding="utf-8")
            try:
                load_config(cfg)
            except ConfigError:
                continue
            raise AssertionError(f"应报 ConfigError: {bad!r}")


if __name__ == "__main__":
    for name in sorted(n for n in globals() if n.startswith("test_")):
        globals()[name]()
        print(f"✓ {name}")
    print("自检通过")
