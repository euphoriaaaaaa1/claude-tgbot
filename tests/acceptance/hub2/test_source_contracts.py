# -*- coding: utf-8 -*-
"""源码级契约：§2.4「禁止 safe_dump（可 grep 断言）」、BRIEF 需求 4 的 5 处硬编码收敛、
§6「消费方最小正确写法」。

这些是 INTERFACE / BRIEF 明确点名"可 grep 断言"的项，不算读实现 —— 只做存在性检查。
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SKIP_DIRS = {".git", "node_modules", "__pycache__", "tests", ".devflow",
             "downloads", "dist", "build", "channels", "configs"}


def _iter_sources(*globs):
    for pat in globs:
        for p in REPO_ROOT.rglob(pat):
            if any(part in SKIP_DIRS for part in p.relative_to(REPO_ROOT).parts):
                continue
            if p.is_file():
                yield p


def _read(p):
    return p.read_text(encoding="utf-8", errors="ignore")


def test_门户代码里不出现safe_dump():
    """§2.4：禁止 `yaml.safe_dump` 出现在任何写路径 —— 一 dump 锚点和注释就没了。"""
    hits = [f"{p.relative_to(REPO_ROOT)}" for p in (REPO_ROOT / "moments").rglob("*.py")
            if "__pycache__" not in str(p) and "safe_dump" in _read(p)]
    assert hits == [], f"这些文件里有 safe_dump：{hits}"


def test_重启脚本读注册表而不是硬编码bot列表():
    """BRIEF 需求 4 第 1 处 + F4 自检的 grep 目标。"""
    p = REPO_ROOT / "restart-bots.example.sh"
    assert p.exists(), "找不到 restart-bots.example.sh"
    txt = _read(p)
    assert "bots_registry" in txt, "重启脚本没接注册表，加 bot 仍要手改"
    assert not re.search(r"^\s*BOTS=\(", txt, re.M), "还留着硬编码 BOTS=(...)"


def test_重启脚本按契约捕获退出码而不是管道吞掉():
    """§6 R5：`cmd | while read` 会把 CLI 的非零码吞掉，脚本照样"成功"跑完 0 个 bot。"""
    txt = _read(REPO_ROOT / "restart-bots.example.sh")
    assert "bots_registry" in txt
    assert not re.search(r"bots_registry[^\n]*\|\s*while", txt), "用了会吞退出码的管道写法"
    assert re.search(r'\$\(\s*(python3?|py)[^)]*bots_registry[^)]*\)', txt), \
        "没用 $(...) 捕获输出并判退出码"


def _restart_script_target():
    """A6 的断言读哪个文件：优先 §7 接缝 `HUB_RESTART_SCRIPT` 指的**现存**文件，
    否则退回版本库里的 `restart-bots.example.sh`（install.sh 的拷贝源，唯一受版本管理的那份）。
    夹具默认把接缝指到不存在的路径，所以常规跑法下走的是后者。"""
    env = os.environ.get("HUB_RESTART_SCRIPT")
    if env and Path(env).is_file():
        return Path(env)
    return REPO_ROOT / "restart-bots.example.sh"


def test_重启脚本把注册表第三列注入BOT_NAMESPACE():
    """§13 A6：收敛动作 = 启动脚本多注一个 `-e BOT_NAMESPACE=$ns`（TS 源码零改动）。
    漏了这行，「零改动即纳管」对新 bot 的**记忆命名空间**就落空 —— 新 bot 会去读兜底表里
    根本没有的条目，历史与记忆挂错人，而重启本身照样"成功"。"""
    p = _restart_script_target()
    assert p.exists(), f"找不到重启脚本：{p}"
    txt = _read(p)
    m = re.search(r"while[^\n]*read\s+(?:-r\s+)?(\w+)\s+(\w+)\s+(\w+)", txt)
    assert m, "没按 §6「消费方最小正确写法」的三列读法消费注册表"
    ns_var = m.group(3)
    assert re.search(rf"BOT_NAMESPACE=[\"']?\$\{{?{ns_var}\b", txt), \
        f"没把注册表第三列（${ns_var}）注入 BOT_NAMESPACE：{p.name}"


@pytest.mark.parametrize("name,marker", [
    pytest.param("start-bots.ps1", r"\$Bots\s*=\s*@?\(", id="windows启动脚本"),
    pytest.param("register-tasks.ps1", r"\$SelfInitBot\s*=\s*[\"']", id="windows注册任务"),
])
def test_windows脚本不再硬编码bot(name, marker):
    found = list(_iter_sources(name))
    if not found:
        pytest.skip(f"仓库里没有 {name}")
    for p in found:
        txt = _read(p)
        assert not re.search(marker, txt), f"{p.name} 还留着硬编码 bot 列表"
        assert "bots_registry" in txt, f"{p.name} 没接注册表"


@pytest.mark.parametrize("marker,why", [
    pytest.param("_BOT_PORTS", "web 的端口表", id="web端口表"),
    pytest.param("_BOT_NAMESPACES", "chat_history 的命名空间表", id="命名空间表"),
])
def test_python侧的bot表必须来自注册表(marker, why):
    """BRIEF 需求 4：5 处硬编码收敛为读同一配置源，加 bot 零代码改动。
    判据取"还留着这个名字的文件必须同时读注册表"，允许它变成派生出来的同名变量。"""
    bad = []
    for p in _iter_sources("*.py"):
        if p.name.startswith("bots_registry"):
            continue
        txt = _read(p)
        if marker in txt and "bots_registry" not in txt:
            bad.append(str(p.relative_to(REPO_ROOT)))
    assert bad == [], f"{why} 还是硬编码的（没读注册表）：{bad}"


def test_dispatcher侧TS保留BOT_NAMESPACE环境变量接缝():
    """§8 承诺 dispatcher/ 全部 TS 零改动，靠的就是这个 env 接缝。
    §13 A6 裁决：与 BRIEF 需求 4 不是冲突 —— env 已优先于表，收敛动作落在启动脚本
    （见上一条 BOT_NAMESPACE 注入），TS 源码零改动。"""
    ts = list(_iter_sources("worker-manager.ts"))
    if not ts:
        pytest.skip("仓库里没有 worker-manager.ts")
    assert any("BOT_NAMESPACE" in _read(p) for p in ts), "env 接缝没了，§8 的零改动承诺落空"


def test_源码里不再散落chenlulu硬编码():
    """legacy 兜底表只允许待在 bots_registry 一处（§6 明写它是兜底）。"""
    hits = []
    for p in _iter_sources("*.py", "*.ts", "*.sh", "*.ps1"):
        rel = str(p.relative_to(REPO_ROOT))
        if rel.startswith("bots_registry") or "example" in rel:
            continue
        if "chenlulu" in _read(p):
            hits.append(rel)
    assert hits == [], f"这些文件还写死了 chenlulu：{hits}"


def test_注册表CLI失败时重启脚本非零退出(tmp_path):
    """§6「验收加一条」：把 CLI 换成必定失败的桩 → 脚本必须非零退出。"""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_py = fake_bin / "python3"
    fake_py.write_text("#!/bin/sh\necho 'boom' >&2\nexit 1\n", encoding="utf-8")
    fake_py.chmod(0o755)
    env = dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}")
    script = REPO_ROOT / "restart-bots.example.sh"
    p = subprocess.run(["/bin/sh", str(script)], cwd=str(tmp_path), env=env,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode != 0, "CLI 失败了脚本却报成功 —— 退出码被管道吞了"
    assert p.stderr.strip() != "", "失败时至少要有一行错误说明"
