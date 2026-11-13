# -*- coding: utf-8 -*-
"""通知（接口文件 1.4、1.5、§3；行为清单第 35–38 条）。通知出口一律是底层适配器替身的 notify，只记参数。

场景（_scene）：claude 侧 anna（在跑，生活设定里称呼用户"主人"）、bella（停用）、coco（idle，算在跑，没填称呼）；
dsh 侧 dora（在跑）、ella（broken）、fay（dispatcher_down，算在跑），dsh 侧没报生活设定能力。
没填称呼时用中性的"用户"并换句（D5，用户 11-13 改），绝不出现"哥哥"。"""
import re
import time

import pytest

import pmom_helpers as H

RUNNING = [("claude", "anna"), ("claude", "coco"), ("dsh", "dora"), ("dsh", "fay")]


def _scene(backends, dsh_caps=None):
    cl = backends.add("claude", bots=[H.bot("anna", display_name="安娜"), H.bot("bella", state="stopped"),
                                      H.bot("coco", state="idle", display_name="可可")],
                      caps={"bot.life_settings": True}, life={"anna": {"user_address": "主人"}, "coco": {}})
    ds = backends.add("dsh", bots=[H.bot("dora", display_name="朵拉"), H.bot("ella", state="broken"),
                                   H.bot("fay", state="dispatcher_down", display_name="菲")],
                      caps=dsh_caps or {})
    return cl, ds


def _post(c, backends, text="今天去海边了", visibility="public", **extra):
    r = c.post("/api/moment", json=dict({"text": text, "visibility": visibility}, **extra))
    assert r.status_code == 200, (r.status_code, r.get_data(as_text=True)[:300])
    sent = list(backends.calls)
    backends.clear()
    return r.get_json()["id"], sent


def _comment(c, backends, mid, text, parent_id=None, **extra):
    backends.clear()
    body = dict({"moment_id": mid, "text": text}, **extra)
    if parent_id is not None:
        body["parent_id"] = parent_id
    r = c.post("/api/comment", json=body)
    assert r.status_code == 200, (r.status_code, r.get_data(as_text=True)[:300])
    calls = list(backends.calls)
    backends.clear()
    return r.get_json(), calls


def _who(calls):
    return sorted((c["backend"], c["bot_id"]) for c in calls)


def _text_for(calls, bot_id):
    (t,) = [c["text"] for c in calls if c["bot_id"] == bot_id]
    return t


def _line_with(text, marker):
    lines = [ln for ln in text.split("\n") if marker in ln]
    assert len(lines) == 1, (marker, lines)
    return lines[0]


# ── 发动态（35）─────────────────────────────────────────────────────────────

def test_发动态_每个在跑的人各收一条_source与key对(c, backends):
    _scene(backends)
    mid, sent = _post(c, backends)
    assert _who(sent) == RUNNING
    assert {(s["source"], s["key"]) for s in sent} == {("user_moment", "user-moment:%d" % mid)}


def test_发动态_通知里有动态原文(c, backends):
    _scene(backends)
    _, sent = _post(c, backends, text="海风很大-pmom")
    assert all("海风很大-pmom" in s["text"] for s in sent)


def test_发动态_底层没报notify能力_那一侧都不调(c, backends):
    _scene(backends, dsh_caps={"bot.notify": False})
    _, sent = _post(c, backends)
    assert _who(sent) == [("claude", "anna"), ("claude", "coco")]


def test_发动态_两边都在跑的人只投dsh那份(c, backends):
    backends.add("claude", bots=[H.bot("anna", state="idle")])
    backends.add("dsh", bots=[H.bot("dsh-5", person="anna")])
    _, sent = _post(c, backends)
    assert _who(sent) == [("dsh", "dsh-5")]


def test_发动态_同一人一侧停用一侧在跑_投在跑那份(c, backends):
    backends.add("claude", bots=[H.bot("anna", state="idle")])
    backends.add("dsh", bots=[H.bot("dsh-5", person="anna", state="stopped")])
    _, sent = _post(c, backends)
    assert _who(sent) == [("claude", "anna")]


def test_发动态_同一侧同一人两份都在跑_只投先出现的(c, backends):
    backends.add("dsh", bots=[H.bot("dsh-5", person="anna"), H.bot("dsh-9", person="anna")])
    _, sent = _post(c, backends)
    assert _who(sent) == [("dsh", "dsh-5")]


def test_发动态_某人投递抛错_别人照投_返回200_动态已存(c, paths, backends):
    from adapters import AdapterError, AdapterUnavailable
    cl, ds = _scene(backends)
    cl.notify_plan["anna"] = AdapterError("stopped", 409, "停用")
    ds.notify_plan["dora"] = AdapterUnavailable("down")
    cl.notify_plan["coco"] = RuntimeError("模拟投递抛错")
    mid, sent = _post(c, backends)
    assert _who(sent) == RUNNING
    assert H.q(paths.db_path, "SELECT id FROM moments") == [{"id": mid}]


def test_发动态_没有人在跑_照常200_谁都不调(c, backends):
    backends.add("claude", bots=[H.bot("bella", state="stopped")])
    _, sent = _post(c, backends)
    assert sent == []


# ── 在 bot 的动态下评论（36）────────────────────────────────────────────────

def test_bot动态下评论_只通知那个人_moment_reply_key带编号_pending为真(c, paths, backends):
    _scene(backends)
    mid = H.bot_moment(paths.db, "dora", "画了一张画")
    body, calls = _comment(c, backends, mid, "好看")
    assert [(x["backend"], x["bot_id"], x["source"], x["key"]) for x in calls] == [
        ("dsh", "dora", "moment_reply", "moment-reply:%d:%d" % (mid, body["id"]))]
    assert body["pending"] is True and H.pending_of(paths.db_path, body["id"]) is True


def test_bot动态下评论_回duplicate当投成_pending仍为真(c, paths, backends):
    _, ds = _scene(backends)
    ds.notify_plan["dora"] = {"status": "duplicate"}
    body, _ = _comment(c, backends, H.bot_moment(paths.db, "dora"), "x")
    assert H.pending_of(paths.db_path, body["id"]) is True


def _fail_cases():
    from adapters import AdapterError, AdapterUnavailable
    return [AdapterError("stopped", 409, "停用"), AdapterUnavailable("down"), {"status": "queued"},
            RuntimeError("模拟")]


@pytest.mark.parametrize("idx", range(4), ids=["AdapterError", "AdapterUnavailable", "status非投成", "其他异常"])
def test_bot动态下评论_没投成_接口仍200且返回pending真_库里pending改回假(c, paths, backends, idx):
    _, ds = _scene(backends)
    ds.notify_plan["dora"] = _fail_cases()[idx]
    body, calls = _comment(c, backends, H.bot_moment(paths.db, "dora"), "x")
    assert len(calls) == 1 and body["pending"] is True
    assert H.pending_of(paths.db_path, body["id"]) is False


def test_bot动态下评论_那人停用_不调notify_库里pending为假(c, paths, backends):
    _scene(backends)
    body, calls = _comment(c, backends, H.bot_moment(paths.db, "bella"), "x")
    assert calls == [] and H.pending_of(paths.db_path, body["id"]) is False


def test_bot动态下评论_底层没报notify能力_不调_库里pending为假(c, paths, backends):
    _scene(backends, dsh_caps={"bot.notify": False})
    body, calls = _comment(c, backends, H.bot_moment(paths.db, "dora"), "x")
    assert calls == [] and H.pending_of(paths.db_path, body["id"]) is False


def test_bot动态下评论_那人不在名单里_不调_库里pending为假(c, paths, backends):
    _scene(backends)
    body, calls = _comment(c, backends, H.bot_moment(paths.db, "nobody"), "x")
    assert calls == [] and H.pending_of(paths.db_path, body["id"]) is False


def test_bot动态下评论_那人两边都在跑_只投dsh那份(c, paths, backends):
    backends.add("claude", bots=[H.bot("anna", state="idle")])
    backends.add("dsh", bots=[H.bot("dsh-5", person="anna")])
    mid = H.bot_moment(paths.db, "anna")
    body, calls = _comment(c, backends, mid, "嗯")
    assert [(x["backend"], x["bot_id"], x["key"]) for x in calls] == [
        ("dsh", "dsh-5", "moment-reply:%d:%d" % (mid, body["id"]))]
    assert H.pending_of(paths.db_path, body["id"]) is True


def test_自己动态下回复bot的评论_只通知那只bot_pending为真(c, paths, backends):
    _scene(backends)
    mid, _ = _post(c, backends)
    bc = H.add_comment(paths.db_path, mid, "coco", "我也想去")
    body, calls = _comment(c, backends, mid, "带你去", parent_id=bc)
    assert [(x["bot_id"], x["source"], x["key"]) for x in calls] == [
        ("coco", "moment_reply", "moment-reply:%d:%d" % (mid, body["id"]))]
    assert body["pending"] is True and H.pending_of(paths.db_path, body["id"]) is True


# ── 在自己动态下普通评论（37）───────────────────────────────────────────────

def test_自己动态下普通评论_每个在跑的人各收一条_pending为假(c, paths, backends):
    _scene(backends)
    mid, _ = _post(c, backends)
    body, calls = _comment(c, backends, mid, "风好大")
    assert _who(calls) == RUNNING
    assert {(x["source"], x["key"]) for x in calls} == {("moment_reply", "moment-reply:%d:%d" % (mid, body["id"]))}
    assert body["pending"] is False and H.pending_of(paths.db_path, body["id"]) is False


def test_回复自己的评论_每个在跑的人各收一条_key是新评论(c, paths, backends):
    _scene(backends)
    mid, _ = _post(c, backends)
    first, _ = _comment(c, backends, mid, "风好大")
    body, calls = _comment(c, backends, mid, "补一句", parent_id=first["id"])
    assert _who(calls) == RUNNING and body["pending"] is False
    assert {x["key"] for x in calls} == {"moment-reply:%d:%d" % (mid, body["id"])}


def test_回复署名为用户主页键的评论_也算回复自己_群投(c, paths, backends):
    _scene(backends)
    mid, _ = _post(c, backends)
    uc = H.add_comment(paths.db_path, mid, "__user__", "主页键署名的评论")
    body, calls = _comment(c, backends, mid, "补一句", parent_id=uc)
    assert _who(calls) == RUNNING and body["pending"] is False


def test_自己动态下评论_某人抛错_别人照投_评论照存_200(c, paths, backends):
    from adapters import AdapterError
    cl, ds = _scene(backends)
    cl.notify_plan["anna"] = RuntimeError("模拟")
    ds.notify_plan["dora"] = AdapterError("stopped", 409, "停用")
    mid, _ = _post(c, backends)
    body, calls = _comment(c, backends, mid, "风好大")
    assert _who(calls) == RUNNING
    assert [r["text"] for r in H.comment_rows(paths.db_path, mid)] == ["风好大"]


def test_父评论不在这条动态下_谁都不通知_pending为假(c, paths, backends):
    _scene(backends)
    mid, _ = _post(c, backends)
    other = H.bot_moment(paths.db, "coco", "别的")
    stray = H.add_comment(paths.db_path, other, "coco", "别处的评论")
    body, calls = _comment(c, backends, mid, "串门", parent_id=stray)
    assert calls == [] and body["pending"] is False and H.pending_of(paths.db_path, body["id"]) is False


# ── 通知文字：发动态（38）───────────────────────────────────────────────────

def test_发动态通知_开头_填了称呼的用称呼_没填的用用户(c, backends):
    _scene(backends)
    _, sent = _post(c, backends)
    assert _text_for(sent, "anna").startswith("[user-moment] 主人 刚发了一条朋友圈（公开）：")
    for b in ("coco", "dora", "fay"):
        assert _text_for(sent, b).startswith("[user-moment] 用户 刚发了一条朋友圈（公开）："), b


def test_发动态通知_私密动态开头标私密仅你可见(c, backends):
    _scene(backends)
    _, sent = _post(c, backends, visibility="private")
    assert _text_for(sent, "coco").startswith("[user-moment] 用户 刚发了一条朋友圈（私密（仅你可见））：")


def test_发动态通知_含点赞与评论两条命令(c, backends):
    _scene(backends)
    mid, sent = _post(c, backends)
    for s in sent:
        assert "scripts/moment_like.py %d" % mid in s["text"]
        assert 'scripts/moment_reply.py %d 0 "<你的评论>"' % mid in s["text"]


def test_发动态通知_恰好一行私聊规则_不含禁止私聊(c, backends):
    _scene(backends)
    _, sent = _post(c, backends)
    for s in sent:
        addr = "主人" if s["bot_id"] == "anna" else H.ADDR
        assert H.dm_lines(s["text"]) == [H.DM_RULE % addr], s["bot_id"]
        assert H.OLD_DM_BAN not in s["text"]


def test_发动态通知_没填称呼_不出现哥哥_回复要求换成中性句(c, backends):
    _scene(backends)
    _, sent = _post(c, backends)
    for b in ("coco", "dora"):
        t = _text_for(sent, b)
        assert H.OLD_FALLBACK not in t and H.REPLY_RULE_NEUTRAL in t, b


def test_发动态通知_底层没报生活设定能力_不调get_life_用用户(c, backends):
    backends.add("dsh", bots=[H.bot("dora")], caps={"bot.life_settings": False},
                 life={"dora": {"user_address": "不该用的称呼"}})
    _, sent = _post(c, backends)
    t = _text_for(sent, "dora")
    assert backends.fakes["dsh"].life_calls == []
    assert t.startswith("[user-moment] 用户 刚发了一条朋友圈") and "不该用的称呼" not in t


def test_发动态通知_最近7天的其他用户动态最多8条_更早的不列(c, paths, backends):
    _scene(backends)
    now = int(time.time())
    for i in range(10):
        H.user_moment(paths.db, "近期动态%02d号" % i, ts=now - (i + 1) * 3600)
    H.user_moment(paths.db, "很久以前的动态", ts=now - 8 * 86400)
    _, sent = _post(c, backends, text="新的一条")
    t = _text_for(sent, "coco")
    listed = [i for i in range(10) if "近期动态%02d号" % i in t]
    assert 1 <= len(listed) <= 8, listed
    assert "很久以前的动态" not in t


# ── 通知文字：评论回复（单人，38）───────────────────────────────────────────

def test_评论回复通知_开头_含评论原文与动态前200字(c, paths, backends):
    _scene(backends)
    moment = "头" + "长" * 249
    mid = H.bot_moment(paths.db, "coco", moment)
    _, calls = _comment(c, backends, mid, "好看-pmom")
    t = _text_for(calls, "coco")
    assert t.startswith("[moment-interaction] 用户在你的朋友圈下评论了：")
    assert "好看-pmom" in t
    assert '原朋友圈（公开）："%s"' % moment[:200] in t and moment[:201] not in t


def test_评论回复通知_填了称呼的开头用称呼(c, paths, backends):
    _scene(backends)
    _, calls = _comment(c, backends, H.bot_moment(paths.db, "anna", "画"), "好看")
    assert _text_for(calls, "anna").startswith("[moment-interaction] 主人在你的朋友圈下评论了：")


def test_评论回复通知_含回写命令与禁止私聊句(c, paths, backends):
    _scene(backends)
    mid = H.bot_moment(paths.db, "coco", "画")
    body, calls = _comment(c, backends, mid, "好看")
    t = _text_for(calls, "coco")
    assert 'scripts/moment_reply.py %d %d "<你的回复内容>"' % (mid, body["id"]) in t
    assert "其他情况下不要在 telegram 私聊里说话" in t


def test_评论回复通知_公开动态_含删评论命令与中性提醒_不出现哥哥(c, paths, backends):
    _scene(backends)
    _, calls = _comment(c, backends, H.bot_moment(paths.db, "coco", "画"), "好看")
    t = _text_for(calls, "coco")
    assert "scripts/moment_delete_comment.py <comment_id>" in t
    assert H.PUBLIC_HINT_NEUTRAL in t
    assert H.OLD_FALLBACK not in t and "「用户」" not in t


def test_评论回复通知_私密动态_没填称呼用中性句(c, paths, backends):
    _scene(backends)
    _, calls = _comment(c, backends, H.bot_moment(paths.db, "coco", "画", visibility="private"), "好看")
    t = _text_for(calls, "coco")
    assert '原朋友圈（私密）："画"' in t and H.PRIVATE_HINT_NEUTRAL in t and H.OLD_FALLBACK not in t


def test_评论回复通知_私密动态_填了称呼用原句(c, paths, backends):
    _scene(backends)
    _, calls = _comment(c, backends, H.bot_moment(paths.db, "anna", "画", visibility="private"), "好看")
    assert "（这是私密朋友圈，只有 主人 能看到，可以放开亲密称呼。）" in _text_for(calls, "anna")


def test_评论回复通知_历史评论只列最近20条_每条截到100字_换行换空格(c, paths, backends):
    _scene(backends)
    mid = H.bot_moment(paths.db, "coco", "画")
    base = 1760000000
    bodies = []
    for i in range(25):
        b = "历史%02d号" % i + ("甲\n乙" if i == 24 else "") + "长" * 120
        bodies.append(b)
        H.add_comment(paths.db_path, mid, H.ME if i % 2 else "coco", b, ts=base + i)
    _, calls = _comment(c, backends, mid, "新评论")
    t = _text_for(calls, "coco")
    assert [i for i in range(25) if "历史%02d号" % i in t] == list(range(5, 25))
    flat = bodies[23]
    assert flat[:100] in t and flat[:101] not in t
    assert "历史24号甲 乙" in t
    assert len(t.encode("utf-8")) <= 15000


def test_评论回复通知_历史里别的bot按显示名标_自己标你_用户标用户(c, paths, backends):
    _scene(backends)
    mid = H.bot_moment(paths.db, "coco", "画")
    H.add_comment(paths.db_path, mid, "dora", "朵拉的话-pm", ts=1760000001)
    H.add_comment(paths.db_path, mid, "coco", "自己的话-pm", ts=1760000002)
    H.add_comment(paths.db_path, mid, H.ME, "用户的话-pm", ts=1760000003)
    _, calls = _comment(c, backends, mid, "新评论")
    t = _text_for(calls, "coco")
    assert "朵拉" in _line_with(t, "朵拉的话-pm").replace("朵拉的话-pm", "")
    assert "你" in _line_with(t, "自己的话-pm").replace("自己的话-pm", "")
    assert "用户" in _line_with(t, "用户的话-pm").replace("用户的话-pm", "")


# ── 通知文字：普通评论（群投，38）───────────────────────────────────────────

def test_普通评论通知_开头_命令里父评论是这条新评论(c, paths, backends):
    _scene(backends)
    mid, _ = _post(c, backends)
    body, calls = _comment(c, backends, mid, "风好大")
    t = _text_for(calls, "coco")
    assert t.startswith("[moment-interaction] 用户在自己的朋友圈下评论了：")
    assert (str(mid), str(body["id"])) in re.findall(r"scripts/moment_reply\.py (\d+) (\d+)", t)


def test_普通评论通知_恰好一行私聊规则(c, paths, backends):
    _scene(backends)
    mid, _ = _post(c, backends)
    _, calls = _comment(c, backends, mid, "风好大")
    for x in calls:
        addr = "主人" if x["bot_id"] == "anna" else H.ADDR
        assert H.dm_lines(x["text"]) == [H.DM_RULE % addr], x["bot_id"]


def test_普通评论通知_其他评论按时间从早到晚_不含这条(c, paths, backends):
    _scene(backends)
    mid, _ = _post(c, backends)
    now = int(time.time())
    H.add_comment(paths.db_path, mid, "anna", "我也想去-pm", ts=now - 100)
    H.add_comment(paths.db_path, mid, H.ME, "风好大-pm", ts=now - 50)
    H.add_comment(paths.db_path, mid, "dora", "带上我-pm", ts=now - 10)
    _, calls = _comment(c, backends, mid, "这一条是新评论")
    for x in calls:
        t = x["text"]
        assert t.count("这一条是新评论") == 1, x["bot_id"]
        pos = [t.find(s) for s in ("我也想去-pm", "风好大-pm", "带上我-pm")]
        assert -1 not in pos and pos == sorted(pos), (x["bot_id"], pos)


def test_普通评论通知_其他评论只列最近20条_截断与换行规则(c, paths, backends):
    _scene(backends)
    mid, _ = _post(c, backends)
    base = int(time.time()) - 1000
    bodies = ["其他%02d号" % i + "长" * 120 for i in range(25)]
    bodies[24] = "其他24号甲\n乙" + "长" * 10
    for i, b in enumerate(bodies):
        H.add_comment(paths.db_path, mid, "dora", b, ts=base + i)
    _, calls = _comment(c, backends, mid, "新评论")
    t = _text_for(calls, "coco")
    assert [i for i in range(25) if "其他%02d号" % i in t] == list(range(5, 25))
    assert bodies[23][:100] in t and bodies[23][:101] not in t
    assert "其他24号甲 乙" in t


def test_普通评论通知_标签_自己标你_用户标称呼_别的在跑的人标显示名(c, paths, backends):
    _scene(backends)
    mid, _ = _post(c, backends)
    now = int(time.time())
    H.add_comment(paths.db_path, mid, "coco", "可可说-pm", ts=now - 30)
    H.add_comment(paths.db_path, mid, "dora", "朵拉说-pm", ts=now - 20)
    H.add_comment(paths.db_path, mid, H.ME, "用户说-pm", ts=now - 10)
    _, calls = _comment(c, backends, mid, "新评论")
    t = _text_for(calls, "coco")
    assert "你" in _line_with(t, "可可说-pm").replace("可可说-pm", "")
    assert "朵拉" in _line_with(t, "朵拉说-pm").replace("朵拉说-pm", "")
    assert "用户" in _line_with(t, "用户说-pm").replace("用户说-pm", "")
    assert "主人" in _line_with(_text_for(calls, "anna"), "用户说-pm").replace("用户说-pm", "")


def test_普通评论通知_长数据下不超15000字节且命令行完整(c, paths, backends):
    _scene(backends)
    mid, _ = _post(c, backends, text="长" * 2000)
    for i in range(30):
        H.add_comment(paths.db_path, mid, "dora", "长" * 500, ts=1760000000 + i)
    body, calls = _comment(c, backends, mid, "长" * 500)
    for x in calls:
        assert len(x["text"].encode("utf-8")) <= 15000, x["bot_id"]
        assert re.search(r"scripts/moment_reply\.py %d %d\b" % (mid, body["id"]), x["text"]), x["bot_id"]


def test_普通评论通知_动态原文只带前200字(c, paths, backends):
    _scene(backends)
    moment = "头" + "长" * 249
    mid, _ = _post(c, backends, text=moment)
    _, calls = _comment(c, backends, mid, "x")
    t = _text_for(calls, "coco")
    assert moment[:200] in t and moment[:201] not in t


def test_普通评论通知_带图时含附图说明与绝对路径(c, paths, backends):
    _scene(backends)
    mid, _ = _post(c, backends)
    body, calls = _comment(c, backends, mid, "看图", image=H.data_url(H.png("notify-img")))
    t = _text_for(calls, "coco")
    assert "（用户 还附了张图：%s）" % body["image_path"] in t
    assert "（主人 还附了张图：%s）" % body["image_path"] in _text_for(calls, "anna")


def test_普通评论通知_私密动态标私密仅你可见(c, paths, backends):
    _scene(backends)
    mid, _ = _post(c, backends, visibility="private")
    _, calls = _comment(c, backends, mid, "x")
    assert "私密（仅你可见）" in _text_for(calls, "coco")


def test_普通评论通知_没填称呼不出现哥哥(c, paths, backends):
    _scene(backends)
    mid, _ = _post(c, backends)
    H.add_comment(paths.db_path, mid, H.ME, "早先的评论", ts=int(time.time()) - 5)
    _, calls = _comment(c, backends, mid, "x")
    for b in ("coco", "dora", "fay"):
        assert H.OLD_FALLBACK not in _text_for(calls, b), b


# ── 通知里不带令牌密钥（38）─────────────────────────────────────────────────

def test_三种通知都不含令牌密钥与全局参数原文(c, paths, backends):
    _scene(backends)
    token = "1234567" + ":" + "Q" * 35
    key = "sk" + "-" + "PMOMSENTINEL" + "x" * 20
    raw_line = "pmom_marker_line: PMOM-GLOBAL-RAW-7731"
    paths.gcfg.write_text('user_display_name: "我"\napi_key: "%s"\nbot_token: "%s"\n%s\n' % (key, token, raw_line),
                          encoding="utf-8")
    mid, sent = _post(c, backends)
    _, c1 = _comment(c, backends, mid, "风好大")
    _, c2 = _comment(c, backends, H.bot_moment(paths.db, "coco"), "好看")
    allc = sent + c1 + c2
    assert {x["source"] for x in allc} == {"user_moment", "moment_reply"}
    leaks = [(x["bot_id"], s) for x in allc for s in (token, key, "PMOM-GLOBAL-RAW-7731")
             if s in x["text"] or s in x["key"]]
    assert leaks == []
