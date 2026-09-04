# -*- coding: utf-8 -*-
"""§2.4 备份轮转 + §3.4 列表 + §3.5 回滚（BRIEF S4）。"""
import datetime as dt

import hub2util as u
import spec

CK = "jiwen.rates.connection_per_min"


def _save(api, global_yml, value):
    return api.post("/hub/api/params",
                    {"version": u.version_of(global_yml), "values": {CK: value}})


def _list(api):
    code, body = api.get("/hub/api/params/backups")
    assert code == 200, body
    return body


def test_备份列表_一次保存后出现一条save类记录且形状完整(api, global_yml):
    code, body = _save(api, global_yml, 0.0012)
    assert code == 200, body
    items = _list(api)["backups"]
    assert len(items) == 1
    it = items[0]
    assert it["id"] == body["backup"]
    assert it["kind"] == "save"
    assert isinstance(it["size"], int) and it["size"] > 0
    assert isinstance(it["ts"], int)


def test_备份列表_按新到旧排序(api, global_yml):
    for i, v in enumerate([0.0011, 0.0012, 0.0013]):
        assert _save(api, global_yml, v)[0] == 200
    ids = u.backup_ids(_list(api))
    assert ids == sorted(ids, reverse=True), f"没按新到旧排：{ids}"


def test_备份列表_目录不存在时200空列表不是错误(api, configs_dir, make_client):
    c, _ = make_client(HUB_CONFIGS_DIR=str(configs_dir / "nope"))
    r = c.get("/hub/api/params/backups")
    assert r.status_code == 200
    assert r.get_json() == {"backups": []}


def test_备份列表_不回显备份内容(api, global_yml):
    _save(api, global_yml, 0.0012)
    blob = u.flat_text(_list(api))
    assert spec.SECRET_KEY not in blob
    assert "user_display_name" not in blob, "§3.4：响应不含备份内容"


def test_连续保存6次_save类恰好留5份且最老那份已从磁盘删除(api, global_yml, configs_dir):
    """BRIEF S4 / §3.5 的 S4 断言。"""
    made = []
    for i in range(6):
        code, body = _save(api, global_yml, 0.001 + i * 0.0001)
        assert code == 200, body
        made.append(body["backup"])
    ids = u.backup_ids(_list(api), kind="save")
    assert len(ids) == 5, f"轮转没生效：{ids}"
    assert made[0] not in ids
    assert not (configs_dir / f"_global.yml.bak.{made[0]}").exists()
    for kept in made[1:]:
        assert (configs_dir / f"_global.yml.bak.{kept}").exists()


def test_备份文件权限600(api, global_yml, configs_dir):
    _, body = _save(api, global_yml, 0.0012)
    assert u.mode_of(configs_dir / f"_global.yml.bak.{body['backup']}") == 0o600


def test_同秒时间戳冲突_追加两位序号且不覆盖既有备份(api, global_yml, configs_dir):
    """§2.4：时间戳同秒冲突 → 追加 `-<2位序号>`，不覆盖既有备份。"""
    import re
    now = dt.datetime.now()
    planted = {}
    for delta in (0, 1):
        ts = (now + dt.timedelta(seconds=delta)).strftime("%Y%m%d-%H%M%S")
        p = configs_dir / f"_global.yml.bak.{ts}"
        p.write_text("planted: 1\n", encoding="utf-8")
        planted[ts] = p.read_bytes()
    code, body = _save(api, global_yml, 0.0012)
    assert code == 200, body
    assert body["backup"] not in planted, "覆盖了同名备份"
    assert re.fullmatch(r"\d{8}-\d{6}-\d{2}", body["backup"]), body["backup"]
    for ts, content in planted.items():
        assert (configs_dir / f"_global.yml.bak.{ts}").read_bytes() == content


def test_写盘失败_500且最老的备份没被提前删掉(api, global_yml, configs_dir):
    """R3-1：轮转必须排在写盘成功之后，否则用户白丢一份历史。"""
    for i in range(6):
        assert _save(api, global_yml, 0.001 + i * 0.0001)[0] == 200
    before = sorted(p.name for p in configs_dir.iterdir())
    version = u.version_of(global_yml)
    configs_dir.chmod(0o500)
    try:
        code, body = api.post("/hub/api/params",
                              {"version": version, "values": {CK: 0.0099}})
    finally:
        configs_dir.chmod(0o700)
    assert code == 500, body
    assert body["error"] == "config_write_failed"
    assert sorted(p.name for p in configs_dir.iterdir()) == before, "写盘失败却动了备份集合"


def test_回滚_文件恢复成备份内容且生成pre_restore类备份(api, global_yml, configs_dir):
    original = global_yml.read_bytes()
    _, saved = _save(api, global_yml, 0.0012)
    bid = saved["backup"]
    changed = global_yml.read_bytes()
    code, body = api.post(f"/hub/api/params/backups/{bid}/restore",
                          {"version": u.version_of(global_yml)})
    assert code == 200, body
    assert body["ok"] is True
    assert global_yml.read_bytes() == original, "回滚没把文件还原"
    assert "restart_hint" in body
    pre = configs_dir / f"_global.yml.pre-restore.{body['backup']}"
    assert pre.exists(), "没给当前文件留 pre-restore 备份"
    assert pre.read_bytes() == changed
    assert u.mode_of(pre) == 0o600


def test_回滚_pre_restore不挤占save的5份配额(api, global_yml):
    """R3-3：连续 restore 5 次后，最初那份 save 备份仍在。"""
    _, first = _save(api, global_yml, 0.0012)
    oldest = first["backup"]
    for _ in range(5):
        code, body = api.post(f"/hub/api/params/backups/{oldest}/restore",
                              {"version": u.version_of(global_yml)})
        assert code == 200, body
    listing = _list(api)
    assert oldest in u.backup_ids(listing, kind="save"), "最初想回到的那份被挤掉了"
    assert len(u.backup_ids(listing, kind="restore")) == 5


def test_回滚_备份id不存在_404_backup_not_found(api, global_yml):
    code, body = api.post("/hub/api/params/backups/20200101-000000/restore",
                          {"version": u.version_of(global_yml)})
    assert code == 404, body
    assert body["error"] == "backup_not_found"


def test_回滚_id带路径穿越_404_backup_not_found(api, global_yml, tmp_path):
    """反向用例：`id` 会拼进文件名，穿越串必须被当成"没这个备份"。
    先做一次真回滚证明端点活着，否则整条断言会在未实现时空过。"""
    outside = tmp_path / "outside.yml"
    outside.write_text("user_display_name: x\nmax_calls_per_5h: 1\nmoments: {}\njiwen: {}\n",
                       encoding="utf-8")
    _, saved = _save(api, global_yml, 0.0012)
    ok = api.post(f"/hub/api/params/backups/{saved['backup']}/restore",
                  {"version": u.version_of(global_yml)})
    assert ok[0] == 200, f"端点没活着，穿越断言会空过：{ok}"
    before = global_yml.read_bytes()
    for bad_id in ("..", "_global.yml", "20200101-000000%00", "%2e%2e"):
        code, body = api.post(f"/hub/api/params/backups/{bad_id}/restore",
                              {"version": u.version_of(global_yml)})
        assert code == 404, (bad_id, code, body)
        assert body.get("error") == "backup_not_found", (bad_id, body)
    assert global_yml.read_bytes() == before
    assert outside.read_text(encoding="utf-8").startswith("user_display_name: x")


def test_回滚_id跨路径分段_404(api, global_yml):
    """多段路径会先撞路由，框架 404 也算挡住了（码可能是 not_found）。"""
    code, body = api.post("/hub/api/params/backups/../../outside.yml/restore",
                          {"version": u.version_of(global_yml)})
    assert code == 404, body
    assert body.get("error") in ("backup_not_found", "not_found")


def test_回滚_备份内容不是合法yaml_400_bad_yaml且不写盘(api, global_yml, configs_dir):
    bad = configs_dir / "_global.yml.bak.20200101-000000"
    bad.write_text("jiwen: [oops\n  x: : :\n", encoding="utf-8")
    before = global_yml.read_bytes()
    code, body = api.post("/hub/api/params/backups/20200101-000000/restore",
                          {"version": u.version_of(global_yml)})
    assert code == 400, body
    assert body["error"] == "bad_yaml"
    assert global_yml.read_bytes() == before


def test_回滚_缺version_400_bad_body(api, global_yml):
    _, saved = _save(api, global_yml, 0.0012)
    code, body = api.post(f"/hub/api/params/backups/{saved['backup']}/restore", {})
    assert code == 400, body
    assert body["error"] == "bad_body"


def test_回滚_version过期_409_config_stale且不写盘(api, global_yml):
    _, saved = _save(api, global_yml, 0.0012)
    before = global_yml.read_bytes()
    code, body = api.post(f"/hub/api/params/backups/{saved['backup']}/restore",
                          {"version": "1-1"})
    assert code == 409, body
    assert body["error"] == "config_stale"
    assert global_yml.read_bytes() == before
