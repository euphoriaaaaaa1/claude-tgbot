# -*- coding: utf-8 -*-
"""`configs/_global.yml` 的唯一读写落点（INTERFACE-hub2 §2 §3）。

四件事，都只有一份实现：

1. **参数 schema**：`jiwen/engine.py` 的 `Rates` / `Thresholds` 自省出**字段与默认值**，
   范围/中文标签/单位手写在 `FIELD_META`，按字段名 join（PLAN D4）。
   两个方向的漂移 + `min <= 默认值 <= max` 都在**导入期** assert —— 进程起不来好过
   把一个没标签、范围靠猜的浮点旋钮交给用户去改生产配置。
2. **行级 YAML 编辑器（F2）**：`yaml.compose()` 拿标量节点的字符区间，**只换那一段**。
   注释、锚点 `&base`、别名 `*base`、merge key `<<:`、块标量 `|`、键序、`0.20` 这种
   数值书写格式**全部逐字节保真**。二期任何写路径都**不把 YAML 重新 dump 出来**
   （§2.4 明写这条可 grep 断言，所以本文件连那个函数名都不出现）。
3. **敏感值遮蔽与回填（D2）**：读原文时把敏感标量就地换成 `__KEEP_EXISTING__`；
   写回时按**点分路径**去磁盘现文件取原值填回，服务端零状态。
4. **写盘顺序（§2.4）**：版本乐观锁 → 备份(O_EXCL 一次建成 600) → 行级编辑 →
   原子写 → **轮转排在写盘成功之后**（写失败却先删了最老备份 = 用户净亏一份历史）。

路径接缝：`HUB_CONFIGS_DIR`（默认仓库 `configs/`），**每次调用现读 env** ——
测试靠 monkeypatch 换目录，导入期定死会串到真 `configs/`。
"""
import contextlib
import dataclasses
import datetime
import json
import os
import re
import stat
import tempfile
import threading

import yaml
from flask import current_app

import prefilter
import quota
from jiwen import engine
from moments.provider_model import HubError
from moments.redact import is_secret_field

PLACEHOLDER = "__KEEP_EXISTING__"
GLOBAL_YML = "_global.yml"
SAVE_PREFIX = GLOBAL_YML + ".bak."
RESTORE_PREFIX = GLOBAL_YML + ".pre-restore."
SAVE_KEEP = 5                       # §2.4：save 类只留 5 份，restore 类不占配额
MAX_RAW_BYTES = 1024 * 1024         # §3.2 检查 2
REQUIRED_TOP_KEYS = ("user_display_name", "max_calls_per_5h", "moments", "jiwen")

# §3.5：`id` 会拼进文件名，所以它是信任边界 —— 只认这一个形状，`..` / `%00` 一律当"没这份备份"
BACKUP_ID_RE = re.compile(r"^\d{8}-\d{6}(?:-\d{2})?$")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MISSING = object()


def configs_dir():
    return os.environ.get("HUB_CONFIGS_DIR") or os.path.join(_REPO_ROOT, "configs")


def global_path():
    return os.path.join(configs_dir(), GLOBAL_YML)


def version_of(path=None):
    """§2.1：`f"{st_mtime_ns}-{st_size}"`；文件不存在时 `"absent"`。"""
    try:
        st = os.stat(path or global_path())
    except OSError:
        return "absent"
    return "%d-%d" % (st.st_mtime_ns, st.st_size)


# ======================================================================
# 参数 schema（PLAN D4：自省字段与默认值 + 手写范围文案，按字段名 join）
# ======================================================================

# 手写部分：(下界, 上界, 中文标签, 单位)。**默认值不在这里** —— 它从 dataclass 自省。
# 区间口径见 INTERFACE §2.2（R7：`*_multiplier` 与 `connection_accel` 是 [0,5]，
# 因为 valence_mild_multiplier 默认就是 1.5，划进 [0,1] 会让用户什么都不改一保存就 400）。
_RATE_META = {
    "connection_per_min": (0.0, 1.0, "基础累积速率", "每分钟"),
    "pride_decay_per_min": (0.0, 1.0, "pride 回归速率", "每分钟"),
    "valence_decay_per_min": (0.0, 1.0, "valence 回归速率", "每分钟"),
    "arousal_decay_per_min": (0.0, 1.0, "arousal 衰减速率", "每分钟"),
    "valence_lock_factor": (0.0, 1.0, "情绪锁定衰减乘数", ""),
    "arousal_connection_rise_rate": (0.0, 1.0, "arousal 攀升速率", "每分钟"),
    "pride_defend_rate": (0.0, 1.0, "pride 自我防卫速率", "每分钟"),
    "pride_arousal_conflict_rate": (0.0, 1.0, "pride 与 arousal 冲突速率", "每分钟"),
    "pride_erosion_rate": (0.0, 1.0, "pride 消解速率", "每分钟"),
    "immersion_decay_per_min": (0.0, 1.0, "沉浸度衰减速率", "每分钟"),
    "activity_connection_relief": (0.0, 1.0, "做事对 connection 的缓解量", "每分钟"),
    "connection_accel": (0.0, 5.0, "冷落加速指数", ""),
    "valence_severe_multiplier": (0.0, 5.0, "严重低落时的速率倍率", ""),
    "valence_mild_multiplier": (0.0, 5.0, "轻度低落时的速率倍率", ""),
    "valence_severe_low": (-1.0, 1.0, "严重低落阈值", ""),
    "valence_mild_low": (-1.0, 1.0, "轻度低落阈值", ""),
    "valence_lock_threshold": (-1.0, 1.0, "情绪锁定触发阈值", ""),
    "arousal_connection_rise_threshold": (-1.0, 1.0, "arousal 攀升触发阈值", ""),
    "pride_defend_threshold": (-1.0, 1.0, "pride 自我防卫触发阈值", ""),
    "pride_defend_target": (-1.0, 1.0, "pride 自我防卫目标值", ""),
    "accel_threshold_min": (0.0, 1440.0, "距上次消息多久才启动加速", "分钟"),
}

_IMMERSION_LABELS = {"reading": "看书", "cooking": "做饭", "search": "查资料",
                     "browse": "刷网页", "observe": "发呆观察", "selfcare": "自我照料"}

_THRESHOLD_META = {
    "notice": (0.0, 1.0, "起念头（不发出）", ""),
    "consider": (0.0, 1.0, "考虑开口（pride 可阻断）", ""),
    "forced": (0.0, 1.0, "强制开口（无视 pride）", ""),
}


def _dc_defaults(cls):
    """dataclass 的字段默认值（含 default_factory）。自省的唯一入口。"""
    out = {}
    for f in dataclasses.fields(cls):
        if f.default is not dataclasses.MISSING:
            out[f.name] = f.default
        elif f.default_factory is not dataclasses.MISSING:   # type: ignore[misc]
            out[f.name] = f.default_factory()                # type: ignore[misc]
    return out


def _build_schema():
    """自省 × 手写文案 join 成 {点分键: {...}}，顺带做三个方向的漂移检查。

    检查①②只对 `Rates` 双向做：`Thresholds` 的另外 5 个字段按 §2.2 **明确不进表单**，
    对它只查"手写的键 dataclass 里有"这一个方向。
    """
    rates, thresholds = _dc_defaults(engine.Rates), _dc_defaults(engine.Thresholds)
    immersion = rates.pop("immersion_map", {})

    missing_meta = sorted(set(rates) - set(_RATE_META))
    if missing_meta:
        raise AssertionError(
            "engine.Rates 新增了字段但 FIELD_META 没有文案（补一行范围+标签）：%s" % missing_meta)
    stale_meta = sorted((set(_RATE_META) - set(rates))
                        | (set(_THRESHOLD_META) - set(thresholds))
                        | (set(_IMMERSION_LABELS) - set(immersion)))
    if stale_meta:
        raise AssertionError("FIELD_META 里的字段 engine 已删/改名：%s" % stale_meta)

    meta = {}
    for name, (lo, hi, label, unit) in _RATE_META.items():
        meta["jiwen.rates." + name] = _item("float", rates[name], lo, hi, label, unit)
    for name, label in _IMMERSION_LABELS.items():
        meta["jiwen.rates.immersion_map." + name] = _item(
            "float", immersion[name], 0.0, 1.0, label + "的初始沉浸度", "")
    for name, (lo, hi, label, unit) in _THRESHOLD_META.items():
        meta["jiwen.thresholds." + name] = _item(
            "float", thresholds[name], lo, hi, label, unit)
    # 以下四个键没有 dataclass 撑腰，默认值取各消费方代码里的那一个（不另立一份数字）
    meta["jiwen.tick_interval_min"] = _item("int", 5, 1, 60, "心跳间隔", "分钟")
    meta["moments.enabled"] = _item("bool", True, None, None, "朋友圈总开关", "")
    meta["moments.daily_post_limit_per_bot"] = _item(
        "int", 3, 0, 50, "每个 bot 每天发帖上限", "条")     # moments/post.py 的回落值
    meta["moments.silence_threshold_minutes"] = _item(
        "int", prefilter.DEFAULT_SILENCE_THRESHOLD_MIN, 1, 1440, "静默判定时长", "分钟")
    meta["max_calls_per_5h"] = _item(
        "int", quota.DEFAULT_MAX_CALLS_PER_5H, 1, 1000, "5 小时窗口调用上限", "次")

    bad = [k for k, it in meta.items()
           if it["type"] != "bool" and not (it["min"] <= it["default"] <= it["max"])]
    if bad:      # R7：字段名维度之外，取值维度的漂移也要抓（valence_mild_multiplier 就是它抓的）
        raise AssertionError("这些字段的默认值落在 FIELD_META 给的范围之外：%s" % sorted(bad))
    return meta


def _item(typ, default, lo, hi, label, unit):
    return {"type": typ, "default": default, "min": lo, "max": hi,
            "label": label, "unit": unit}


FIELD_META = _build_schema()      # 导入期就跑完三个 assert：漂移了进程直接起不来


# ======================================================================
# 行级 YAML 编辑器（F2）—— 二期唯一的 YAML 写入手段，全程不 dump
# ======================================================================


def compose(text):
    """解析成节点树；失败抛 YAMLError（调用方负责翻成 bad_yaml / global_yml_unreadable）。"""
    return yaml.compose(text, Loader=yaml.SafeLoader)


def yaml_error_detail(exc, prefix):
    """只取行号，**绝不带上 str(exc)** —— PyYAML 的消息里带出错那一行的原文，
    对"用户提交的文本"是回显、对"磁盘上的文件"就是把明文 key 吐进响应。"""
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    if mark is not None:
        return "%s：第 %d 行附近" % (prefix, mark.line + 1)
    return prefix


def scalars(node, prefix=""):
    """深度遍历，产出 (点分路径, 叶子键名, ScalarNode)。

    **跳过 merge key `<<`**：它的值节点就是锚点定义处那一个对象，跟着走会把
    `_defaults.retry` 重复报成 `jiwen.delta_llm.<<.retry`，替换时同一段字节被改两次。
    锚点本体会在它自己的定义位置被遍历到，不会漏。
    """
    if isinstance(node, yaml.MappingNode):
        for k, v in node.value:
            name = getattr(k, "value", None)
            if not isinstance(name, str) or name == "<<":
                continue
            path = prefix + "." + name if prefix else name
            if isinstance(v, yaml.ScalarNode):
                yield path, name, v
            else:
                for item in scalars(v, path):
                    yield item


def find(node, parts):
    """按点分路径段取节点；取不到回 None（不猜结构）。"""
    cur = node
    for part in parts:
        if not isinstance(cur, yaml.MappingNode):
            return None
        for k, v in cur.value:
            if getattr(k, "value", None) == part:
                cur = v
                break
        else:
            return None
    return cur


def splice(text, spans):
    """spans = [(起, 止, 新片段)]，**从后往前**替换，前面的下标才不会被挪动。

    按 (起, 止) 去重，首见保留：YAML 别名（*alias）让 compose 对同一节点对象
    产出多条记录，重复区间叠加替换会错位吃掉后续字节（新片段长度≠原区间长度）。
    去重放在这里而不是各调用方——mask_secrets 与 resolve_placeholders 都从
    scalars() 拿 span，将来任何新调用方也自动安全。"""
    uniq = {}
    for start, end, piece in spans:
        uniq.setdefault((start, end), piece)
    out = text
    for (start, end), piece in sorted(uniq.items(), key=lambda s: s[0][0], reverse=True):
        out = out[:start] + piece + out[end:]
    return out


def _literal(value, typ):
    """写回 YAML 的标量字面量。float 一律走 repr，`5` → `5.0`（§2.3：float 接受 int）。"""
    if typ == "bool":
        return "true" if value else "false"
    if typ == "int":
        return str(int(value))
    if typ == "str":
        # 表单里没有字符串旋钮（FIELD_META 只有 bool/int/float），这一格是给
        # 老的 /api/image_provider 用的：它原来把整表 dump 回去，一次切生图 provider
        # 就把 _global.yml 的注释与锚点全展平。json.dumps 的双引号串是 YAML 的
        # 合法子集 —— 引号/换行/emoji 都不用自己转义，也就没有"漏一种转义"的洞。
        return json.dumps(str(value), ensure_ascii=False)
    return repr(float(value))


def set_scalar(text, root, key, value, typ):
    """把 `key` 这个叶子标量改成 `value`，返回新文本。

    - 叶子在 → 只替换它的值那一段字节（行尾注释、缩进、其余行一字不动）；
    - 叶子不在但父映射在 → 按父的既有子项缩进，在块首插一行；
    - 父映射也不在 → `409 key_absent`，行级编辑器无处落笔，不猜结构。
    """
    parts = key.split(".")
    leaf = find(root, parts)
    literal = _literal(value, typ)
    if isinstance(leaf, yaml.ScalarNode):
        return splice(text, [(leaf.start_mark.index, leaf.end_mark.index, literal)])
    parent = find(root, parts[:-1])
    if not isinstance(parent, yaml.MappingNode) or not parent.value:
        raise HubError(409, "key_absent",
                       "配置里没有 %s，父层级也不存在；请先用高级模式补上该键" % key)
    anchor = parent.value[0][0]          # 父块的第一个子键：拿它的列号当缩进，行首当插入点
    col = anchor.start_mark.column
    at = anchor.start_mark.index - col
    return text[:at] + "%s%s: %s\n" % (" " * col, parts[-1], literal) + text[at:]


def mask_secrets(text, root):
    """把每个敏感标量的值就地换成占位符，返回 (遮蔽后文本, 敏感路径列表)。

    敏感判定沿用 [I1] §0.2 的字段名族（`key|token|secret|password|credential|auth`），
    与出站脱敏共用一份 `redact.is_secret_field`，不另立一套正则。
    """
    spans, paths, seen = [], [], set()
    for path, name, node in scalars(root):
        if not is_secret_field(name):
            continue
        # YAML 别名（*alias）让 compose 返回同一个节点对象，同一段字节会被产出多次；
        # 重复 span 叠加替换会错位吃掉后续字节（占位符长度≠原值长度）。按位置去重，
        # 保留首见路径（锚点定义处），别名引用处报的路径本就不是实际改动位置。
        pos = (node.start_mark.index, node.end_mark.index)
        if pos in seen:
            continue
        seen.add(pos)
        spans.append((pos[0], pos[1], PLACEHOLDER))
        paths.append(path)
    return splice(text, spans), paths


def resolve_placeholders(text, root):
    """把提交文本里**敏感键上**的占位符按点分路径回填成磁盘现值。

    非敏感键上的 `__KEEP_EXISTING__` 按字面量原样留下（PLAN D2 边界 3：不猜用户意图）。
    解不掉的路径（键被改名/移动）全部收集起来交给调用方报 400，**一个都不写盘**。
    """
    targets = [(p, n) for p, name, n in scalars(root)
               if n.value == PLACEHOLDER and is_secret_field(name)]
    if not targets:
        return text, []
    disk = read_global()
    disk_root = None
    if disk is not None:
        try:
            disk_root = compose(disk)
        except yaml.YAMLError:
            disk_root = None
    spans, unresolved = [], []
    for path, node in targets:
        src = find(disk_root, path.split(".")) if disk_root is not None else None
        if not isinstance(src, yaml.ScalarNode):
            unresolved.append(path)
            continue
        spans.append((node.start_mark.index, node.end_mark.index,
                      disk[src.start_mark.index:src.end_mark.index]))
    return (text if unresolved else splice(text, spans)), unresolved


# ======================================================================
# 磁盘：读 / 备份 / 原子写 / 轮转（§2.4）
# ======================================================================


def read_global():
    """读 `_global.yml` 全文；文件不存在或读不出来回 None（不抛，调用方按语境定码）。"""
    try:
        with open(global_path(), "r", encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def load_global():
    """(数据, 原文)；原文读不出或不是映射时数据为 None。表单端点只要数据。"""
    text = read_global()
    if text is None:
        return None, None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None, text
    return (data if isinstance(data, dict) else None), text


def _mode_of(path):
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return 0o600


def make_backup(kind, content):
    """把 `content` 存成一份备份，返回备份 id。

    `O_CREAT|O_EXCL|0o600` 一次建成：没有"先建再 chmod"的 644 窗口，
    也天然不会覆盖同名的既有备份（同秒冲突就往后取 `-01`…`-99`）。
    """
    prefix = SAVE_PREFIX if kind == "save" else RESTORE_PREFIX
    base = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    for bid in [base] + ["%s-%02d" % (base, i) for i in range(1, 100)]:
        target = os.path.join(configs_dir(), prefix + bid)
        try:
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        except OSError as e:
            raise HubError(500, "config_write_failed", "备份失败：%s" % type(e).__name__)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content.encode("utf-8"))
        except OSError as e:
            raise HubError(500, "config_write_failed", "备份失败：%s" % type(e).__name__)
        return bid
    raise HubError(500, "config_write_failed", "同一秒内的备份名已用尽")


def _atomic_write(text):
    """mkstemp（0600 随机名）→ 写 → fsync → os.replace。失败一律 500，不留临时文件。"""
    path = global_path()
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=configs_dir(), prefix=".global-", suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, _mode_of(path))
        os.replace(tmp, path)
        tmp = None
    except OSError as e:
        raise HubError(500, "config_write_failed", "写配置失败：%s" % type(e).__name__)
    finally:
        if tmp:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def rotate_saves():
    """§2.4 第 5 步：**必须在写盘成功之后**跑。按文件名时间戳排序删最老的，不看 mtime。"""
    try:
        # 过 BACKUP_ID_RE：不然本地放一个 _global.yml.bak.zzz 就能把真备份逐个挤掉，
        # 而 list_backups 过滤了正则、页面上还看不到这个占位的
        names = sorted(n for n in os.listdir(configs_dir())
                       if n.startswith(SAVE_PREFIX) and BACKUP_ID_RE.match(n[len(SAVE_PREFIX):]))
    except OSError:
        return
    for name in names[:-SAVE_KEEP] if len(names) > SAVE_KEEP else []:
        # 并发下两边可能算出同一份"最老的"，晚到的那次 FileNotFoundError 不该冒成 500
        with contextlib.suppress(OSError):
            os.unlink(os.path.join(configs_dir(), name))


def list_backups():
    """§3.4：新到旧。目录读不到 → 空列表（正常态，不是错误）。**不含备份内容**。"""
    directory = configs_dir()
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    out = []
    for name in names:
        for prefix, kind in ((SAVE_PREFIX, "save"), (RESTORE_PREFIX, "restore")):
            if not name.startswith(prefix):
                continue
            bid = name[len(prefix):]
            if not BACKUP_ID_RE.match(bid):
                continue
            try:
                st = os.stat(os.path.join(directory, name))
            except OSError:
                break
            out.append({"id": bid, "kind": kind,
                        "size": int(st.st_size), "ts": int(st.st_mtime)})
            break
    out.sort(key=lambda b: b["id"], reverse=True)
    return out


def backup_path(bid, kind="save"):
    """把用户给的 `id` 解析成路径。**形状不对一律回 None** —— 这是拼文件名的信任边界。"""
    if not isinstance(bid, str) or not BACKUP_ID_RE.match(bid):
        return None
    prefix = SAVE_PREFIX if kind == "save" else RESTORE_PREFIX
    return os.path.join(configs_dir(), prefix + bid)


@contextlib.contextmanager
def hold_config_lock(app=None):
    """三个写端点共用的进程内非阻塞锁（§2.4），抢不到 → `409 config_busy`。

    与版本乐观锁解决的**不是同一个问题**：锁挡"同时写"，版本号挡"拿着旧快照写"，两者都要。
    ponytail: 单进程前提（Flask threaded=True）；真上多 worker 就把它换成 flock，接口不变。
    """
    ext = (app or current_app).extensions
    lock = ext.setdefault("hub_config", threading.Lock())    # dict.setdefault 在 GIL 下原子
    if not lock.acquire(blocking=False):
        raise HubError(409, "config_busy", "另一次保存正在进行，请稍候重试")
    try:
        yield
    finally:
        lock.release()


def check_version(version):
    """§2.4 第 1 步：与磁盘现值不符 → 409，**此前未产生任何副作用**（不写盘、不备份）。"""
    if version != version_of():
        raise HubError(409, "config_stale", "配置已被他人修改，请刷新后重试")


def commit(new_text, kind="save", backup=True):
    """备份 → 原子写 → 轮转。返回本次备份 id（backup=False 时 None）。调用方须已持锁并过完版本校验。

    backup=False 给低权面的单枚举翻转用（如 /api/image_provider 切生图 provider）：
    行级编辑器 + 原子写本身保真，改错了再切回即可；若照常备份，低权用户高频切换
    会把 admin 参数页的 5 份 save 备份全部挤掉（跨权限副作用）。"""
    if not backup:
        _atomic_write(new_text)
        return None
    current = read_global()
    bid = make_backup(kind, current if current is not None else "")
    _atomic_write(new_text)
    if kind == "save":          # restore 类不挤 save 的 5 份配额（R3-3）
        rotate_saves()
    return bid


# ======================================================================
# 表单：取值 / 校验 / 应用（§2.1 §2.3）
# ======================================================================


def dig(data, key):
    """按点分路径取值；任一层不是映射或缺键 → `_MISSING`。"""
    cur = data
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def form_values(data):
    """§2.1：yml 里缺的键回落 dataclass 默认值，并记一条 `missing_in_yml:<键>` 告警
    （首次安装就是这个态，不是错误）。"""
    values, warnings = {}, []
    for key, item in FIELD_META.items():
        got = dig(data, key) if isinstance(data, dict) else _MISSING
        if got is _MISSING:
            values[key] = item["default"]
            warnings.append("missing_in_yml:" + key)
        else:
            values[key] = got
    return values, warnings


def schema_list():
    """§2.1 的 `schema`：**只含 FIELD_META 里的字段**（B1）。bool 不给 min/max。"""
    out = []
    for key, item in FIELD_META.items():
        row = {"key": key, "type": item["type"], "default": item["default"],
               "label": item["label"], "unit": item["unit"]}
        if item["type"] != "bool":
            row["min"] = item["min"]
            row["max"] = item["max"]
        out.append(row)
    return out


def _bad(key, why):
    raise HubError(400, "bad_field", "%s %s" % (key, why))


def validate_values(values):
    """§2.3 逐键校验，先命中先返回。类型规则写死：
    bool 只收 JSON true/false；int 只收 JSON 整数（`5.0` 不算）；float 收 int 与 float。"""
    for key, raw in values.items():
        item = FIELD_META.get(key)
        if item is None:
            _bad(key, "不是可在表单里修改的参数")
        typ = item["type"]
        if typ == "bool":
            if not isinstance(raw, bool):
                _bad(key, "的值必须是 bool（只接受 true / false）")
            continue
        if typ == "int":
            if isinstance(raw, bool) or not isinstance(raw, int):
                _bad(key, "的值必须是 int 整数")
        else:
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                _bad(key, "的值必须是 float 数值")
        num = float(raw)
        if num != num or num in (float("inf"), float("-inf")):   # NaN / ±Infinity
            _bad(key, "必须是有限数值，允许范围 [%s, %s]" % (item["min"], item["max"]))
        if not (item["min"] <= num <= item["max"]):
            _bad(key, "超出允许范围 [%s, %s]" % (item["min"], item["max"]))


def _same(current, new, typ):
    """"提交值与磁盘现值是不是同一个值"。类型不对（磁盘上是字符串等）一律算不同。"""
    if current is _MISSING:
        return False
    if typ == "bool":
        return current is new
    if isinstance(current, bool) or not isinstance(current, (int, float)):
        return False
    return float(current) == float(new)


def restart_hint(keys):
    """§5 + §13 A8：逐键求值后**按位或**；`changed: []` 时两者均为 false。"""
    return {"bot": any(k.startswith("jiwen.") or k == "max_calls_per_5h" for k in keys),
            "moments_web": any(k.startswith("moments.") for k in keys)}


def apply_form(values, data, text):
    """算出真的变了的键 + 编辑后的新文本。**不碰磁盘** —— key_absent 要在备份之前就炸出来，
    否则用户拿到 409 的同时还白多一份备份。

    ponytail: 每改一个键重新 compose 一次（35 个键、几 KB 的文件，O(n·len) 无所谓），
    换来的是不用自己维护 splice 之后的下标偏移，那才是 3am 要解码的东西。
    """
    changed = [k for k, v in values.items()
               if not _same(dig(data, k), v, FIELD_META[k]["type"])]
    new_text = text
    for key in changed:
        new_text = set_scalar(new_text, compose(new_text), key,
                              values[key], FIELD_META[key]["type"])
    return changed, new_text


def missing_top_keys(root):
    """§3.2 检查 5：四个顶层键缺任一就报，它们是各消费方一定会读的结构。"""
    have = {getattr(k, "value", None) for k, _ in root.value}
    return [k for k in REQUIRED_TOP_KEYS if k not in have]


def _selftest():
    """`python3 -m moments.hub_config` 跑的自检：行级编辑器的保真与边界。"""
    src = ("# c\na: 1\nblk: |\n  x\nm:\n  api_key: real-secret\n"
           "  n: 0.20   # tail\n  deep:\n    q: 1\n")
    root = compose(src)
    out = set_scalar(src, root, "m.n", 0.25, "float")
    assert out == src.replace("0.20", "0.25"), out          # 只动值，行尾注释还在
    assert "# c" in out and "blk: |" in out
    out2 = set_scalar(src, root, "m.deep.z", 3, "int")
    assert "    z: 3\n" in out2 and "    q: 1\n" in out2, out2   # 按父的缩进补一行
    try:
        set_scalar(src, root, "nope.z", 1, "int")
        raise AssertionError("父映射不存在时该报 key_absent")
    except HubError as e:
        assert e.error == "key_absent", e.error
    masked, paths = mask_secrets(src, root)
    assert paths == ["m.api_key"] and "real-secret" not in masked
    assert masked.count("\n") == src.count("\n")            # 遮蔽不改行数
    back, unresolved = resolve_placeholders(masked, compose(masked))
    assert unresolved == ["m.api_key"] and back == masked   # 磁盘上没有 → 全部解不掉
    assert _literal(5, "float") == "5.0" and _literal(True, "bool") == "true"
    assert restart_hint(["moments.enabled", "jiwen.x"]) == {"bot": True, "moments_web": True}
    assert restart_hint([]) == {"bot": False, "moments_web": False}
    assert backup_path("../x") is None and backup_path("20200101-000000") is not None
    validate_values({"moments.enabled": False, "max_calls_per_5h": 1})
    for bad in ({"nope": 1}, {"moments.enabled": 1}, {"jiwen.tick_interval_min": 5.0},
                {"max_calls_per_5h": 0}, {"jiwen.rates.connection_per_min": float("nan")}):
        try:
            validate_values(bad)
            raise AssertionError("该报 bad_field：%r" % (bad,))
        except HubError as e:
            assert e.error == "bad_field", (bad, e.error)
    print("hub_config 自检通过（%d 个表单键）" % len(FIELD_META))


if __name__ == "__main__":
    _selftest()
