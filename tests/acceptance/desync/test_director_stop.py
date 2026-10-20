"""需求⑤ + 缺陷③b（公开 director.py / bots_registry.py）：
停止 bot 的入口硬闸（inject / _ensure_worker_alive / _prewarm_all_workers）、
inject payload 的 human_ts、disabled_ids_safe 的 fail-open。
"""
import datetime
import json
import os
import re
import stat

from _helpers import NOW, human, write_cfg

HIST = [human(NOW - 600)]
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.000Z$")


def _inbox(iso, bot):
    p = os.path.join(iso["ch"], bot, "inbox")
    return sorted(os.listdir(p)) if os.path.isdir(p) else []


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ---------- 停止硬闸 ----------
def test_inject_已停止bot_不写inbox_返回空串_打stopped_skip(director, iso, capsys):
    write_cfg(iso["cfg"], "bot2", enabled=False)
    assert director.inject("bot2", "-100", HIST) == ""
    assert _inbox(iso, "bot2") == []
    assert "[director] stopped_skip where=inject bot=bot2" in capsys.readouterr().out


def test_inject_未停止bot_照常写inbox_返回路径(director, iso):
    write_cfg(iso["cfg"], "bot2", enabled=True)
    r = director.inject("bot2", "-100", HIST)
    assert r and os.path.isfile(r) and os.path.basename(r).startswith("director-")
    assert len(_inbox(iso, "bot2")) == 1


def test_inject_停止bot_stopped_skip_300秒内只打一次(director, iso, capsys):
    write_cfg(iso["cfg"], "bot1", enabled=False)
    director.inject("bot1", "-100", HIST)
    director.inject("bot1", "-100", HIST)
    assert capsys.readouterr().out.count("stopped_skip where=inject bot=bot1") == 1


def test_ensure_worker_alive_已停止bot_打stopped_skip(director, iso, capsys):
    write_cfg(iso["cfg"], "bot3", enabled=False)
    assert director._ensure_worker_alive("bot3", "-100") is None
    out = capsys.readouterr().out
    assert re.search(r"\[director\] stopped_skip where=\S+ bot=bot3", out), out


def test_prewarm_只跳过停止的bot(director, iso, capsys):
    write_cfg(iso["cfg"], "bot2", enabled=False)
    director._prewarm_all_workers("-100")
    out = capsys.readouterr().out
    assert "stopped_skip where=prewarm bot=bot2" in out
    assert "bot=bot1" not in out and "bot=bot3" not in out


def test_inject_停止bot_不调tmux_不拉起(director, iso):
    write_cfg(iso["cfg"], "bot2", enabled=False)
    director.inject("bot2", "-100", HIST)
    assert not os.path.exists(iso["calls"])


# ---------- inject payload human_ts（③b）----------
def _payload(director, iso, **kw):
    p = director.inject("bot1", "-100", HIST, **kw)
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _ub(ts, text="嗯"):
    return {"ts": ts, "speaker": "主人", "text": text, "is_bot": False}


def test_inject_user_batch非空_human_ts为批内最大ts的UTC(director, iso):
    pl = _payload(director, iso, user_batch=[_ub(NOW - 50), _ub(NOW - 10)])
    assert pl["human_ts"] == _iso(NOW - 10)
    assert ISO_RE.match(pl["human_ts"])


def test_inject_user_batch为None_无human_ts键(director, iso):
    assert "human_ts" not in _payload(director, iso, user_batch=None)


def test_inject_user_batch为空列表_无human_ts键(director, iso):
    assert "human_ts" not in _payload(director, iso, user_batch=[])


def test_inject_user_batch全部坏ts_无human_ts键_不抛(director, iso):
    bad = [_ub("abc"), _ub(0), _ub(-5)]
    assert "human_ts" not in _payload(director, iso, user_batch=bad)


def test_inject_user_batch混有坏ts_只丢坏条(director, iso):
    pl = _payload(director, iso, user_batch=[_ub("x"), _ub(str(int(NOW - 20))), _ub(NOW - 40)])
    assert pl["human_ts"] == _iso(int(NOW - 20))


def test_inject_其余payload键不变(director, iso):
    pl = _payload(director, iso, user_batch=[_ub(NOW - 1)])
    for k in ("text", "chat_id", "from_username", "sender_username", "chat_type", "scene", "is_bot_sender", "ts", "message_id"):
        assert k in pl, k
    assert pl["from_username"] == "director"


# ---------- disabled_ids_safe ----------
def test_disabled_ids_safe_configs目录缺_返回空集(registry, iso):
    os.rmdir(iso["cfg"])
    assert registry.disabled_ids_safe() == set()


def test_disabled_ids_safe_坏yml跳过_其它照常(registry, iso):
    write_cfg(iso["cfg"], "bot2", enabled=False)
    with open(os.path.join(iso["cfg"], "bad.yml"), "w") as f:
        f.write("not: [valid")
    assert registry.disabled_ids_safe() == {"bot2"}


def test_disabled_ids_safe_中文bot名_可判停止(registry, iso):
    write_cfg(iso["cfg"], "陈璐璐", enabled=False)
    assert registry.disabled_ids_safe() == {"陈璐璐"}


def test_disabled_ids_safe_目录不可读_空集且stderr一次(registry, iso, capsys):
    os.chmod(iso["cfg"], 0)
    try:
        assert registry.disabled_ids_safe() == set()
        assert registry.disabled_ids_safe() == set()
    finally:
        os.chmod(iso["cfg"], stat.S_IRWXU)
    err = capsys.readouterr().err
    assert err.count("disabled_ids failed:") == 1, err


def test_RESUME_GRACE_MIN常量为30(director):
    assert director.RESUME_GRACE_MIN == 30
