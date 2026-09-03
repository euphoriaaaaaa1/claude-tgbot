# -*- coding: utf-8 -*-
"""管理台蓝图（PLAN M1 空壳骨架 / INTERFACE §2 §3 §4 §5 §6）。

四段固定顺序，段界就是并行分工线，改自己那段别越界：

  第 1 段  页面路由        M1 已实现，其余模块只读不改
  第 2 段  provider CRUD   M3 填（§3 + §5 claude-native）
  第 3 段  OAuth 账户      M5 填（§4）
  第 4 段  bots 状态与重启 M7 填（§6）

未填的端点一律回 ``501 {"error":"not_implemented"}``：形状合规（§0.1），
但 **501 不在 §7 错误码总表里** —— 它是「这段还没人填」的施工态，
某段填完时该段的 ``_todo()`` 必须一个不剩。

鉴权由 ``moments/hub_auth.py`` 的 before_request 统管（``/hub`` 及其子路径全锁），
本模块不写任何鉴权判断。``/hub/healthz`` 已在 ``hub_auth.install()`` 注册，**不要在这里重复注册**。
"""
from flask import Blueprint, jsonify, render_template

from moments import provider_model

hub_bp = Blueprint("hub", __name__)

# §4.2：OAuth 回调端口的唯一真相表。路由、页面文案、README 三处都从它取，
# 不在各处各硬编码一遍（[RA] 建议2）。M5 接第 3 段时复用它，别另起一张。
OAUTH_CALLBACK_PORTS = {"codex": 1455, "antigravity": 51121}
OAUTH_PROVIDER_LABELS = {"codex": "ChatGPT", "antigravity": "Google（Antigravity）"}
OAUTH_WAIT_SECONDS = 300        # §4.4 服务端授权等待窗口，页面按它显示倒计时


def _todo():
    """空壳占位：合规 JSON 形状 + 501。填完该端点即删本次调用。"""
    return jsonify({"error": "not_implemented"}), 501


# ======================================================================
# 第 1 段 · 页面路由（M1，已完成）
#
# 铁律（§2 末 / [R4D] I1-3）：三个页面在 cliproxy 没跑、settings.json 被手写坏、
# bot 全离线等任何依赖故障下**一律 200 HTML**，数据全部由前端调 /hub/api/* 取。
# 所以这三个函数**禁止**碰 cliproxy、禁止读 settings.json —— 不碰依赖，
# 就不可能被依赖打成 5xx。要加数据请加到 API 端点里，不要加到这里。
# ======================================================================


@hub_bp.get("/hub")
def hub_home():
    return render_template("hub.html", nav_active="hub")


@hub_bp.get("/hub/provider")
def hub_provider_page():
    # 下拉框的 kind 选项从 KIND_SPEC 派生（[RA] 建议2：校验/互转/页面三处同源）。
    return render_template(
        "hub_provider.html",
        nav_active="provider",
        kinds=provider_model.SUPPORTED_KINDS,
        default_haiku_alias=provider_model.DEFAULT_HAIKU_ALIAS,
        oauth_providers=[{"id": pid,
                          "label": OAUTH_PROVIDER_LABELS[pid],
                          "callback_port": port}
                         for pid, port in OAUTH_CALLBACK_PORTS.items()],
        oauth_wait_minutes=OAUTH_WAIT_SECONDS // 60,
    )


@hub_bp.get("/hub/bots")
def hub_bots_page():
    return render_template("hub_bots.html", nav_active="bots")


# ======================================================================
# 第 2 段 · 第三方 key provider + Claude 原生订阅（M3 填）
#
# 契约：INTERFACE §3.2-§3.7、§5；错误码只许用 §7 总表里的。
# 依赖：M4 的 moments/provider_model.py（校验/互转/落键计算）、
#       M2 的 moments/cliproxy_client.py（管理 API + set_alias_exclusive）、
#       moments/settings_io.py（原子写）。
# 注意 §0.3：cliproxy 三键任一缺失时，GET /hub/api/provider 仍 200
#       （warnings=["cliproxy_unconfigured"]），其余写端点 503；
#       claude-native/activate 不碰 cliproxy，不受影响。
# ======================================================================


@hub_bp.get("/hub/api/provider")
def provider_list():
    return _todo()


@hub_bp.post("/hub/api/provider")
def provider_create():
    return _todo()


@hub_bp.patch("/hub/api/provider/<pid>")
def provider_update(pid):
    return _todo()


@hub_bp.delete("/hub/api/provider/<pid>")
def provider_delete(pid):
    return _todo()


@hub_bp.post("/hub/api/provider/<pid>/activate")
def provider_activate(pid):
    return _todo()


@hub_bp.post("/hub/api/provider/<pid>/test")
def provider_test(pid):
    return _todo()


@hub_bp.post("/hub/api/claude-native/activate")
def claude_native_activate():
    return _todo()


# ======================================================================
# 第 3 段 · OAuth 订阅账户（M5 填）
#
# 契约：INTERFACE §4.1-§4.7。provider 枚举只有 codex / antigravity
# （anthropic 被显式移除，Claude 订阅走第 2 段的 claude-native）。
# 回调端口取本模块顶部的 OAUTH_CALLBACK_PORTS，别再写一张表。
# §4.1 恒 200：读不出来时 accounts=[] + warnings 非空，页面才打得开。
# ======================================================================


@hub_bp.get("/hub/api/oauth/accounts")
def oauth_accounts():
    return _todo()


@hub_bp.post("/hub/api/oauth/start")
def oauth_start():
    return _todo()


@hub_bp.post("/hub/api/oauth/submit")
def oauth_submit():
    return _todo()


@hub_bp.get("/hub/api/oauth/status")
def oauth_status():
    return _todo()


@hub_bp.delete("/hub/api/oauth/accounts/<provider>/<email_hash>")
def oauth_account_delete(provider, email_hash):
    return _todo()


@hub_bp.post("/hub/api/oauth/accounts/<provider>/<email_hash>/activate")
def oauth_account_activate(provider, email_hash):
    return _todo()


# ======================================================================
# 第 4 段 · bot 状态与重启（M7 填）
#
# 契约：INTERFACE §6.0-§6.2。要点：探活用 POST（dispatcher 对非 POST 一律 405）、
# 并发探活整体封顶 2 秒、重启改异步 202 + 轮询、job 只留最近 5 条、
# tail 出站前过 §0.2 第二层脱敏（restart-bots.sh 的 stderr 可能带出 Telegram token）。
# 两个测试接缝 HUB_RESTART_CMD / HUB_BOTS_FILE 在 bots_client.py 里落，不写进 README。
# ======================================================================


@hub_bp.get("/hub/api/bots")
def bots_list():
    return _todo()


@hub_bp.post("/hub/api/bots/restart")
def bots_restart():
    return _todo()


@hub_bp.get("/hub/api/bots/restart/<job_id>")
def bots_restart_status(job_id):
    return _todo()
