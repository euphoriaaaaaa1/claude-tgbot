# -*- coding: utf-8 -*-
"""pub-draft 起草脚本（dsh 后端）验收：B18/B19。

dsh 是回环假适配器（/adapter/v1 形状）；令牌只放临时文件。
"""
import os
import stat

import pytest
import yaml

import pd_helpers as H
from conftest import BOT, CHAT, write_bot_cfg


def _setup_dsh(pd_env, base_url=None, with_token=True, persona=H.PERSONA):
    channel = write_bot_cfg(pd_env, extra="")
    (channel / "access.json").write_text('{"allowFrom": ["%s"]}' % CHAT, encoding="utf-8")
    H.write_persona(channel, persona)
    token_path = pd_env["home"] / ".config" / "claude-dsh-console" / "adapter.token"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    if with_token:
        token_path.write_text(H.DSH_TOKEN, encoding="utf-8")
        token_path.chmod(0o600)
    cfg = {"jiwen": {"tick_interval_min": 5},
           "console": {"dsh_adapter_url": base_url or ""}}
    H.dump_yaml(cfg, pd_env["configs"] / "_global.yml")
    extra = {"CONSOLE_DSH_ADAPTER_URL": base_url or "", "DSH_ADAPTER_URL": base_url or ""}
    if with_token:
        extra["CONSOLE_ADAPTER_TOKEN_FILE"] = str(token_path)
    return channel, extra


def _run(pd_env, args, extra):
    return H.run_draft(args, pd_env, cwd=pd_env["work"], extra_env=extra)


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_B18a_dsh起草成功_只含sleep_hours_单行输出(pd_env):
    srv, _puts = H.dsh_stub()
    try:
        _setup_dsh(pd_env, srv.url)
        extra = {"CONSOLE_DSH_ADAPTER_URL": srv.url, "DSH_ADAPTER_URL": srv.url,
                 "CONSOLE_ADAPTER_TOKEN_FILE": str(pd_env["home"] / ".config" / "claude-dsh-console" / "adapter.token")}
        r = _run(pd_env, [BOT, "--backend", "dsh"], extra)
    finally:
        srv.stop()
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    path = pd_env["work"] / ("dsh-%s-proactive-draft.yml" % BOT)
    assert path.is_file()
    assert _mode(path) == 0o600
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert list(data.keys()) == ["sleep_hours"], data
    assert len(r.stdout.strip().splitlines()) == 1, r.stdout
    H.no_sensitive_output(r)

    # B18c：同一份 dsh 草稿不走 --backend dsh（按 claude 规则）→ 4，配置不变
    cfg = pd_env["configs"] / ("%s.yml" % BOT)
    before = H.sha256(cfg)
    draft = pd_env["work"] / "d-claude.yml"
    draft.write_text(yaml.safe_dump({"sleep_hours": ["00:30-06:30"], "persona_summary": "多余"},
                                    allow_unicode=True), encoding="utf-8")
    r2 = H.run_draft([BOT, "--apply", str(draft)], pd_env)
    assert r2.returncode == 4, (r2.returncode, r2.stdout, r2.stderr)
    assert H.sha256(cfg) == before


def test_B18b_dsh_apply多余键丢弃_只写sleep_hours(pd_env):
    srv, puts = H.dsh_stub()
    try:
        _setup_dsh(pd_env, srv.url)
        draft = pd_env["work"] / "d.yml"
        draft.write_text(yaml.safe_dump(
            {"sleep_hours": ["00:30-06:30"], "persona_summary": "多余"},
            allow_unicode=True), encoding="utf-8")
        extra = {"CONSOLE_DSH_ADAPTER_URL": srv.url, "DSH_ADAPTER_URL": srv.url,
                 "CONSOLE_ADAPTER_TOKEN_FILE": str(pd_env["home"] / ".config" / "claude-dsh-console" / "adapter.token")}
        r = _run(pd_env, [BOT, "--backend", "dsh", "--apply", str(draft)], extra)
    finally:
        srv.stop()
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert len(puts) == 1, puts
    body = puts[0]
    inner = body.get("values", body)
    assert inner == {"sleep_hours": ["00:30-06:30"]}, body


@pytest.mark.parametrize("mode,args_kind", [
    ("token_missing", "draft"),
    ("bot_missing", "draft"),
    ("life_missing", "draft"),
    ("apply_422", "apply"),
    ("refused", "draft"),
    ("5xx", "draft"),
    ("bad_shape", "draft"),
], ids=["没配令牌", "找不到bot", "没有生活设定文件", "apply回422", "连不上",
        "5xx", "回包形状不对"])
def test_B19_dsh错误契约(pd_env, mode, args_kind):
    srv = None
    try:
        if mode == "refused":
            base, with_token = H.dead_url(), True
        else:
            srv, _puts = H.dsh_stub(mode="ok" if mode == "token_missing" else mode)
            base, with_token = srv.url, mode != "token_missing"
        _setup_dsh(pd_env, base, with_token=with_token)
        extra = {"CONSOLE_DSH_ADAPTER_URL": base, "DSH_ADAPTER_URL": base}
        token_path = pd_env["home"] / ".config" / "claude-dsh-console" / "adapter.token"
        if with_token:
            extra["CONSOLE_ADAPTER_TOKEN_FILE"] = str(token_path)
        args = [BOT, "--backend", "dsh"]
        if args_kind == "apply":
            draft = pd_env["work"] / "d.yml"
            draft.write_text(yaml.safe_dump({"sleep_hours": ["00:30-06:30"]},
                                            allow_unicode=True), encoding="utf-8")
            args += ["--apply", str(draft)]
        r = _run(pd_env, args, extra)
        if srv is not None:
            hits = list(srv.hits)
    finally:
        if srv is not None:
            srv.stop()
    expect = {"token_missing": 2, "bot_missing": 2, "life_missing": 2,
              "apply_422": 4, "refused": 5, "5xx": 5, "bad_shape": 5}[mode]
    assert r.returncode == expect, (mode, r.returncode, r.stdout, r.stderr)
    if expect == 5:
        assert "读写 dsh 失败：" in r.stderr, r.stderr
    if mode == "token_missing":
        assert hits == [], "没配令牌不该发请求"
    H.no_sensitive_output(r)
