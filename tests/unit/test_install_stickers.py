"""scripts/install_stickers.py：随包表情包安装（首次生成、已有清单跳过、幂等、不覆盖图片）。
全部在临时 HOME 下跑真脚本，不碰真实家目录。
跑：python3 -m pytest tests/unit/test_install_stickers.py -q
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "install_stickers.py"
BUNDLE = ROOT / "assets" / "stickers" / "sfw"


def run(home: Path, repo: Path = ROOT, stickers_dir: str | None = None):
    env = {k: v for k, v in os.environ.items() if k != "STICKERS_DIR"}
    env.update(HOME=str(home), USERPROFILE=str(home), PYTHONUTF8="1")
    if stickers_dir is not None:
        env["STICKERS_DIR"] = stickers_dir
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), "--home", str(home)],
        capture_output=True, text=True, env=env,
    )


def manifest_entries():
    return json.loads((BUNDLE / "manifest.json").read_text(encoding="utf-8"))["files"]


def stickers_json(home: Path) -> Path:
    return home / ".claude" / "channels" / "_shared" / "stickers.json"


def snapshot(directory: Path) -> dict:
    return {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in sorted(directory.iterdir())}


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


def test_素材包与manifest一一对应():
    entries = manifest_entries()
    names = [e["file"] for e in entries]
    assert len(names) == len(set(names)) == 84
    assert sorted(names) == sorted(p.name for p in (BUNDLE / "files").iterdir())


def test_首次安装_复制图片并生成sfw清单(home: Path):
    r = run(home)
    assert r.returncode == 0, r.stderr
    sticker_dir = home / "resource" / "stickers" / "sfw"
    data = json.loads(stickers_json(home).read_text(encoding="utf-8"))
    assert data["nsfw"] == []
    entries = manifest_entries()
    assert len(data["sfw"]) == len(entries)
    by_path = {item["path"]: item for item in data["sfw"]}
    for entry in entries:
        dest = sticker_dir / entry["file"]
        item = by_path[str(dest)]
        # gif 技能约定的字段：url / label / path；path 是复制后的绝对路径。
        assert set(item) >= {"url", "label", "path", "id", "tier"}
        assert Path(item["path"]).is_absolute()
        assert item["label"] == entry["label"] and item["tier"] == "sfw"
        assert item.get("category") == entry["category"]  # null 分类不写键
        assert dest.read_bytes() == (BUNDLE / "files" / entry["file"]).read_bytes()
    assert len({item["id"] for item in data["sfw"]}) == len(entries)


def test_清单已存在_整步跳过且不改动(home: Path):
    target = stickers_json(home)
    target.parent.mkdir(parents=True)
    original = b'{"sfw": [{"url": "u", "label": "mine", "path": "/x"}], "nsfw": []}'
    target.write_bytes(original)
    r = run(home)
    assert r.returncode == 0, r.stderr
    assert "跳过" in r.stdout
    assert target.read_bytes() == original
    assert not (home / "resource").exists()
    assert sorted(p.name for p in target.parent.iterdir()) == ["stickers.json"]


def test_清单已存在时_STICKERS_DIR非法也只跳过(home: Path):
    target = stickers_json(home)
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")
    r = run(home, stickers_dir="relative/dir")
    assert r.returncode == 0, r.stderr
    assert target.read_text(encoding="utf-8") == "{}"


def test_重复执行幂等(home: Path):
    assert run(home).returncode == 0
    sticker_dir = home / "resource" / "stickers" / "sfw"
    before_json = stickers_json(home).read_bytes()
    before_files = snapshot(sticker_dir)
    r = run(home)
    assert r.returncode == 0, r.stderr
    assert "跳过" in r.stdout
    assert stickers_json(home).read_bytes() == before_json
    assert snapshot(sticker_dir) == before_files


def test_已有同名图片不覆盖_清单仍指向它(home: Path):
    first = manifest_entries()[0]["file"]
    sticker_dir = home / "resource" / "stickers" / "sfw"
    sticker_dir.mkdir(parents=True)
    (sticker_dir / first).write_bytes(b"user-own-image")
    r = run(home)
    assert r.returncode == 0, r.stderr
    assert (sticker_dir / first).read_bytes() == b"user-own-image"
    paths = [item["path"] for item in json.loads(stickers_json(home).read_text(encoding="utf-8"))["sfw"]]
    assert str(sticker_dir / first) in paths
    assert not [p for p in sticker_dir.iterdir() if p.name.startswith(".")]  # 不留临时文件


@pytest.mark.parametrize("raw, expect", [("~/custom/st", "home/custom/st"), ("ABS", "abs-root")])
def test_STICKERS_DIR_与dispatcher同口径(home: Path, tmp_path: Path, raw: str, expect: str):
    if raw == "ABS":
        raw = str(tmp_path / "abs-root")
    r = run(home, stickers_dir=raw)
    assert r.returncode == 0, r.stderr
    sticker_dir = tmp_path / expect / "sfw"
    assert len(list(sticker_dir.iterdir())) == 84
    for item in json.loads(stickers_json(home).read_text(encoding="utf-8"))["sfw"]:
        assert Path(item["path"]).parent == sticker_dir


def test_STICKERS_DIR相对路径_退2且不写任何东西(home: Path):
    r = run(home, stickers_dir="relative/dir")
    assert r.returncode == 2
    assert not stickers_json(home).exists()


def test_manifest含路径穿越_退1不写清单(home: Path, tmp_path: Path):
    repo = tmp_path / "repo"
    bundle = repo / "assets" / "stickers" / "sfw"
    shutil.copytree(BUNDLE, bundle)
    data = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    data["files"][0]["file"] = "../escape.jpg"
    (bundle / "manifest.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    r = run(home, repo=repo)
    assert r.returncode == 1
    assert not stickers_json(home).exists()
    assert not (home / "resource" / "stickers" / "escape.jpg").exists()


def test_素材缺图_退1不写清单_补齐后重跑成功(home: Path, tmp_path: Path):
    repo = tmp_path / "repo"
    bundle = repo / "assets" / "stickers" / "sfw"
    shutil.copytree(BUNDLE, bundle)
    missing = manifest_entries()[3]["file"]
    (bundle / "files" / missing).unlink()
    r = run(home, repo=repo)
    assert r.returncode == 1
    assert not stickers_json(home).exists()
    shutil.copy(BUNDLE / "files" / missing, bundle / "files" / missing)
    assert run(home, repo=repo).returncode == 0
    assert len(json.loads(stickers_json(home).read_text(encoding="utf-8"))["sfw"]) == 84


def test_安装入口已接线():
    assert "scripts/install_stickers.py" in (ROOT / "install.sh").read_text(encoding="utf-8")
    ps1 = (ROOT / "windows" / "register-tasks.ps1").read_text(encoding="utf-8-sig")
    assert "install_stickers.py" in ps1


def load_script_module():
    spec = importlib.util.spec_from_file_location("install_stickers_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_复制中途出错_不写清单_修好后重跑补齐(home: Path, monkeypatch):
    module = load_script_module()
    monkeypatch.delenv("STICKERS_DIR", raising=False)
    real_copyfile = module.shutil.copyfile
    calls = {"n": 0}

    def flaky_copyfile(src, dst, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 10:  # 第 10 张时磁盘出错，前 9 张已复制
            raise OSError("模拟磁盘写入失败")
        return real_copyfile(src, dst, *args, **kwargs)

    monkeypatch.setattr(module.shutil, "copyfile", flaky_copyfile)
    argv = ["--repo", str(ROOT), "--home", str(home)]
    assert module.main(argv) == 1
    sticker_dir = home / "resource" / "stickers" / "sfw"
    assert not stickers_json(home).exists()
    assert len(list(sticker_dir.iterdir())) == 83  # 只缺出错那一张，也不留临时文件
    monkeypatch.setattr(module.shutil, "copyfile", real_copyfile)
    assert module.main(argv) == 0
    assert len(list(sticker_dir.iterdir())) == 84
    assert len(json.loads(stickers_json(home).read_text(encoding="utf-8"))["sfw"]) == 84


def test_目标位置是同名目录_记失败不写清单(home: Path):
    first = manifest_entries()[0]["file"]
    sticker_dir = home / "resource" / "stickers" / "sfw"
    (sticker_dir / first).mkdir(parents=True)
    r = run(home)
    assert r.returncode == 1
    assert first in r.stderr
    assert not stickers_json(home).exists()
    assert (sticker_dir / first).is_dir()


@pytest.mark.parametrize("bad_name", ["a:b.jpg", "C:evil.jpg", "x.jpg:stream"])
def test_manifest文件名含冒号_被拒(home: Path, tmp_path: Path, bad_name: str):
    repo = tmp_path / "repo"
    bundle = repo / "assets" / "stickers" / "sfw"
    shutil.copytree(BUNDLE, bundle)
    data = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    data["files"][0]["file"] = bad_name
    (bundle / "manifest.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    r = run(home, repo=repo)
    assert r.returncode == 1
    assert not stickers_json(home).exists()
    assert not (home / "resource").exists()
