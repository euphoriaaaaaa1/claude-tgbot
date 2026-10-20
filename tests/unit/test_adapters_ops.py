# -*- coding: utf-8 -*-
"""T15：生命周期任务引擎（锁、记录、步骤、回滚、needs_attention、崩溃后恢复）。"""
import json
import os
import time

import pytest

from adapters import ops


def wait_op(op_id, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        op = ops.get_op(op_id)
        if op is not None and op["state"] in ("done", "failed", "needs_attention"):
            return op
        time.sleep(0.01)
    raise AssertionError("操作没有在 %.1f 秒内结束：%r" % (timeout, ops.get_op(op_id)))


@pytest.fixture(autouse=True)
def ops_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    saved = dict(ops._kinds)
    yield tmp_path
    ops._kinds.clear()
    ops._kinds.update(saved)


def test_op_id_shape_and_validation():
    op_id = ops.new_op_id("probe")
    assert ops.OP_ID_RE.fullmatch(op_id)
    assert op_id.split("-")[1] == "probe"
    with pytest.raises(ValueError):
        ops.new_op_id("X")
    with pytest.raises(ValueError):
        ops.acquire("bad-id")


def test_lock_is_exclusive_and_released():
    op_id = ops.new_op_id("probe")
    lock = ops.acquire(op_id, persons=("p",))
    assert lock.held
    with pytest.raises(ops.LifecycleBusy):
        ops.acquire(ops.new_op_id("probe"))
    lock.release()
    lock.release()  # 重复释放无害
    second = ops.acquire(ops.new_op_id("probe"))
    second.release()


def test_lock_file_contains_op_id():
    op_id = ops.new_op_id("probe")
    with ops.acquire(op_id):
        raw = open(ops.lock_path(), "rb").read()
        assert raw[0:1] == b"\0"
        assert raw[1:] == op_id.encode("ascii")


def test_submit_success_and_public_shape():
    events = []

    def one(task):
        events.append("one")
        return "第一步完成"

    ops.register("probe", (ops.Step("one", one),))
    lock = ops.acquire(ops.new_op_id("probe"), persons=("p",))
    op_id = ops.submit(lock, "probe", backend="claude", bot_id="p", person="p")
    op = wait_op(op_id)
    assert events == ["one"]
    assert op["state"] == "done"
    assert op["code"] is None
    assert op["needs_attention"] is False
    assert op["result"] is None
    assert op["steps"][0]["step"] == "one"
    assert op["steps"][0]["status"] == "done"
    assert op["message"] == ""
    # 不对外泄露内部 status / _internal
    assert "status" not in op and "_internal" not in op


def test_step_failure_rolls_back_and_reports_code():
    events = []

    def one(task):
        events.append("one")
        return "done"

    def one_undo(task):
        events.append("one undo")
        return "已撤回"

    def two(task):
        raise ops.StepFailed("exists", 409, "编号已被占用")

    ops.register("probe", (ops.Step("one", one, undo=one_undo), ops.Step("two", two)))
    lock = ops.acquire(ops.new_op_id("probe"))
    op_id = ops.submit(lock, "probe", backend="claude", bot_id="p")
    op = wait_op(op_id)
    assert op["state"] == "failed"
    assert op["code"] == "exists"
    assert op["message"] == "编号已被占用"
    assert events == ["one", "one undo"]
    rec = ops._load(op_id)
    assert rec["state"] == "rolled_back"
    steps = {s["step"]: s["status"] for s in op["steps"]}
    assert steps == {"one": "undone", "two": "failed"}


def test_rollback_failure_becomes_needs_attention():
    def one(task):
        return "done"

    def one_undo(task):
        raise RuntimeError("undo blew up")

    def two(task):
        raise ops.StepFailed("internal", 500, "boom")

    ops.register("probe", (ops.Step("one", one, undo=one_undo), ops.Step("two", two)))
    lock = ops.acquire(ops.new_op_id("probe"))
    op_id = ops.submit(lock, "probe", backend="claude")
    op = wait_op(op_id)
    assert op["state"] == "needs_attention"
    assert op["code"] == "needs_attention"
    assert op["needs_attention"] is True


def test_step_stuck_then_resolve_continues():
    def one(task):
        return "done"

    def two(task):
        raise ops.StepStuck("停在中间")

    def two_check(task):
        return True

    ops.register("probe", (ops.Step("one", one), ops.Step("two", two, check=two_check)))
    lock = ops.acquire(ops.new_op_id("probe"))
    op_id = ops.submit(lock, "probe", backend="claude")
    op = wait_op(op_id)
    assert op["state"] == "needs_attention"
    assert op["code"] == "needs_attention"
    ops.resolve(op_id)
    op = wait_op(op_id)
    assert op["state"] == "done"
    steps = {s["step"]: s["status"] for s in op["steps"]}
    assert steps == {"one": "done", "two": "done"}


def test_resolve_rejects_terminal_op():
    ops.register("probe", (ops.Step("one", lambda t: "done"),))
    lock = ops.acquire(ops.new_op_id("probe"))
    op_id = ops.submit(lock, "probe", backend="claude")
    wait_op(op_id)
    with pytest.raises(ops.NotResolvable) as exc:
        ops.resolve(op_id)
    assert exc.value.code == "not_resolvable"
    with pytest.raises(ops.OpNotFound):
        ops.resolve("20260101T000000Z-probe-deadbeef")


def test_recover_orphans_rolls_back_uncommitted():
    events = []

    def one(task):
        return "one"

    def one_undo(task):
        events.append("undo one")

    def two(task):
        return "two"

    def two_undo(task):
        events.append("undo two")

    ops.register("probe", (ops.Step("one", one, undo=one_undo), ops.Step("two", two, undo=two_undo)))
    op_id = ops.new_op_id("probe")
    rec = ops._new_record(op_id, "probe", "claude", "p", "p", None, None, ["one", "two"])
    rec["status"] = "running"
    ops._set_step(rec, "one", "done")
    ops._set_step(rec, "two", "running")
    ops._inner(rec)["doubt"] = "two"
    ops._write(rec)
    handled = ops.recover_orphans(wait=0.1, poll=0.01)
    assert handled == [op_id]
    assert events == ["undo two", "undo one"]
    rec = ops._load(op_id)
    assert rec["status"] == "failed"
    assert rec["state"] == "rolled_back"


def test_recover_orphans_finishes_committed_steps():
    def one(task):
        return "one"

    def two(task):
        return "two"

    ops.register("probe", (ops.Step("one", one, commit=True), ops.Step("two", two)))
    op_id = ops.new_op_id("probe")
    rec = ops._new_record(op_id, "probe", "claude", "p", "p", None, None, ["one", "two"])
    rec["status"] = "running"
    ops._set_step(rec, "one", "done")
    ops._set_step(rec, "two", "running")
    ops._inner(rec)["committed"] = True
    ops._inner(rec)["doubt"] = "two"
    ops._write(rec)
    ops.recover_orphans(wait=0.1, poll=0.01)
    assert ops.get_op(op_id)["state"] == "done"


def test_records_are_scrubbed(tmp_path):
    token = "123456789:" + "A" * 35

    def one(task):
        return "令牌是 " + token

    ops.register("probe", (ops.Step("one", one),))
    lock = ops.acquire(ops.new_op_id("probe"))
    op_id = ops.submit(lock, "probe", backend="claude")
    wait_op(op_id)
    raw = open(os.path.join(ops.ops_dir(), op_id + ".json"), encoding="utf-8").read()
    assert token not in raw
    assert "令牌已隐藏" in raw


def _acquire_free(kind="probe", timeout=3.0):
    """后台线程写完终态后可能还差一个 finally 才放锁；测试里等一小会。"""
    end = time.monotonic() + timeout
    while True:
        try:
            return ops.acquire(ops.new_op_id(kind))
        except ops.LifecycleBusy:
            if time.monotonic() >= end:
                raise
            time.sleep(0.01)


def test_list_ops_active_filter_and_limit():
    ops.register("probe", (ops.Step("one", lambda t: "done"),))
    ids = []
    for _ in range(3):
        lock = _acquire_free()
        ids.append(ops.submit(lock, "probe", backend="claude"))
    for op_id in ids:
        wait_op(op_id)
    assert len(ops.list_ops(limit=2)) == 2
    assert ops.list_ops(active=True) == []
    # 手工造一条未结束记录：active 列表里出现，state 映射为 running
    op_id = ops.new_op_id("probe")
    rec = ops._new_record(op_id, "probe", "claude", "p", "p", None, None, ["one"])
    rec["status"] = "running"
    ops._write(rec)
    active = ops.list_ops(active=True)
    assert [o["op_id"] for o in active] == [op_id]
    assert active[0]["state"] == "running"
    assert active[0]["needs_attention"] is False


def test_public_scrubs_error_message():
    rec = {"op_id": "20260101T000000Z-probe-deadbeef", "kind": "probe", "status": "failed",
           "error": {"code": "step_failed", "http_status": 500,
                     "message": "bad 123456789:" + "A" * 35},
           "steps": [], "result": None, "message": "bad 123456789:" + "A" * 35,
           "created_at": "x", "updated_at": "x"}
    op = ops.public(rec)
    assert "A" * 35 not in json.dumps(op, ensure_ascii=False)
    assert "令牌已隐藏" in json.dumps(op, ensure_ascii=False)


def test_get_op_rejects_bad_ids():
    assert ops.get_op("../../etc/passwd") is None
    assert ops.get_op("20260101T000000Z-probe-deadbeef") is None
