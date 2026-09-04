# -*- coding: utf-8 -*-
"""§2.3 R10「字节级 diff」+ §8「注释/锚点/键序/数值格式全保留」+ §5 生效提示 + 并发。

这些是二期最硬的反向承诺：**没被提交的键必须逐字节原样**。
"""
import threading

import pytest
import yaml

import hub2util as u
import spec

CK = "jiwen.rates.connection_per_min"


def _save(api, global_yml, values):
    return api.post("/hub/api/params",
                    {"version": u.version_of(global_yml), "values": values})


def test_保存_逐行diff只出现被改的那一行(api, global_yml):
    before = global_yml.read_text(encoding="utf-8")
    code, _ = _save(api, global_yml, {CK: 0.0012})
    assert code == 200
    diff = u.changed_lines(before, global_yml.read_text(encoding="utf-8"))
    assert len(diff) == 1, f"除了目标行还动了别的：{diff}"
    assert "connection_per_min" in diff[0][1]


def test_保存_注释一字不变(api, global_yml):
    assert _save(api, global_yml, {CK: 0.0012})[0] == 200, "保存没成功，保真断言会空过"
    txt = global_yml.read_text(encoding="utf-8")
    for comment in ("# ===== claudebotlife 全局配置 =====",
                    "# 这一行注释是 F2 字节保真断言的锚：任何一次保存后它必须一字不动",
                    "# 静默多久算冷场", "# §2.2：不进表单"):
        assert comment in txt, f"注释被吃掉了：{comment}"


def test_保存_锚点别名mergekey与块标量原样保留(api, global_yml):
    assert _save(api, global_yml, {CK: 0.0012})[0] == 200, "保存没成功，保真断言会空过"
    txt = global_yml.read_text(encoding="utf-8")
    assert "&base" in txt, "锚点被展开了"
    assert "<<: *base" in txt, "merge key 被展开了"
    assert "prompt: |" in txt, "块标量被改写了"
    assert "    第二行照旧。" in txt


def test_保存_数值书写格式不被规整(api, global_yml):
    """`0.20` 不许变成 `0.2`（§2.3）。"""
    assert _save(api, global_yml, {CK: 0.0012})[0] == 200, "保存没成功，保真断言会空过"
    assert "notice: 0.20" in global_yml.read_text(encoding="utf-8")


def test_保存_改阈值时只有那一行变且新值正确(api, global_yml):
    before = global_yml.read_text(encoding="utf-8")
    code, body = _save(api, global_yml, {"jiwen.thresholds.notice": 0.25})
    assert code == 200, body
    after = global_yml.read_text(encoding="utf-8")
    diff = u.changed_lines(before, after)
    assert len(diff) == 1
    assert "notice" in diff[0][1]
    assert u.dig(u.load(global_yml), "jiwen.thresholds.notice") == 0.25
    assert "consider: 0.35" in after and "forced: 0.50" in after


def test_保存_键顺序不变(api, global_yml):
    before = list(yaml.safe_load(global_yml.read_text(encoding="utf-8")).keys())
    assert _save(api, global_yml, {CK: 0.0012})[0] == 200, "保存没成功，保真断言会空过"
    after = list(yaml.safe_load(global_yml.read_text(encoding="utf-8")).keys())
    assert before == after


@pytest.mark.parametrize("values,bot,web", [
    pytest.param({CK: 0.0012}, True, False, id="jiwen类"),
    pytest.param({"max_calls_per_5h": 99}, True, False, id="顶层配额"),
    pytest.param({"moments.daily_post_limit_per_bot": 6}, False, True, id="moments类"),
    pytest.param({CK: 0.0013, "moments.enabled": False}, True, True, id="两类混合"),
])
def test_保存_生效提示按改动的键派生(api, global_yml, values, bot, web):
    """§5 的表 + §13 A8 裁决：逐键求值后**按位或**取并集，不是"按第一个命中的类别定档"。"""
    code, body = _save(api, global_yml, values)
    assert code == 200, body
    assert body["restart_hint"] == {"bot": bot, "moments_web": web}


def test_保存_响应不提供momentsweb的重启端点(api, global_yml):
    """§5：moments_web 只给文案不给按钮，测试断言响应里没有这个端点。"""
    code, body = _save(api, global_yml, {"moments.enabled": False})
    assert code == 200, body
    blob = u.flat_text(body)
    assert "/hub/api/moments" not in blob
    assert "moments-web/restart" not in blob


def test_保存_备份内容等于保存前的原文且权限600(api, global_yml, configs_dir):
    before = global_yml.read_bytes()
    code, body = _save(api, global_yml, {CK: 0.0012})
    assert code == 200, body
    bak = configs_dir / f"_global.yml.bak.{body['backup']}"
    assert bak.exists(), f"备份文件不在：{sorted(p.name for p in configs_dir.iterdir())}"
    assert bak.read_bytes() == before
    assert u.mode_of(bak) == 0o600


def test_保存_备份文件名不含任何密钥片段(api, global_yml, configs_dir):
    _, body = _save(api, global_yml, {CK: 0.0012})
    names = " ".join(p.name for p in configs_dir.iterdir())
    assert spec.SECRET_KEY not in names
    assert "s3cr3t" not in names
    assert body["backup"] and spec.SECRET_KEY not in body["backup"]


@pytest.mark.concurrency
def test_并发两次保存_只有一个成功另一个409(make_client, global_yml):
    """F1：同一个 app 实例下双写，赢家 200，输家必须是 config_stale 或 config_busy，
    绝不许两个都 200（那意味着有一次改动被静默吞掉）。"""
    c1, app = make_client()
    c2 = app.test_client()
    version = u.version_of(global_yml)
    results = []
    gate = threading.Barrier(2)

    def go(client, value):
        gate.wait()
        r = client.post("/hub/api/params",
                        json={"version": version, "values": {CK: value}})
        results.append((r.status_code, r.get_json()))

    ts = [threading.Thread(target=go, args=(c1, 0.0011)),
          threading.Thread(target=go, args=(c2, 0.0013))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)

    codes = sorted(c for c, _ in results)
    assert len(results) == 2, f"有请求没回来：{results}"
    assert codes == [200, 409], f"并发结果不对：{results}"
    loser = [b for c, b in results if c == 409][0]
    assert loser.get("error") in ("config_stale", "config_busy"), loser
    on_disk = u.dig(u.load(global_yml), "jiwen.rates.connection_per_min")
    assert on_disk in (0.0011, 0.0013)
