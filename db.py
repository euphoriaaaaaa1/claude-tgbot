"""SQLite 状态存储（WAL 模式，支持并发读写）。

所有表的 schema 在 init() 里。其他模块通过这里的函数访问数据库，
不要在外部直接拼 SQL。
"""
import os
import sqlite3
import tempfile
import time
import json
import contextlib
from datetime import datetime, timezone

# 默认落在仓库根的 state.db；测试/门禁可用 CLAUDEBOT_DB_PATH 指到临时目录，
# 避免运行产物污染公开仓。
DB_PATH = os.path.abspath(os.path.expanduser(
    os.environ.get("CLAUDEBOT_DB_PATH")
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.db")))

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS heartbeat_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT, ts INTEGER,
    mood REAL, mood_factors TEXT,
    recurring TEXT, sporadic TEXT, hobby TEXT,
    wildcard_used TEXT,
    judge_speak INTEGER, judge_reason TEXT,
    skipped INTEGER, skip_reason TEXT,
    final_text TEXT
);
CREATE INDEX IF NOT EXISTS idx_heartbeat_bot_ts ON heartbeat_log(bot_id, ts DESC);

CREATE TABLE IF NOT EXISTS ongoing_events (
    bot_id TEXT, event_name TEXT, effect TEXT, mood_delta REAL,
    started_at INTEGER, expires_at INTEGER, last_mentioned_at INTEGER,
    PRIMARY KEY (bot_id, event_name, started_at)
);

CREATE TABLE IF NOT EXISTS daily_wildcards (
    bot_id TEXT, date TEXT, card_id INTEGER,
    card TEXT, emotion TEXT, used INTEGER DEFAULT 0,
    PRIMARY KEY (bot_id, date, card_id)
);

CREATE TABLE IF NOT EXISTS seen_news (
    item_hash TEXT PRIMARY KEY, seen_at INTEGER
);

CREATE TABLE IF NOT EXISTS event_dice_log (
    bot_id TEXT PRIMARY KEY, last_roll_at INTEGER
);

CREATE TABLE IF NOT EXISTS hobby_log (
    bot_id TEXT, hobby_name TEXT, ts INTEGER,
    PRIMARY KEY (bot_id, ts)
);

CREATE TABLE IF NOT EXISTS call_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER, kind TEXT, weight INTEGER
);
CREATE INDEX IF NOT EXISTS idx_call_log_ts ON call_log(ts DESC);

CREATE TABLE IF NOT EXISTS moments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT, ts INTEGER,
    text TEXT,
    image_path TEXT,
    metadata_json TEXT,
    moment_kind TEXT,
    visibility TEXT DEFAULT 'public'  -- 'public' | 'private'
);
CREATE INDEX IF NOT EXISTS idx_moments_ts ON moments(ts DESC);

CREATE TABLE IF NOT EXISTS moment_likes (
    moment_id INTEGER,
    liker TEXT,                        -- '哥哥' / bot 名 / 等
    ts INTEGER,
    PRIMARY KEY (moment_id, liker)
);
CREATE INDEX IF NOT EXISTS idx_moment_likes_mid ON moment_likes(moment_id);

CREATE TABLE IF NOT EXISTS moment_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    moment_id INTEGER,
    from_user TEXT,                    -- '哥哥' / bot id
    text TEXT,
    ts INTEGER,
    parent_id INTEGER DEFAULT NULL,    -- 回复某条评论时填
    pending INTEGER DEFAULT 0          -- 1 = 等待 bot 回复中
);
CREATE INDEX IF NOT EXISTS idx_moment_comments_mid ON moment_comments(moment_id, ts);

CREATE TABLE IF NOT EXISTS moment_post_dedup (
    msg_key TEXT PRIMARY KEY,          -- 幂等键：bot_id:调用方消息 id
    ts INTEGER
);

CREATE TABLE IF NOT EXISTS bot_profiles (
    bot_id TEXT PRIMARY KEY,
    avatar_url TEXT,                   -- data:... 或 https://...
    banner_url TEXT,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS current_obsessions (
    bot_id TEXT,
    name TEXT,                         -- "在追《X》" / "迷上烤面包"
    effect TEXT,                       -- "今晚还想看一集；嘴里念叨那个角色"
    theme TEXT,                        -- "剧" / "烹饪" / "旅游" 用于反重复
    started_at INTEGER,
    expires_at INTEGER,
    PRIMARY KEY (bot_id, name, started_at)
);
CREATE INDEX IF NOT EXISTS idx_obsessions_bot_exp ON current_obsessions(bot_id, expires_at DESC);
"""


# 当前 schema 版本。每次新增表/列都 +1，并在 _migrate() 里补一段幂等迁移。
# 1 = 旧公开版（无版本表）；2 = 本次合并 CBL 后的完整 schema。
SCHEMA_VERSION = 2
_SCHEMA_VERSION_TABLE = "schema_version"


def _split_sql(sql: str) -> list[str]:
    """把 DDL 脚本按分号拆成单条语句（本文件的 schema 不含带分号的字符串/注释）。"""
    return [s.strip() for s in sql.split(";") if s.strip()]


def _table_names(conn) -> set:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _columns(conn, table: str) -> set:
    return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}


def _read_schema_version(conn) -> int:
    """读 schema 版本；没有版本表/没有行 → 0（旧库）。"""
    if _SCHEMA_VERSION_TABLE not in _table_names(conn):
        return 0
    row = conn.execute(
        "SELECT version FROM %s ORDER BY version DESC LIMIT 1" % _SCHEMA_VERSION_TABLE
    ).fetchone()
    try:
        return int(row[0]) if row and row[0] is not None else 0
    except (TypeError, ValueError):
        return 0


def _set_schema_version(conn, version: int) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS %s (version INTEGER NOT NULL)" % _SCHEMA_VERSION_TABLE)
    n = conn.execute("SELECT COUNT(*) FROM %s" % _SCHEMA_VERSION_TABLE).fetchone()[0]
    if n:
        conn.execute("UPDATE %s SET version=?" % _SCHEMA_VERSION_TABLE, (int(version),))
        conn.execute("DELETE FROM %s WHERE rowid NOT IN "
                     "(SELECT MIN(rowid) FROM %s)" % (_SCHEMA_VERSION_TABLE, _SCHEMA_VERSION_TABLE))
    else:
        conn.execute("INSERT INTO %s(version) VALUES(?)" % _SCHEMA_VERSION_TABLE,
                     (int(version),))
    conn.execute("PRAGMA user_version=%d" % int(version))


def _migration_needed(conn) -> bool:
    """旧库（有用户表但无版本表/版本落后/缺列）才需要备份 + 迁移。"""
    tables = _table_names(conn)
    user_tables = tables - {_SCHEMA_VERSION_TABLE, "sqlite_sequence"}
    if not user_tables:
        return False                      # 全新空库：没有旧数据要备份
    if _read_schema_version(conn) < SCHEMA_VERSION:
        return True
    # 版本号对但列被手工删掉的极端情况也补一次
    return not ({"visibility"} <= _columns(conn, "moments")
                and {"signature", "sig_updated_at", "display_name", "sig_asked_at"}
                <= _columns(conn, "bot_profiles")
                and {"image_path"} <= _columns(conn, "moment_comments")
                and "moment_post_dedup" in tables
                and "current_obsessions" in tables)


def _backup_db(conn) -> str:
    """用 SQLite 在线备份 API 把旧库完整复制到同目录 ``state.db.bak-<时间>``。

    先写唯一临时文件再 os.replace，避免留下半截备份；失败原样抛（不迁移无备份的库）。
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest_path = "%s.bak-%s" % (DB_PATH, stamp)
    if os.path.exists(dest_path):
        dest_path = "%s-%d" % (dest_path, os.getpid())
    tmp = "%s.tmp-%d" % (dest_path, os.getpid())
    try:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        dest = sqlite3.connect(tmp)
        try:
            conn.backup(dest)
            dest.commit()
        finally:
            dest.close()
        if os.name == "posix":
            with contextlib.suppress(OSError):
                os.chmod(tmp, 0o600)
        os.replace(tmp, dest_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return dest_path


def _migrate(conn) -> None:
    """在调用方已开启的事务里建表、补列；全部幂等。"""
    for stmt in _split_sql(SCHEMA):
        if stmt.upper().startswith("PRAGMA"):
            continue                      # PRAGMA 在事务外单独执行
        conn.execute(stmt)
    if "visibility" not in _columns(conn, "moments"):
        conn.execute("ALTER TABLE moments ADD COLUMN visibility TEXT DEFAULT 'public'")
    cols = _columns(conn, "bot_profiles")
    if "signature" not in cols:
        conn.execute("ALTER TABLE bot_profiles ADD COLUMN signature TEXT")
        conn.execute("ALTER TABLE bot_profiles ADD COLUMN sig_updated_at INTEGER")
    if "display_name" not in cols:
        conn.execute("ALTER TABLE bot_profiles ADD COLUMN display_name TEXT")
    # F-31：签名"上次询问时刻"，冷却按它算（模型答"不换"也计时）
    if "sig_asked_at" not in cols:
        conn.execute("ALTER TABLE bot_profiles ADD COLUMN sig_asked_at INTEGER")
    if "image_path" not in _columns(conn, "moment_comments"):
        conn.execute("ALTER TABLE moment_comments ADD COLUMN image_path TEXT")
    _set_schema_version(conn, SCHEMA_VERSION)


def init():
    """初始化 schema，幂等。

    旧库先备份（SQLite 在线备份），再在**单个事务**里建表/补列/写 schema 版本；
    任一步失败整体回滚，库保持迁移前状态。全新空库不产生备份。
    """
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        if _migration_needed(conn):
            _backup_db(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            _migrate(conn)
        except BaseException:
            conn.rollback()
            raise


@contextlib.contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ─── call_log ──────────────────────────────────────────────────
def insert_call_log(ts: int, kind: str, weight: int):
    with _connect() as c:
        c.execute("INSERT INTO call_log(ts, kind, weight) VALUES(?,?,?)", (ts, kind, weight))


def sum_call_weight_since(ts_start: int) -> int:
    with _connect() as c:
        row = c.execute("SELECT COALESCE(SUM(weight),0) AS s FROM call_log WHERE ts >= ?", (ts_start,)).fetchone()
        return int(row["s"])


# ─── ongoing_events ────────────────────────────────────────────
def get_ongoing_event(bot_id: str, now: datetime = None, since_ts: int = None):
    """取该 bot 的事件（供情绪相关功能用）。

    - 默认（不传 since_ts）：当前**未过期**的最近一条事件，需传 now。
    - 传 since_ts：返回 started_at 晚于 since_ts 的最近一条事件，**不看是否过期**。
      F-21 情绪桥用它：60 秒存续的 instant 事件在 5 分钟一次的 tick 里用"当前未过期"
      筛会永远抓不到，改用开始时刻窗口判定。
    """
    with _connect() as c:
        if since_ts is not None:
            row = c.execute(
                """SELECT event_name, effect, mood_delta, started_at, expires_at, last_mentioned_at
                   FROM ongoing_events
                   WHERE bot_id=? AND started_at > ?
                   ORDER BY started_at DESC LIMIT 1""",
                (bot_id, int(since_ts)),
            ).fetchone()
        else:
            now_ts = int(now.timestamp())
            row = c.execute(
                """SELECT event_name, effect, mood_delta, started_at, expires_at, last_mentioned_at
                   FROM ongoing_events
                   WHERE bot_id=? AND expires_at > ?
                   ORDER BY started_at DESC LIMIT 1""",
                (bot_id, now_ts),
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        # 补 name 字段：caller 多用 sporadic['name']，db 列叫 event_name，兼容兜底
        d.setdefault("name", d.get("event_name", ""))
        return d


def get_last_event_started(bot_id: str, event_name: str) -> int | None:
    """返回该 bot 最近一次某事件的 started_at unix ts，用于周期事件判断。"""
    with _connect() as c:
        row = c.execute(
            "SELECT started_at FROM ongoing_events WHERE bot_id=? AND event_name=? "
            "ORDER BY started_at DESC LIMIT 1",
            (bot_id, event_name),
        ).fetchone()
        return row[0] if row else None


def create_ongoing_event(bot_id: str, event, expires_at: datetime):
    with _connect() as c:
        c.execute(
            """INSERT OR REPLACE INTO ongoing_events
               (bot_id, event_name, effect, mood_delta, started_at, expires_at, last_mentioned_at)
               VALUES(?,?,?,?,?,?,NULL)""",
            (bot_id, event["name"], event["effect"], event.get("mood_delta", 0.0),
             int(time.time()), int(expires_at.timestamp())),
        )


def mark_event_mentioned(bot_id: str, event_name: str, started_at: int, ts: int):
    with _connect() as c:
        c.execute(
            "UPDATE ongoing_events SET last_mentioned_at=? WHERE bot_id=? AND event_name=? AND started_at=?",
            (ts, bot_id, event_name, started_at),
        )


# ─── event_dice_log ────────────────────────────────────────────
def hours_since_last_roll(bot_id: str) -> float:
    with _connect() as c:
        row = c.execute("SELECT last_roll_at FROM event_dice_log WHERE bot_id=?", (bot_id,)).fetchone()
        if not row or row["last_roll_at"] is None:
            return 999.0
        return (time.time() - row["last_roll_at"]) / 3600


def update_last_roll(bot_id: str, now: datetime):
    with _connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO event_dice_log(bot_id, last_roll_at) VALUES(?,?)",
            (bot_id, int(now.timestamp())),
        )


# ─── hobby_log ─────────────────────────────────────────────────
def hours_since_last_hobby(bot_id: str) -> float:
    with _connect() as c:
        row = c.execute("SELECT MAX(ts) AS last FROM hobby_log WHERE bot_id=?", (bot_id,)).fetchone()
        if not row or row["last"] is None:
            return 999.0
        return (time.time() - row["last"]) / 3600


def log_hobby(bot_id: str, hobby_name: str, now: datetime):
    with _connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO hobby_log(bot_id, hobby_name, ts) VALUES(?,?,?)",
            (bot_id, hobby_name, int(now.timestamp())),
        )


# ─── daily_wildcards ───────────────────────────────────────────
def save_today_wildcards(bot_id: str, date_str: str, cards: list):
    with _connect() as c:
        c.execute("DELETE FROM daily_wildcards WHERE bot_id=? AND date=?", (bot_id, date_str))
        for i, card in enumerate(cards):
            c.execute(
                "INSERT INTO daily_wildcards(bot_id, date, card_id, card, emotion, used) VALUES(?,?,?,?,?,0)",
                (bot_id, date_str, i, card.get("card", ""), card.get("emotion", "")),
            )


def get_today_wildcards(bot_id: str, date_str: str):
    with _connect() as c:
        rows = c.execute(
            "SELECT card_id, card, emotion, used FROM daily_wildcards WHERE bot_id=? AND date=?",
            (bot_id, date_str),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_wildcard_used(bot_id: str, date_str: str, card_id: int):
    with _connect() as c:
        c.execute(
            "UPDATE daily_wildcards SET used=1 WHERE bot_id=? AND date=? AND card_id=?",
            (bot_id, date_str, card_id),
        )


# ─── seen_news ─────────────────────────────────────────────────
def is_news_seen(item_hash: str) -> bool:
    with _connect() as c:
        return c.execute("SELECT 1 FROM seen_news WHERE item_hash=?", (item_hash,)).fetchone() is not None


def mark_news_seen(item_hash: str):
    with _connect() as c:
        c.execute("INSERT OR IGNORE INTO seen_news(item_hash, seen_at) VALUES(?,?)",
                  (item_hash, int(time.time())))


# ─── heartbeat_log ─────────────────────────────────────────────
def log_heartbeat(bot_id: str, ts: int, mood: float, mood_factors: list,
                  recurring: str, sporadic: str, hobby: str, wildcard: str,
                  judge_speak: bool, judge_reason: str,
                  skipped: bool, skip_reason: str, final_text: str):
    with _connect() as c:
        c.execute(
            """INSERT INTO heartbeat_log
               (bot_id, ts, mood, mood_factors, recurring, sporadic, hobby, wildcard_used,
                judge_speak, judge_reason, skipped, skip_reason, final_text)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (bot_id, ts, mood, json.dumps(mood_factors, ensure_ascii=False),
             recurring, sporadic, hobby, wildcard,
             1 if judge_speak else 0, judge_reason,
             1 if skipped else 0, skip_reason, final_text),
        )


def list_active_obsessions(bot_id: str, now_ts: int = None) -> list:
    now_ts = now_ts or int(time.time())
    with _connect() as c:
        rows = c.execute(
            """SELECT name, effect, theme, started_at, expires_at
               FROM current_obsessions
               WHERE bot_id=? AND expires_at > ?
               ORDER BY started_at DESC""",
            (bot_id, now_ts),
        ).fetchall()
        return [dict(r) for r in rows]


def add_obsession(bot_id: str, name: str, effect: str, theme: str,
                  duration_days: int = 14):
    now_ts = int(time.time())
    expires_at = now_ts + duration_days * 86400
    with _connect() as c:
        c.execute(
            """INSERT OR REPLACE INTO current_obsessions
               (bot_id, name, effect, theme, started_at, expires_at)
               VALUES(?,?,?,?,?,?)""",
            (bot_id, name, effect, theme, now_ts, expires_at),
        )


def cleanup_expired_obsessions():
    with _connect() as c:
        c.execute("DELETE FROM current_obsessions WHERE expires_at < ?", (int(time.time()),))


def last_heartbeat_ts(bot_id: str):
    with _connect() as c:
        row = c.execute(
            "SELECT MAX(ts) AS ts FROM heartbeat_log WHERE bot_id=?",
            (bot_id,),
        ).fetchone()
        return int(row["ts"]) if row and row["ts"] else None


def last_self_initiate_ts(bot_id: str, chat_id: str | None = None) -> int | None:
    """最近一次 bot 真的 dm 出去过的时间戳。

    复用 heartbeat_log.judge_speak=1 + skipped=0 反推（emit("TEXT") 那一刻写入）。
    chat_id 参数预留向后兼容，当前 heartbeat_log 没有 chat_id 维度（self-initiate 是 1 bot 1 chat）。
    """
    with _connect() as c:
        row = c.execute(
            "SELECT MAX(ts) AS ts FROM heartbeat_log "
            "WHERE bot_id=? AND judge_speak=1 AND skipped=0",
            (bot_id,),
        ).fetchone()
        return int(row["ts"]) if row and row["ts"] else None


# ─── moments ───────────────────────────────────────────────────
def insert_moment(bot_id: str, ts: int, text: str, image_path: str,
                  metadata: dict, kind: str, visibility: str = "public") -> int:
    with _connect() as c:
        cur = c.execute(
            """INSERT INTO moments(bot_id, ts, text, image_path, metadata_json, moment_kind, visibility)
               VALUES(?,?,?,?,?,?,?)""",
            (bot_id, ts, text, image_path, json.dumps(metadata, ensure_ascii=False), kind, visibility),
        )
        return cur.lastrowid


def get_moment(moment_id: int):
    with _connect() as c:
        row = c.execute("SELECT * FROM moments WHERE id=?", (moment_id,)).fetchone()
        return dict(row) if row else None


def count_moments_today(bot_id: str, date_str: str) -> int:
    """统计某 bot 今天已发的朋友圈数（用本地日期）。"""
    with _connect() as c:
        rows = c.execute(
            """SELECT COUNT(*) AS n FROM moments
               WHERE bot_id=? AND date(ts, 'unixepoch', 'localtime') = ?""",
            (bot_id, date_str),
        ).fetchone()
        return int(rows["n"])


def list_moments(bot_id: str = None, limit: int = 50, since_ts: int = 0,
                 since_id=None, visibility: str = None):
    """列朋友圈。F-15：visibility 与 (ts,id) 严格游标都在 SQL 里下推。
    - visibility 给定 → 只回该可见度；
    - since_id 给定 → 只要 (ts,id) 严格大于 (since_ts,since_id)；否则 ts >= since_ts；
    - limit=None → 不截断。"""
    where, args = [], []
    if bot_id:
        where.append("bot_id=?")
        args.append(bot_id)
    if visibility is not None:
        where.append("visibility=?")
        args.append(visibility)
    if since_id is not None:
        where.append("(ts > ? OR (ts = ? AND id > ?))")
        args += [since_ts, since_ts, since_id]
    else:
        where.append("ts >= ?")
        args.append(since_ts)
    sql = "SELECT * FROM moments WHERE " + " AND ".join(where) + " ORDER BY ts DESC, id DESC"
    if isinstance(limit, int) and not isinstance(limit, bool):
        sql += " LIMIT ?"
        args.append(limit)
    with _connect() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


_SQLITE_INT_MAX = 2 ** 63 - 1


def delete_moment_cascade(moment_id: int, owner: str) -> dict:
    """删一条 bot_id == owner 的动态，连同它的全部评论（含回复的回复）与全部点赞，同一个事务里做完。

    返回 {"status": "deleted", "comments": 删掉的评论数, "likes": 删掉的点赞数,
          "image_paths": 这条动态与其评论引用过的图片路径（库里原样，去重，不含空值）}；
    没有这条 → {"status": "missing"}；不是 owner 发的 → {"status": "forbidden"}，后两种库不变。
    只删库里的行，图片文件删不删由调用方判断。
    先 BEGIN IMMEDIATE 拿写锁再查：两个请求同时删同一条时，后拿到锁的那个查到的已是删完的库，只会得到 missing。
    """
    if moment_id > _SQLITE_INT_MAX:      # 超出 SQLite 整数范围的编号不可能存在，免得绑定参数时溢出
        return {"status": "missing"}
    with _connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT bot_id, image_path FROM moments WHERE id=?", (moment_id,)).fetchone()
        if row is None:
            return {"status": "missing"}
        if row["bot_id"] != owner:
            return {"status": "forbidden"}
        paths = [row["image_path"]] + [r["image_path"] for r in c.execute(
            "SELECT image_path FROM moment_comments WHERE moment_id=? ORDER BY id", (moment_id,))]
        comments = c.execute("DELETE FROM moment_comments WHERE moment_id=?", (moment_id,)).rowcount
        likes = c.execute("DELETE FROM moment_likes WHERE moment_id=?", (moment_id,)).rowcount
        c.execute("DELETE FROM moments WHERE id=?", (moment_id,))
    return {"status": "deleted", "comments": comments, "likes": likes,
            "image_paths": list(dict.fromkeys(p for p in paths if p))}


def moment_image_refs() -> list:
    """库里动态与评论引用的全部图片路径（原样，可能重复）：动态的 image_path、动态 metadata 里的
    image_paths（bot 多图）、评论的 image_path。删动态后判断一张图是否还被别处引用时用。"""
    with _connect() as c:
        refs = [r[0] for r in c.execute(
            "SELECT image_path FROM moments WHERE image_path IS NOT NULL AND image_path != ''")]
        refs += [r[0] for r in c.execute(
            "SELECT image_path FROM moment_comments WHERE image_path IS NOT NULL AND image_path != ''")]
        metas = [r[0] for r in c.execute(
            "SELECT metadata_json FROM moments WHERE metadata_json LIKE '%image_paths%'")]
    for meta in metas:
        try:
            paths = json.loads(meta).get("image_paths")
        except (TypeError, ValueError, AttributeError):
            continue
        if isinstance(paths, list):
            refs += [p for p in paths if isinstance(p, str) and p]
    return refs


# ─── moment_likes ──────────────────────────────────────────────
def toggle_like(moment_id: int, liker: str) -> bool:
    """加/取消赞，返回 True=已赞 False=已取消"""
    with _connect() as c:
        row = c.execute(
            "SELECT 1 FROM moment_likes WHERE moment_id=? AND liker=?",
            (moment_id, liker),
        ).fetchone()
        if row:
            c.execute("DELETE FROM moment_likes WHERE moment_id=? AND liker=?", (moment_id, liker))
            return False
        c.execute(
            "INSERT INTO moment_likes(moment_id, liker, ts) VALUES(?,?,?)",
            (moment_id, liker, int(time.time())),
        )
        return True


def like_moment(moment_id: int, liker: str) -> bool:
    """显式点赞（幂等）：已赞返回 False（未改动），本次新赞返回 True。

    与 toggle_like 的区别：重复投递同一点赞指令不会取消赞。
    """
    with _connect() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO moment_likes(moment_id, liker, ts) VALUES(?,?,?)",
            (moment_id, liker, int(time.time())),
        )
        return cur.rowcount > 0


def list_likers(moment_id: int) -> list:
    with _connect() as c:
        rows = c.execute(
            "SELECT liker, ts FROM moment_likes WHERE moment_id=? ORDER BY ts ASC",
            (moment_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def likers_bulk(moment_ids: list) -> dict:
    """一次取多条 moment 的点赞，返回 {moment_id: [likers]}"""
    if not moment_ids:
        return {}
    placeholders = ",".join("?" * len(moment_ids))
    with _connect() as c:
        rows = c.execute(
            f"SELECT moment_id, liker FROM moment_likes WHERE moment_id IN ({placeholders}) ORDER BY ts ASC",
            moment_ids,
        ).fetchall()
        out = {}
        for r in rows:
            out.setdefault(r["moment_id"], []).append(r["liker"])
        return out


def pending_likes_for_bot(bot_id: str, since_ts: int) -> list:
    """该 bot 的朋友圈在 since_ts 后被赞的记录（用于 self-initiate 注入"哥哥赞了你这几条"情境）。"""
    with _connect() as c:
        rows = c.execute(
            """SELECT ml.moment_id, ml.liker, ml.ts, m.text
               FROM moment_likes ml JOIN moments m ON ml.moment_id = m.id
               WHERE m.bot_id = ? AND ml.ts >= ? AND ml.liker != ?
               ORDER BY ml.ts ASC""",
            (bot_id, since_ts, bot_id),
        ).fetchall()
        return [dict(r) for r in rows]


# ─── moment_comments ───────────────────────────────────────────
def add_comment(moment_id: int, from_user: str, text: str,
                parent_id: int = None, pending: bool = False,
                image_path: str = None) -> int:
    with _connect() as c:
        cur = c.execute(
            """INSERT INTO moment_comments(moment_id, from_user, text, ts, parent_id, pending, image_path)
               VALUES(?,?,?,?,?,?,?)""",
            (moment_id, from_user, text, int(time.time()), parent_id, 1 if pending else 0, image_path),
        )
        return cur.lastrowid


def mark_pending(comment_id: int, pending: bool):
    with _connect() as c:
        c.execute("UPDATE moment_comments SET pending=? WHERE id=?",
                  (1 if pending else 0, comment_id))


def delete_comment(comment_id: int, only_from: str = None) -> bool:
    """删除评论。only_from 用作权限校验（仅删特定用户的评论）。"""
    with _connect() as c:
        if only_from:
            n = c.execute("DELETE FROM moment_comments WHERE id=? AND from_user=?",
                          (comment_id, only_from)).rowcount
        else:
            n = c.execute("DELETE FROM moment_comments WHERE id=?", (comment_id,)).rowcount
        return n > 0


def get_comment(comment_id: int):
    with _connect() as c:
        row = c.execute("SELECT * FROM moment_comments WHERE id=?", (comment_id,)).fetchone()
        return dict(row) if row else None


def has_bot_reply(moment_id: int, parent_id, from_user: str) -> bool:
    """该 (moment, parent, bot) 是否已存在一条回复；用于回复幂等去重。"""
    with _connect() as c:
        if parent_id is None:
            row = c.execute(
                "SELECT 1 FROM moment_comments WHERE moment_id=? AND parent_id IS NULL "
                "AND from_user=? LIMIT 1", (moment_id, from_user)).fetchone()
        else:
            row = c.execute(
                "SELECT 1 FROM moment_comments WHERE moment_id=? AND parent_id=? "
                "AND from_user=? LIMIT 1", (moment_id, parent_id, from_user)).fetchone()
        return row is not None


def claim_moment_post(msg_key: str) -> bool:
    """发圈幂等键声明：首次返回 True，重复投递返回 False。"""
    with _connect() as c:
        cur = c.execute("INSERT OR IGNORE INTO moment_post_dedup(msg_key, ts) VALUES(?,?)",
                        (msg_key, int(time.time())))
        return cur.rowcount > 0


def list_comments(moment_id: int) -> list:
    with _connect() as c:
        rows = c.execute(
            "SELECT * FROM moment_comments WHERE moment_id=? ORDER BY ts ASC",
            (moment_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def comments_bulk(moment_ids: list) -> dict:
    if not moment_ids:
        return {}
    placeholders = ",".join("?" * len(moment_ids))
    with _connect() as c:
        rows = c.execute(
            f"SELECT * FROM moment_comments WHERE moment_id IN ({placeholders}) ORDER BY ts ASC",
            moment_ids,
        ).fetchall()
        out = {}
        for r in rows:
            out.setdefault(r["moment_id"], []).append(dict(r))
        return out


# ─── bot_profiles ──────────────────────────────────────────────
def get_profile(bot_id: str) -> dict:
    with _connect() as c:
        row = c.execute(
            "SELECT avatar_url, banner_url, signature, sig_updated_at, sig_asked_at, display_name, updated_at FROM bot_profiles WHERE bot_id=?",
            (bot_id,),
        ).fetchone()
        return dict(row) if row else {"avatar_url": None, "banner_url": None,
                                       "signature": None, "sig_updated_at": 0,
                                       "sig_asked_at": 0,
                                       "display_name": None, "updated_at": 0}


def set_display_name(bot_id: str, display_name: str):
    cur = get_profile(bot_id)
    with _connect() as c:
        c.execute(
            """INSERT INTO bot_profiles(bot_id, avatar_url, banner_url, signature,
                                         sig_updated_at, display_name, updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(bot_id) DO UPDATE SET
                 display_name=excluded.display_name,
                 updated_at=excluded.updated_at""",
            (bot_id, cur.get("avatar_url"), cur.get("banner_url"),
             cur.get("signature"), cur.get("sig_updated_at"),
             display_name or None, int(time.time())),
        )


def set_signature(bot_id: str, signature: str):
    """更新 bot 个性签名（不影响 avatar/banner）"""
    cur = get_profile(bot_id)
    with _connect() as c:
        c.execute(
            """INSERT INTO bot_profiles(bot_id, avatar_url, banner_url, signature, sig_updated_at, updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(bot_id) DO UPDATE SET
                 signature=excluded.signature,
                 sig_updated_at=excluded.sig_updated_at,
                 updated_at=excluded.updated_at""",
            (bot_id, cur.get("avatar_url"), cur.get("banner_url"),
             signature, int(time.time()), int(time.time())),
        )


def touch_sig_asked(bot_id: str, ts: int):
    """记录"上次询问签名时刻"（F-31 冷却按它算，模型答"不换"也更新它）。"""
    with _connect() as c:
        c.execute("INSERT OR IGNORE INTO bot_profiles(bot_id) VALUES(?)", (bot_id,))
        c.execute("UPDATE bot_profiles SET sig_asked_at=? WHERE bot_id=?", (int(ts), bot_id))


def set_profile(bot_id: str, avatar_url: str = None, banner_url: str = None):
    """避免覆盖：若某字段为 None 则保留原值。要清空请显式传 ''。"""
    cur = get_profile(bot_id)
    if avatar_url is None:
        avatar_url = cur.get("avatar_url")
    if banner_url is None:
        banner_url = cur.get("banner_url")
    with _connect() as c:
        c.execute(
            """INSERT INTO bot_profiles(bot_id, avatar_url, banner_url, updated_at)
               VALUES(?,?,?,?)
               ON CONFLICT(bot_id) DO UPDATE SET
                 avatar_url=excluded.avatar_url,
                 banner_url=excluded.banner_url,
                 updated_at=excluded.updated_at""",
            (bot_id, avatar_url or None, banner_url or None, int(time.time())),
        )


# ─── 清理 ──────────────────────────────────────────────────────
def cleanup_older_than(days: int):
    """清理 heartbeat_log / call_log / seen_news 中超过 days 天的旧数据。
    moments 不清理（用户可能想长期保留）。
    """
    cutoff = int(time.time()) - days * 86400
    with _connect() as c:
        c.execute("DELETE FROM heartbeat_log WHERE ts < ?", (cutoff,))
        c.execute("DELETE FROM call_log WHERE ts < ?", (cutoff,))
        c.execute("DELETE FROM seen_news WHERE seen_at < ?", (cutoff,))
        c.execute("DELETE FROM hobby_log WHERE ts < ?", (cutoff,))
        # ongoing_events 清掉过期的，但每个 (bot_id, event_name) **保留最近一行**：
        # 周期事件（如 28 天的"例假来了"）靠 get_last_event_started 判定"上次什么时候来过"，
        # 把过期行全删会让周期判定退化成"首次出现"而立刻重新触发。
        c.execute(
            """DELETE FROM ongoing_events
               WHERE expires_at < ?
                 AND rowid NOT IN (
                     SELECT rowid FROM (
                         SELECT rowid,
                                ROW_NUMBER() OVER (
                                    PARTITION BY bot_id, event_name
                                    ORDER BY started_at DESC, rowid DESC) AS rn
                         FROM ongoing_events
                     ) WHERE rn = 1
                 )""",
            (int(time.time()) - 86400,),
        )


# ─── 删除 bot 时清朋友圈（INTERFACE-bot-lifecycle 3.3.4 第 4 步、3.7 第 12 条）─────────────────────
# 清除范围：11 张表各一个条件，只按 person，同一行满足几条也只算一次。导出与删除用同一份条件、在同一个写事务里做，
# 所以导出的行数必然等于删掉的行数。点赞、评论的条件要读 moments，必须排在 moments 之前删。
_OF_PERSON_MOMENTS = "moment_id IN (SELECT id FROM moments WHERE bot_id = :p)"
_PURGE_SCOPE = (
    ("moment_likes", "liker = :p OR " + _OF_PERSON_MOMENTS),
    ("moment_comments", "from_user = :p OR " + _OF_PERSON_MOMENTS),
    ("moments", "bot_id = :p"),
    ("bot_profiles", "bot_id = :p"),
    ("heartbeat_log", "bot_id = :p"),
    ("ongoing_events", "bot_id = :p"),
    ("daily_wildcards", "bot_id = :p"),
    ("event_dice_log", "bot_id = :p"),
    ("hobby_log", "bot_id = :p"),
    ("current_obsessions", "bot_id = :p"),
    # 键是 "<bot>:<消息 id>"。按字面前缀比，不用 LIKE：编号里的 "_" 在 LIKE 里是通配符（mio_b 会带上 mioxb 的键），
    # LIKE 对英文字母又不分大小写
    ("moment_post_dedup", "substr(msg_key, 1, length(:k)) = :k"),
)


def purge_person(person: str, export_path) -> dict:
    """删除 bot 时清掉这个 person 在朋友圈库里的数据，返回 ``{"exported": 行数, "deleted": 行数}``，两数相等。

    范围见 ``_PURGE_SCOPE``：它发的动态连同其下全部点赞与评论，它在别人动态下的点赞与评论，``bot_profiles`` 那一行，
    生活引擎按 bot 存的六张表，``moment_post_dedup`` 里以 ``<person>:`` 开头的键。不改表结构，不靠级联。
    做法：``BEGIN IMMEDIATE`` 拿写锁 → 读出范围内的整行 → 写导出件 ``export_path``（``{"person", "exported_at",
    "tables": {表名: [整行…]}}``，11 张表都有键，0600，同目录临时文件 + fsync + 改名）→ 每张表一条 DELETE → 提交。
    - 任何一步失败整体回滚、什么都不删，异常原样抛出。已写好的导出件留着：它只是库里现有数据的副本，重做时覆盖。
    - 重做（重新收拾、重启后续做）时这个 person 已经没有行、导出件又已在：不覆盖，上次导出的数据不能被空表冲掉。
    - 库文件不存在 → 没有任何人的数据，也不替它建库；库里缺的表按没有行算。
    """
    if not isinstance(person, str) or not person:
        raise ValueError("person 必须是非空字符串")
    export_path = os.fspath(export_path)
    params = {"p": person, "k": person + ":"}
    tables = {name: [] for name, _where in _PURGE_SCOPE}
    if not os.path.exists(DB_PATH):
        _export_unless_redo(export_path, person, tables, 0)
        return {"exported": 0, "deleted": 0}
    with _connect() as c:
        c.execute("BEGIN IMMEDIATE")
        present = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        scope = [(name, where) for name, where in _PURGE_SCOPE if name in present]
        for name, where in scope:
            tables[name] = [dict(r) for r in c.execute("SELECT * FROM %s WHERE %s" % (name, where), params)]
        exported = sum(len(rows) for rows in tables.values())
        _export_unless_redo(export_path, person, tables, exported)
        deleted = sum(c.execute("DELETE FROM %s WHERE %s" % (name, where), params).rowcount for name, where in scope)
        if deleted != exported:   # 同一事务、同一份条件不该对不上；对不上就整体回滚，不留半截
            raise RuntimeError("朋友圈清除的删除行数与导出行数对不上，已回滚")
    return {"exported": exported, "deleted": deleted}


def _export_unless_redo(path: str, person: str, tables: dict, exported: int) -> None:
    if exported == 0 and os.path.lexists(path):
        return
    doc = {"person": person, "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "tables": tables}
    data = json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8")   # 先编码：编不出来就什么文件都不建
    folder = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".moments-export-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            if hasattr(os, "fchmod"):
                with contextlib.suppress(OSError):
                    os.fchmod(f.fileno(), 0o600)   # Windows 没有/不适用
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    try:
        dir_fd = os.open(folder, os.O_RDONLY)   # Windows 不能 open 目录 → 跳过
    except OSError:
        dir_fd = None
    if dir_fd is not None:
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


if __name__ == "__main__":
    init()
    print(f"DB initialized at {DB_PATH}")
