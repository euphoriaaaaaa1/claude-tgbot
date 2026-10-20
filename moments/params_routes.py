"""系统参数页的壳（BRIEF-shell-fixes §3）：以 dsh 侧 moments/hub_routes.py 第 5 段（交接副本 code-d491e36）为准
整体搬入，只改越界点：

- 路径：页面 ``/params``，接口 ``/api/params…``（dsh 版是 ``/hub/params``、``/hub/api/params…``），写法同朋友圈页。
- 读写都在锁里：dsh 版先在锁外读盘、算好新文本，再进锁核版本、写盘；这份文件运行时还会被别的程序写，
  所以壳里"读当前文件 → 核版本 → 算新文本 → 备份 → 写"整段在锁里做，读端点也在锁里取原文与版本。
- 生效提示不带重启按钮（dsh 的重启接口不在壳里），页面只给文字提示。

登录门、跨站核对、供应商模式可达面在 moments/web.py 里对本蓝图生效。机器全在 moments/params_config.py，
本模块只做"校验顺序 + 翻码 + 组响应"。
"""
import functools
import logging
import os

import yaml
from flask import Blueprint, jsonify, render_template, request

from moments import params_config as pc

params_bp = Blueprint("params", __name__)
_log = logging.getLogger(__name__)
READ_WAIT = 5.0     # 读端点等锁的秒数；保存只占几毫秒，等不到说明有异常的长占用，回 409 config_busy


def _api(fn):
    """把 ParamsError 翻成 ``{"error","detail"}`` + 状态码；其余异常照旧冒泡给 500 处理器。"""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except pc.ParamsError as e:
            return jsonify({"error": e.error, "detail": e.detail}), e.status
    return wrapper


def _cfg_body():
    """请求体必须是 JSON 对象（数组、字符串、空体一律 400 bad_body）。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise pc.ParamsError(400, "bad_body", "请求体必须是一个 JSON 对象")
    return body


def _cfg_version(body):
    """三个写端点的 `version` 都必填且必须是字符串。"""
    version = body.get("version")
    if not isinstance(version, str):
        raise pc.ParamsError(400, "bad_body", "缺少 version（请先 GET 拿当前版本）")
    return version


def _unreadable():
    return pc.ParamsError(409, "global_yml_unreadable", "configs/_global.yml 读不出来，先用高级模式修复原文")


@params_bp.get("/params")
def params_page():
    # 页面恒 200，状态全由前端调 /api/params* 呈现，这里不碰磁盘。
    return render_template("params.html", placeholder=pc.PLACEHOLDER)


@params_bp.get("/api/params")
@_api
def params_get():
    """**恒 200**（锁等不到除外）。文件坏/不在 → 值全回落默认，advanced_available=false，页面据此禁用保存。"""
    with pc.hold_config_lock(wait=READ_WAIT):
        text, version = pc.snapshot()
    data = pc.parse(text)
    if data is None:
        values, _ = pc.form_values(None)
        return jsonify({"values": values, "schema": pc.schema_list(), "version": version,
                        "advanced_available": False, "warnings": ["global_yml_unreadable"]})
    values, warnings = pc.form_values(data)
    return jsonify({"values": values, "schema": pc.schema_list(), "version": version,
                    "advanced_available": True, "warnings": warnings})


def _voice_skills() -> dict:
    """写盘出锁之后调（voice-writing 3.3）：跟随全局语音模型的 bot 重写语音技能段，汇总放进响应的 ``voice_skills``。
    每次写盘都跑、不比较写前写后（内容没变不写盘）。adapters 在函数里导入：adapters.claude_local 导入期就导入
    moments 下的模块，放在模块顶上容易绕成导入环。出任何异常都不影响已经保存的配置，只在 message 里如实说。"""
    try:
        import adapters
        hook = getattr(adapters, "on_global_config_saved", None)
        if hook is None:
            return {"message": "语音技能没有跟着更新（公开版暂未提供跟随重写），下次在管理台改相关 bot 的语音设置时会补上"}
        return hook()
    except Exception as e:  # noqa: BLE001
        _log.exception("语音技能跟随全局模型重写出错")
        return {"message": "语音技能没有跟着更新（%s），下次在系统参数页保存改动、或在管理台改相关 bot 的语音设置时会补上"
                % type(e).__name__}


def _noop(version):
    return jsonify({"ok": True, "changed": [], "backup": None, "version": version,
                    "restart_hint": pc.restart_hint([])})


@params_bp.post("/api/params")
@_api
def params_save():
    """校验顺序照 dsh 版：body → 未知键/类型/范围 → 空操作 200 → version → 磁盘可读 → 同值空操作 →
    版本乐观锁 → 写。纯输入错优先于冲突错。从"读磁盘"起整段在锁里。"""
    body = _cfg_body()
    values = body.get("values")
    if not isinstance(values, dict):
        raise pc.ParamsError(400, "bad_body", "缺少 values 或它不是 JSON 对象")
    pc.validate_values(values)
    if not values:      # 空操作：不写盘、不备份，也不是错误
        return _noop(pc.version_of())
    version = _cfg_version(body)
    with pc.hold_config_lock():
        text, current = pc.snapshot()
        data = pc.parse(text)
        if data is None:
            raise _unreadable()
        changed, new_text = pc.apply_form(values, data, text)
        if not changed:     # 提交了但与磁盘同值 → 同样不写盘、不备份
            return _noop(current)
        if version != current:
            raise pc.ParamsError(409, "config_stale", "配置已被他人修改，请刷新后重试")
        pc.check_version(version)       # 写盘前再核一次：读完到现在若被别的程序改过，照样 409
        backup = pc.commit(new_text)
        new_version = pc.version_of()
    return jsonify({"ok": True, "changed": changed, "backup": backup, "version": new_version,
                    "restart_hint": pc.restart_hint(changed), "voice_skills": _voice_skills()})


@params_bp.get("/api/params/raw")
@_api
def params_raw_get():
    """行级遮蔽，其余字节逐字原样。坏 YAML 时**不回显任何原文**（定位不了敏感路径，吐原文就等于吐明文 key）。"""
    with pc.hold_config_lock(wait=READ_WAIT):
        text, version = pc.snapshot()
    if text is None:
        raise pc.ParamsError(404, "not_found", "configs/_global.yml 不存在")
    try:
        root = pc.compose(text)
    except yaml.YAMLError as e:
        raise pc.ParamsError(409, "global_yml_unreadable",
                             pc.yaml_error_detail(e, "configs/_global.yml 解析失败"))
    if not isinstance(root, yaml.MappingNode):
        raise pc.ParamsError(409, "global_yml_unreadable", "configs/_global.yml 顶层不是映射：第 1 行")
    masked, paths = pc.mask_secrets(text, root)
    return jsonify({"text": masked, "sensitive_paths": paths, "placeholder": pc.PLACEHOLDER,
                    "version": version})


@params_bp.post("/api/params/raw")
@_api
def params_raw_post():
    """固定校验顺序（body / 体积 / YAML / 顶层 / 关键键 / 占位符 / 版本 / 写盘），任一失败均不写盘、不备份。
    占位符回填要读磁盘现值，从它起在锁里。"""
    body = _cfg_body()
    text = body.get("text")
    if not isinstance(text, str):
        raise pc.ParamsError(400, "bad_body", "缺少 text 或它不是字符串")
    version = _cfg_version(body)
    if len(text.encode("utf-8")) > pc.MAX_RAW_BYTES:
        raise pc.ParamsError(413, "payload_too_large", "配置原文超过 1 MiB")
    try:
        root = pc.compose(text)
    except yaml.YAMLError as e:
        # detail 只给行号：回显用户提交的原文等于把他刚粘进去的 key 又吐回响应里
        raise pc.ParamsError(400, "bad_yaml", pc.yaml_error_detail(e, "YAML 解析失败"))
    if not isinstance(root, yaml.MappingNode):
        raise pc.ParamsError(400, "bad_yaml", "第 1 行：配置顶层必须是一个映射（key: value）")
    missing = pc.missing_top_keys(root)
    if missing:
        raise pc.ParamsError(400, "schema_missing_key", "缺少关键顶层键：%s" % "、".join(missing))
    with pc.hold_config_lock():
        filled, unresolved = pc.resolve_placeholders(text, root)
        if unresolved:
            raise pc.ParamsError(400, "placeholder_unresolved",
                                 "这些路径上的占位符在现配置里找不到对应的原值：%s" % "、".join(unresolved))
        pc.check_version(version)
        backup = pc.commit(filled)
        new_version = pc.version_of()
    # 高级模式无法逐键归因，两个都提示重启
    return jsonify({"ok": True, "backup": backup, "version": new_version,
                    "restart_hint": {"bot": True, "moments_web": True}, "voice_skills": _voice_skills()})


@params_bp.get("/api/params/backups")
@_api
def params_backups():
    """新到旧，**不含备份内容**（内容里有明文 key）。目录读不到 = 空列表，不是错误。"""
    return jsonify({"backups": pc.list_backups()})


@params_bp.post("/api/params/backups/<bid>/restore")
@_api
def params_restore(bid):
    """回滚 = 一次新的写盘：先给当前文件留一份 `pre-restore` 备份再写回，所以回滚本身也可回滚，
    且不挤占 save 的配额（两类各留 2 份）。"""
    body = _cfg_body()
    version = _cfg_version(body)
    path = pc.backup_path(bid)
    if path is None or not os.path.isfile(path):
        raise pc.ParamsError(404, "backup_not_found", "没有这份备份")
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError) as e:
        raise pc.ParamsError(404, "backup_not_found", "备份读不出来：%s" % type(e).__name__)
    try:
        root = pc.compose(content)
    except yaml.YAMLError as e:
        raise pc.ParamsError(400, "bad_yaml", pc.yaml_error_detail(e, "备份内容解析失败"))
    if not isinstance(root, yaml.MappingNode):
        raise pc.ParamsError(400, "bad_yaml", "第 1 行：备份内容顶层不是映射")
    with pc.hold_config_lock():
        pc.check_version(version)
        pre = pc.commit(content, kind="restore")
        new_version = pc.version_of()
    return jsonify({"ok": True, "backup": pre, "version": new_version,
                    "restart_hint": {"bot": True, "moments_web": True}, "voice_skills": _voice_skills()})
