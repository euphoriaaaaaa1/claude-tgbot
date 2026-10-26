# -*- coding: utf-8 -*-
"""pub-draft 起草脚本（claude 后端）验收：B6-B17、B23-B25。

全部子进程运行 scripts/draft_proactive_config.py；模型是回环假服务。
"""
import os
import re
import stat

import pytest
import yaml

import pd_helpers as H
from conftest import BOT, write_bot_cfg


def _setup(pd_env, base_url, key=H.MODEL_KEY, persona=H.PERSONA):
    channel = write_bot_cfg(pd_env, extra="")
    H.write_model_cfg(pd_env, base_url, key=key)
    H.write_persona(channel, persona)
    return channel


def _draft_path(pd_env, name=None, cwd=None):
    return (cwd or pd_env["work"]) / (name or ("%s-proactive-draft.yml" % BOT))


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.mark.parametrize("variant", ["未配置密钥", "不存在的bot", "人设为空"])
def test_B6_B7_前置错误退出2且零请求(pd_env, variant):
    srv = H.model_stub(H.valid_draft_yaml())
    try:
        if variant == "未配置密钥":
            channel = write_bot_cfg(pd_env)
            H.write_model_cfg(pd_env, srv.url, key=None)
            H.write_persona(channel)
        elif variant == "不存在的bot":
            H.write_model_cfg(pd_env, srv.url)
        else:
            channel = write_bot_cfg(pd_env)
            H.write_model_cfg(pd_env, srv.url)
            H.write_persona(channel, "")
        r = H.run_draft([BOT], pd_env)
    finally:
        srv.stop()
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert srv.hits == [], "前置错误不该发请求"
    H.no_sensitive_output(r)


def test_B8_成功生成草稿_四行输出_0600(pd_env):
    srv = H.model_stub(H.valid_draft_yaml())
    try:
        _setup(pd_env, srv.url)
        r = H.run_draft([BOT], pd_env, cwd=pd_env["work"])
    finally:
        srv.stop()
    assert r.returncode == 0, r.stderr
    path = _draft_path(pd_env)
    assert path.is_file(), list(pd_env["work"].iterdir())
    assert os.path.abspath(path) == os.path.join(os.path.abspath(pd_env["work"]),
                                                 "%s-proactive-draft.yml" % BOT)
    assert _mode(path) == 0o600
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["persona_summary"] == "摘要值"
    assert data["speaking_threshold"] == "阈值值"
    assert data["jiwen"]["delta_hints"] == "思念规则"
    assert data["sleep_hours"] == ["00:30-06:30"]
    lines = r.stdout.strip().splitlines()
    assert len(lines) == 4, lines
    assert all(re.fullmatch(r"(persona_summary|speaking_threshold|jiwen\.delta_hints)：\d+ 字", l)
               for l in lines[:3]), lines
    assert re.fullmatch(r"sleep_hours：\d+ 段", lines[3]), lines
    H.no_sensitive_output(r, extra_texts=("摘要值", "阈值值", "思念规则"))
    assert H.PERSONA not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("status", [500, 302], ids=["HTTP500", "HTTP302不跟随"])
def test_B9_B10_请求失败退出3且不生成落点(pd_env, status):
    def handler(_req, _raw):
        headers = {"Content-Type": "application/json"}
        if status == 302:
            headers["Location"] = "/redirected"
        return status, headers, '{"%s": "body"}' % H.RESP_ID
    srv = H.FakeHTTP(handler)
    try:
        _setup(pd_env, srv.url)
        r = H.run_draft([BOT], pd_env, cwd=pd_env["work"])
    finally:
        srv.stop()
    assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)
    assert ("请求 DeepSeek 失败：HTTP %d" % status) in r.stderr
    assert len(r.stderr.strip().splitlines()) == 1, r.stderr
    assert not _draft_path(pd_env).exists()
    if status == 302:
        assert len(srv.hits) == 1, "30x 不该跟随重定向"
    H.no_sensitive_output(r)


BAD_RESPONSES = [
    ("缺sleep_hours", "persona_summary: 摘要值\nspeaking_threshold: 阈值值\njiwen:\n  delta_hints: 规则\n",
     "end_turn", True),
    ("空项", "persona_summary: \"\"\nspeaking_threshold: 阈值值\njiwen:\n  delta_hints: 规则\nsleep_hours: []\n",
     "end_turn", True),
    ("sleep不合规", "persona_summary: 摘要值\nspeaking_threshold: 阈值值\njiwen:\n  delta_hints: 规则\nsleep_hours:\n  - \"0:30-06:30\"\n",
     "end_turn", True),
    ("被max_tokens截断", None, "max_tokens", True),
    ("回包无正文", None, "end_turn", False),
]


@pytest.mark.parametrize("name,text,stop_reason,draft_expected", BAD_RESPONSES,
                         ids=[c[0] for c in BAD_RESPONSES])
def test_B11a_b_c_d_回包不合规(pd_env, name, text, stop_reason, draft_expected):
    text = H.valid_draft_yaml() if text is None and stop_reason == "max_tokens" else text
    srv = H.model_stub(text, stop_reason=stop_reason)
    try:
        _setup(pd_env, srv.url)
        r = H.run_draft([BOT], pd_env, cwd=pd_env["work"])
    finally:
        srv.stop()
    assert r.returncode == 4, (name, r.returncode, r.stdout, r.stderr)
    path = _draft_path(pd_env)
    if draft_expected:
        assert path.is_file(), "%s：回了正文时草稿要落盘" % name
        assert path.read_text(encoding="utf-8").strip() == text.strip()
    else:
        assert not path.exists()
    H.no_sensitive_output(r)


VALID_SLEEP = [
    ([], [], "空列表清空"),
    (["00:00-00:00"], ["00:00-00:00"], "起止相同"),
    (["00:30-06:30", "22:00-01:00"], ["00:30-06:30", "22:00-01:00"], "跨午夜"),
]
INVALID_SLEEP = [
    ["0:30-06:30"], ["00:3-06:30"], ["00:30-6:30"], ["24:00-06:00"],
    ["00:60-01:00"], ["00:30-06:30\n"], ["００:３０-０６:３０"], [1],
    ["00:00-01:00"] * 7,
]


@pytest.mark.parametrize("sleep,expected", [(c[0], c[1]) for c in VALID_SLEEP],
                         ids=[c[2] for c in VALID_SLEEP])
def test_B12_sleep_hours合规_apply成功(pd_env, sleep, expected):
    cfg = write_bot_cfg(pd_env, extra="sleep_hours:\n  - \"01:00-02:00\"\n")
    draft = pd_env["work"] / "draft.yml"
    draft.write_text(H.valid_draft_yaml(sleep_hours=sleep), encoding="utf-8")
    r = H.run_draft([BOT, "--apply", str(draft)], pd_env)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert H.load_yaml(cfg)["sleep_hours"] == expected


@pytest.mark.parametrize("sleep", INVALID_SLEEP,
                         ids=["一位时", "一位分", "分一位", "24时", "60分", "行尾换行",
                              "全角数字", "非字符串", "七项"])
def test_B12_sleep_hours不合规_apply退出4配置不变(pd_env, sleep):
    cfg = write_bot_cfg(pd_env, extra="sleep_hours:\n  - \"01:00-02:00\"\n")
    before = H.sha256(cfg)
    draft = pd_env["work"] / "draft.yml"
    draft.write_text(H.valid_draft_yaml(sleep_hours=sleep), encoding="utf-8")
    r = H.run_draft([BOT, "--apply", str(draft)], pd_env)
    assert r.returncode == 4, (r.returncode, r.stdout, r.stderr)
    assert H.sha256(cfg) == before


def test_B13_B14_落点冲突与非法位置(pd_env):
    srv = H.model_stub(H.valid_draft_yaml())
    try:
        _setup(pd_env, srv.url)
        # 已存在且没 --force：不覆盖
        existing = _draft_path(pd_env)
        existing.write_text("旧内容", encoding="utf-8")
        existing.chmod(0o644)
        r = H.run_draft([BOT], pd_env, cwd=pd_env["work"])
        assert r.returncode == 2, (r.returncode, r.stderr)
        assert existing.read_text(encoding="utf-8") == "旧内容"
        assert _mode(existing) == 0o644
        # 带 --force：覆盖且 0600
        r = H.run_draft([BOT, "--force"], pd_env, cwd=pd_env["work"])
        assert r.returncode == 0, (r.returncode, r.stderr)
        assert _mode(existing) == 0o600
        assert H.PERSONA not in existing.read_text(encoding="utf-8")

        # 落点问题：目录 / 父目录不存在 / 配置目录 / 密钥目录 / 符号链接指进密钥目录
        a_dir = pd_env["work"] / "a-dir"
        a_dir.mkdir()
        target = pd_env["secrets"] / "should-not-exist.yml"
        link = pd_env["work"] / "link.yml"
        link.symlink_to(target)
        bad_outs = [
            ("目录", a_dir),
            ("父目录不存在", pd_env["work"] / "no" / "such" / "x.yml"),
            ("配置目录内", pd_env["configs"] / "evil-draft.yml"),
            ("密钥目录内", pd_env["secrets"] / "evil-draft.yml"),
            ("符号链接进密钥目录", link),
        ]
        for name, out in bad_outs:
            r = H.run_draft([BOT, "--out", str(out)], pd_env, cwd=pd_env["work"])
            assert r.returncode == 2, (name, r.returncode, r.stdout, r.stderr)
            if name == "目录":
                assert out.is_dir()
            else:
                assert not out.exists() or out.is_symlink(), name
        assert not target.exists()
    finally:
        srv.stop()


def test_B15_apply成功_四项写入其他键相等_新增一份备份(pd_env):
    cfg = write_bot_cfg(pd_env, extra=(
        "bio: 旧简介\npersona_summary: 旧摘要\nspeaking_threshold: 旧阈值\n"
        "sleep_hours:\n  - \"01:00-02:00\"\n"
        "jiwen:\n  delta_hints: 旧规则\n  rates_override:\n    connection_per_min: 0.1\n"))
    before = H.load_yaml(cfg)
    backups_before = set(pd_env["configs"].glob("*bak-life-*"))
    draft = pd_env["work"] / "draft.yml"
    draft.write_text(H.valid_draft_yaml(), encoding="utf-8")
    r = H.run_draft([BOT, "--apply", str(draft)], pd_env)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    after = H.load_yaml(cfg)
    assert after["persona_summary"] == "摘要值"
    assert after["speaking_threshold"] == "阈值值"
    assert after["jiwen"]["delta_hints"] == "思念规则"
    assert after["sleep_hours"] == ["00:30-06:30"]
    assert after["bio"] == before["bio"]
    assert after["jiwen"]["rates_override"] == before["jiwen"]["rates_override"]
    assert after["id"] == before["id"] and after["chat_id"] == before["chat_id"]
    new_backups = set(pd_env["configs"].glob("*bak-life-*")) - backups_before
    assert len(new_backups) == 1, new_backups
    H.no_sensitive_output(r)


def test_B11e_f_g_B16_B17_apply草稿不合规与缺省sleep(pd_env):
    cfg = write_bot_cfg(pd_env, extra="sleep_hours:\n  - \"01:00-02:00\"\n")

    def run_draft_text(text, expect, label):
        path = pd_env["work"] / ("d-%s.yml" % label)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8")
        before = H.sha256(cfg)
        r = H.run_draft([BOT, "--apply", str(path)], pd_env)
        assert r.returncode == expect, (label, r.returncode, r.stdout, r.stderr)
        if expect == 4:
            assert H.sha256(cfg) == before, label
        return r

    full = {"persona_summary": "摘要值", "speaking_threshold": "阈值值",
            "jiwen": {"delta_hints": "规则"}, "sleep_hours": ["00:30-06:30"]}
    missing = dict(full)
    missing.pop("speaking_threshold")
    empty = dict(full, persona_summary="")
    flat = """persona_summary: 摘要值
speaking_threshold: 阈值值
sleep_hours:
  - "00:30-06:30"
"""
    for text, label in [
        (yaml.safe_dump(missing, allow_unicode=True), "缺项"),
        (yaml.safe_dump(empty, allow_unicode=True), "空项"),
        ("persona_summary: [unclosed\n", "不是YAML"),
        ("- a\n- b\n", "不是键值表"),
        (yaml.safe_dump(dict(full, persona_summary="长" * 4001), allow_unicode=True), "超4000字"),
        ('persona_summary: "a\\x01b"\n' + yaml.safe_dump({k: v for k, v in full.items() if k != "persona_summary"}, allow_unicode=True), "控制字符"),
        (b"\xff\xfe\x00", "非UTF8"),
    ]:
        r = run_draft_text(text, 4, label)
        H.no_sensitive_output(r)

    # 草稿读不出来 → 2
    r = H.run_draft([BOT, "--apply", str(pd_env["work"] / "no-such.yml")], pd_env)
    assert r.returncode == 2, (r.returncode, r.stderr)
    denied = pd_env["work"] / "denied.yml"
    denied.write_text(yaml.safe_dump(full, allow_unicode=True), encoding="utf-8")
    denied.chmod(0o000)
    r = H.run_draft([BOT, "--apply", str(denied)], pd_env)
    assert r.returncode == 2, (r.returncode, r.stderr)

    # B17：只有三项（无 sleep_hours）→ 成功，原 sleep_hours 不变
    r = run_draft_text(flat, 0, "三项")
    assert r.returncode == 0
    assert H.load_yaml(cfg)["sleep_hours"] == ["01:00-02:00"]


@pytest.mark.parametrize("case", [
    "http://192.168.1.5:1",
    "ftp://example.com",
    "http://user@127.0.0.1:1",
    "http://127.0.0.1:1\\x",
    "http://127.0.0.1:1 /x",
    "LOOPBACK",
    "ENV覆盖",
], ids=["非回环http", "ftp", "含at", "含反斜杠", "含空白", "回环允许", "环境变量覆盖基址"])
def test_B24_地址校验(pd_env, case):
    srv = H.model_stub(H.valid_draft_yaml()) if case in ("LOOPBACK", "ENV覆盖") else None
    try:
        channel = write_bot_cfg(pd_env)
        extra = {}
        if case == "ENV覆盖":
            # 配置里故意放不合规地址，只靠 PROACTIVE_DRAFT_BASE_URL 覆盖（接口四之三）
            H.write_model_cfg(pd_env, "ftp://invalid.example")
            extra["PROACTIVE_DRAFT_BASE_URL"] = srv.url
        else:
            H.write_model_cfg(pd_env, srv.url if srv else case)
        H.write_persona(channel)
        r = H.run_draft([BOT], pd_env, cwd=pd_env["work"], extra_env=extra)
    finally:
        if srv is not None:
            srv.stop()
    if case in ("LOOPBACK", "ENV覆盖"):
        assert r.returncode == 0, (r.returncode, r.stderr)
        assert len(srv.hits) == 1
    else:
        assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
        assert not _draft_path(pd_env).exists()
    H.no_sensitive_output(r)


def test_B25_人设超256KiB退出2且零请求(pd_env):
    srv = H.model_stub(H.valid_draft_yaml())
    try:
        channel = _setup(pd_env, srv.url, persona="a" * (256 * 1024 + 1))
        assert (channel / "CLAUDE.md").stat().st_size > 256 * 1024
        r = H.run_draft([BOT], pd_env, cwd=pd_env["work"])
    finally:
        srv.stop()
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert srv.hits == []
    assert not _draft_path(pd_env).exists()


def test_B28_gitignore命中草稿文件(pd_env):
    import subprocess
    r = subprocess.run(["git", "check-ignore", "-q", "%s-proactive-draft.yml" % BOT],
                       cwd=str(H.REPO), capture_output=True, text=True)
    assert r.returncode == 0, "仓根 .gitignore 未覆盖 *-proactive-draft.yml"
