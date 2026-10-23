"""claude_cli 的命令行路径带辅助调用隔离参数、缺省在每次新建的空临时目录里跑（不调真实 claude，替换 subprocess.run 记参数）。"""
import os
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import claude_cli  # noqa: E402


def _run_cli_path(monkeypatch, **kw):
    seen = {}

    def fake_run(args, **opts):
        cwd = opts.get("cwd")
        seen.update(args=list(args), cwd=cwd, input=opts.get("input"),
                    cwd_listing=os.listdir(cwd) if cwd and os.path.isdir(cwd) else None)
        return types.SimpleNamespace(returncode=0, stdout="好\n", stderr="")

    monkeypatch.setenv("CLAUDEBOTLIFE_FORCE_CLAUDE", "1")   # 走命令行路径，不走 DeepSeek
    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    out = claude_cli.call_claude("写一句话", **kw)
    return out, seen


def test_命令行路径带隔离参数_提示词仍走标准输入(monkeypatch):
    out, seen = _run_cli_path(monkeypatch, model="haiku")
    assert out == "好"
    args = seen["args"]
    assert args[0] == claude_cli.CLAUDE_BIN and args[1] == "--print"
    assert args[args.index("--setting-sources") + 1] == "project,local"
    assert "--strict-mcp-config" in args and "--no-session-persistence" in args
    assert args[args.index("--model") + 1] == "haiku"
    assert seen["input"].endswith("写一句话") and not any("写一句话" in a for a in args)


def test_缺省工作目录每次新建_调用时为空_用完即删(monkeypatch):
    _, seen = _run_cli_path(monkeypatch)
    _, seen2 = _run_cli_path(monkeypatch)
    assert os.path.basename(seen["cwd"]).startswith("claudebotlife-oneshot-")
    assert seen["cwd_listing"] == []
    assert seen["cwd"] != seen2["cwd"]
    assert not os.path.exists(seen["cwd"])
    assert "--model" not in seen["args"]


def test_显式工作目录照用(monkeypatch, tmp_path):
    _, seen = _run_cli_path(monkeypatch, cwd=str(tmp_path))
    assert seen["cwd"] == str(tmp_path)
