"""SQLite persistence layer: schema, connection helpers, small query utils."""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("PODCAST_DB", os.path.join(os.path.dirname(__file__), "..", "data", "podcast.db"))

_lock = threading.RLock()


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


_conn: sqlite3.Connection | None = None


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = connect()
    return _conn


@contextmanager
def tx():
    """Serialized write transaction (sqlite + threaded http server)."""
    conn = get_conn()
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


SCHEMA = """
CREATE TABLE IF NOT EXISTS hosts (
  id INTEGER PRIMARY KEY,
  slug TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  bio TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS topics (
  id INTEGER PRIMARY KEY,
  slug TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  description TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS guests (
  id INTEGER PRIMARY KEY,
  slug TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  bio TEXT DEFAULT '',
  email TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  salt TEXT NOT NULL,
  passhash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'admin'
);

CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

-- 永久单集：guid 永不变；immutable_media 决定渠道发布采用“固定媒体版本”还是“可更新永久地址”
CREATE TABLE IF NOT EXISTS episodes (
  id INTEGER PRIMARY KEY,
  guid TEXT UNIQUE NOT NULL,
  slug TEXT UNIQUE NOT NULL,
  host_id INTEGER REFERENCES hosts(id),
  topic_id INTEGER REFERENCES topics(id),
  title TEXT NOT NULL,
  summary TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',           -- draft|scheduled|published|blocked
  release_policy TEXT NOT NULL DEFAULT 'hold',    -- hold = 缺音频暂缓；transcript_first = 可先公开文字稿
  immutable_media INTEGER NOT NULL DEFAULT 1,     -- 1 固定版本发布；0 永久地址允许更新音频
  publish_at TEXT,                                -- 定时上线时间
  published_at TEXT,
  block_reason TEXT,
  current_media_id INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- 音轨修订：重新剪辑产生新版本，旧版本永远保留以便历史引用
CREATE TABLE IF NOT EXISTS media_assets (
  id INTEGER PRIMARY KEY,
  episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  version_no INTEGER NOT NULL,
  url TEXT NOT NULL,
  duration_ms INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'uploaded',          -- uploaded|transcoding|ready|failed|withdrawn
  license TEXT NOT NULL DEFAULT 'unknown',         -- owned|licensed|cc0|cc-by|unknown
  license_status TEXT NOT NULL DEFAULT 'active',  -- active|revoked|expired
  license_note TEXT DEFAULT '',
  is_current INTEGER NOT NULL DEFAULT 0,
  etag TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(episode_id, version_no)
);

-- 嘉宾参与单集
CREATE TABLE IF NOT EXISTS episode_guests (
  id INTEGER PRIMARY KEY,
  episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  guest_id INTEGER NOT NULL REFERENCES guests(id) ON DELETE CASCADE,
  role TEXT DEFAULT 'guest',
  created_at TEXT NOT NULL,
  UNIQUE(episode_id, guest_id)
);

-- 嘉宾授权片段：授权可被撤销；锚定到某个音轨版本的时间码
CREATE TABLE IF NOT EXISTS consent_segments (
  id INTEGER PRIMARY KEY,
  episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  guest_id INTEGER NOT NULL REFERENCES guests(id),
  media_id INTEGER NOT NULL REFERENCES media_assets(id),
  start_ms INTEGER,
  end_ms INTEGER,
  orig_start_ms INTEGER,
  orig_end_ms INTEGER,
  title TEXT DEFAULT '',
  consent_status TEXT NOT NULL DEFAULT 'granted',  -- granted|revoked|pending
  status TEXT NOT NULL DEFAULT 'active',           -- active|broken (重剪后无法映射→待修复)
  note TEXT DEFAULT '',
  updated_at TEXT NOT NULL
);

-- 文字稿行：按版本锚定；重剪后经映射产生新行，绝不照搬旧秒数
CREATE TABLE IF NOT EXISTS transcript_lines (
  id INTEGER PRIMARY KEY,
  episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  media_id INTEGER REFERENCES media_assets(id),  -- 文字稿先行时可空
  speaker TEXT NOT NULL DEFAULT 'host',            -- host 或 guest:<id>
  start_ms INTEGER,
  end_ms INTEGER,
  orig_start_ms INTEGER,
  orig_end_ms INTEGER,
  text TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active'            -- active|broken|hidden
);

CREATE TABLE IF NOT EXISTS chapters (
  id INTEGER PRIMARY KEY,
  episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  media_id INTEGER NOT NULL REFERENCES media_assets(id),
  start_ms INTEGER,
  orig_start_ms INTEGER,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active'            -- active|broken
);

-- 编辑注释/批注：无法映射时进入待修复
CREATE TABLE IF NOT EXISTS annotations (
  id INTEGER PRIMARY KEY,
  episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  media_id INTEGER NOT NULL REFERENCES media_assets(id),
  start_ms INTEGER,
  end_ms INTEGER,
  orig_start_ms INTEGER,
  orig_end_ms INTEGER,
  body TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'note',
  status TEXT NOT NULL DEFAULT 'active'            -- active|broken
);

-- 重剪辑辑映射：旧时间线 -> 新时间线
CREATE TABLE IF NOT EXISTS edit_maps (
  id INTEGER PRIMARY KEY,
  episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  from_media_id INTEGER NOT NULL REFERENCES media_assets(id),
  to_media_id INTEGER NOT NULL REFERENCES media_assets(id),
  note TEXT DEFAULT '',
  applied_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS edit_segments (
  id INTEGER PRIMARY KEY,
  map_id INTEGER NOT NULL REFERENCES edit_maps(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,           -- keep|cut|replace
  src_start INTEGER NOT NULL,
  src_end INTEGER NOT NULL,
  dst_start INTEGER,            -- cut 为空
  dst_end INTEGER
);

-- 发布渠道（RSS feed 等）；渠道看到的是冻结的“实际渠道版本”
CREATE TABLE IF NOT EXISTS channels (
  id INTEGER PRIMARY KEY,
  slug TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'rss',
  etag TEXT,
  last_build TEXT
);

CREATE TABLE IF NOT EXISTS channel_episodes (
  id INTEGER PRIMARY KEY,
  channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
  episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  media_id INTEGER,                       -- 冻结的音轨版本（可空=纯文字稿条目）
  enclosure_url TEXT,
  rss_guid TEXT NOT NULL,
  media_mode TEXT NOT NULL,               -- frozen|current
  published_at TEXT NOT NULL,
  refreshed_at TEXT NOT NULL,
  UNIQUE(channel_id, episode_id)
);

-- 分阶段流水线：转码 / 全文索引 / RSS 发布
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  stage TEXT NOT NULL,                     -- transcode|index|rss
  state TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|failed|skipped|blocked
  detail TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS business_inquiries (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  contact TEXT NOT NULL,
  topic TEXT DEFAULT '',
  body TEXT NOT NULL,
  handled INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

-- 播放器回调审计：旧版本回调被拒绝留痕
CREATE TABLE IF NOT EXISTS player_events (
  id INTEGER PRIMARY KEY,
  episode_id INTEGER REFERENCES episodes(id) ON DELETE CASCADE,
  media_id INTEGER,
  position_ms INTEGER,
  accepted INTEGER NOT NULL DEFAULT 0,
  reject_reason TEXT DEFAULT '',
  created_at TEXT NOT NULL
);

-- FTS5 全文索引（仅写入“可公开”的文字稿行；未发布/未授权片段永不进入）
-- trigram 分词器支持中文子串检索（中文无空格，unicode61 无法切分）
CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
  text,
  episode_id UNINDEXED,
  line_id UNINDEXED,
  tokenize = 'trigram'
);
"""


def init_db(path: str | None = None):
    global _conn
    os.makedirs(os.path.dirname(os.path.abspath(path or DB_PATH)), exist_ok=True)
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.close()
    _conn = connect(path)
    return _conn


# ---- tiny query helpers ----------------------------------------------------

def q(sql: str, args=()):
    return get_conn().execute(sql, args).fetchall()


def q1(sql: str, args=()):
    return get_conn().execute(sql, args).fetchone()


def execute(sql: str, args=()):
    with tx() as conn:
        cur = conn.execute(sql, args)
        return cur.lastrowid
