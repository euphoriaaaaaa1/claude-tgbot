"""SQLite 状态存储（WAL 模式，支持并发读写）。

所有表的 schema 在 init() 里。其他模块通过这里的函数访问数据库，
不要在外部直接拼 SQL。
"""
import os
import sqlite3
import time
import json
import contextlib
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.db")

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


def init():
    """初始化 schema，幂等。包含已存在表的列迁移。"""
    with _connect() as conn:
        conn.executescript(SCHEMA)
        # 旧库 moments 表可能没 visibility 列，补一下
        cols = {r[1] for r in conn.execute("PRAGMA table_info(moments)").fetchall()}
        if "visibility" not in cols:
            conn.execute("ALTER TABLE moments ADD COLUMN visibility TEXT DEFAULT 'public'")
        # bot_profiles 加 signature / display_name 字段
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bot_profiles)").fetchall()}
        if "signature" not in cols:
            conn.execute("ALTER TABLE bot_profiles ADD COLUMN signature TEXT")
            conn.execute("ALTER TABLE bot_profiles ADD COLUMN sig_updated_at INTEGER")
        if "display_name" not in cols:
            conn.execute("ALTER TABLE bot_profiles ADD COLUMN display_name TEXT")
        # moment_comments 加 image_path（bot 评论可附图）
        cols = {r[1] for r in conn.execute("PRAGMA table_info(moment_comments)").fetchall()}
        if "image_path" not in cols:
            conn.execute("ALTER TABLE moment_comments ADD COLUMN image_path TEXT")


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
def get_ongoing_event(bot_id: str, now: datetime):
    now_ts = int(now.timestamp())
    with _connect() as c:
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


def list_moments(bot_id: str = None, limit: int = 50, since_ts: int = 0):
    with _connect() as c:
        if bot_id:
            rows = c.execute(
                """SELECT * FROM moments WHERE bot_id=? AND ts >= ? ORDER BY ts DESC LIMIT ?""",
                (bot_id, since_ts, limit),
            ).fetchall()
        else:
            rows = c.execute(
                """SELECT * FROM moments WHERE ts >= ? ORDER BY ts DESC LIMIT ?""",
                (since_ts, limit),
            ).fetchall()
        return [dict(r) for r in rows]


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
            "SELECT avatar_url, banner_url, signature, sig_updated_at, display_name, updated_at FROM bot_profiles WHERE bot_id=?",
            (bot_id,),
        ).fetchone()
        return dict(row) if row else {"avatar_url": None, "banner_url": None,
                                       "signature": None, "sig_updated_at": 0,
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
        # ongoing_events 也清掉过期的
        c.execute("DELETE FROM ongoing_events WHERE expires_at < ?", (int(time.time()) - 86400,))


if __name__ == "__main__":
    init()
    print(f"DB initialized at {DB_PATH}")
