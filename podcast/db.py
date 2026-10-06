"""数据库层：模式定义与连接助手。全部使用 Python 标准库 sqlite3。"""
import os, sqlite3, time

DB_PATH = os.environ.get("PODCAST_DB", os.path.join(os.path.dirname(__file__), "..", "podcast.db"))

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS topics(
  id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, name TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS policies(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL,
  allow_transcript_only INTEGER NOT NULL DEFAULT 0,  -- 缺音频时：1=公开文字稿 0=暂缓发布
  description TEXT DEFAULT '');

CREATE TABLE IF NOT EXISTS episodes(
  id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
  summary TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'draft',  -- draft|scheduled|published|held
  policy_code TEXT NOT NULL DEFAULT 'hold',
  publish_at REAL,            -- 定时上线时间戳
  created_at REAL NOT NULL, updated_at REAL NOT NULL);

CREATE TABLE IF NOT EXISTS episode_topics(
  episode_id INTEGER NOT NULL REFERENCES episodes(id),
  topic_id INTEGER NOT NULL REFERENCES topics(id),
  PRIMARY KEY(episode_id, topic_id));

CREATE TABLE IF NOT EXISTS guests(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, bio TEXT DEFAULT '', contact_email TEXT DEFAULT '');

CREATE TABLE IF NOT EXISTS episode_guests(
  episode_id INTEGER NOT NULL REFERENCES episodes(id),
  guest_id INTEGER NOT NULL REFERENCES guests(id),
  role TEXT DEFAULT 'guest', PRIMARY KEY(episode_id, guest_id));

-- 音轨修订：每次重新剪辑/换音轨产生一个新版本，旧版本保留用于审计与历史引用
CREATE TABLE IF NOT EXISTS audio_revisions(
  id INTEGER PRIMARY KEY, episode_id INTEGER NOT NULL REFERENCES episodes(id),
  version INTEGER NOT NULL, media_url TEXT, duration_ms INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'missing',   -- missing|transcoding|ready|failed
  supersedes INTEGER, created_at REAL NOT NULL,
  UNIQUE(episode_id, version));

-- 编辑映射：from_revision -> to_revision 的时间轴操作（delete/insert），单位毫秒
CREATE TABLE IF NOT EXISTS edit_maps(
  id INTEGER PRIMARY KEY, episode_id INTEGER NOT NULL REFERENCES episodes(id),
  from_revision INTEGER NOT NULL, to_revision INTEGER NOT NULL,
  ops_json TEXT NOT NULL, created_at REAL NOT NULL);

-- 注释：字幕/章节/嘉宾授权片段。按修订版本复制（copy-on-write），永不原地改旧秒数
CREATE TABLE IF NOT EXISTS annotations(
  id INTEGER PRIMARY KEY, episode_id INTEGER NOT NULL REFERENCES episodes(id),
  revision_id INTEGER NOT NULL REFERENCES audio_revisions(id),
  type TEXT NOT NULL,                       -- subtitle|chapter|guest_clip
  start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL,
  text TEXT DEFAULT '', guest_id INTEGER, license_id INTEGER,
  status TEXT NOT NULL DEFAULT 'active',    -- active|pending_repair|dropped
  reason TEXT DEFAULT '',                   -- source_deleted|partial_overlap|unmappable
  orig_start_ms INTEGER, orig_end_ms INTEGER,  -- 待修复时保留旧时间码供人工参考
  created_at REAL NOT NULL);

-- 授权：嘉宾对单集的许可，可撤销
CREATE TABLE IF NOT EXISTS licenses(
  id INTEGER PRIMARY KEY, guest_id INTEGER NOT NULL REFERENCES guests(id),
  episode_id INTEGER NOT NULL REFERENCES episodes(id),
  scope TEXT NOT NULL DEFAULT 'full',       -- full|segments
  status TEXT NOT NULL DEFAULT 'granted',   -- granted|revoked|pending
  note TEXT DEFAULT '', updated_at REAL NOT NULL);

CREATE TABLE IF NOT EXISTS channels(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  generation INTEGER NOT NULL DEFAULT 0);   -- 失效信号：发布/撤下/换版本时递增

-- 渠道实际播出的版本（同一单集不同渠道可播不同剪辑）
CREATE TABLE IF NOT EXISTS channel_versions(
  id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL REFERENCES channels(id),
  episode_id INTEGER NOT NULL REFERENCES episodes(id),
  revision_id INTEGER REFERENCES audio_revisions(id),  -- NULL = 纯文字稿上线
  transcript_only INTEGER NOT NULL DEFAULT 0,
  published_at REAL NOT NULL,
  UNIQUE(channel_id, episode_id));

CREATE TABLE IF NOT EXISTS publish_schedules(
  id INTEGER PRIMARY KEY, episode_id INTEGER NOT NULL REFERENCES episodes(id),
  run_at REAL NOT NULL, channel_code TEXT NOT NULL DEFAULT 'public',
  status TEXT NOT NULL DEFAULT 'pending',   -- pending|done|blocked|canceled
  blocked_reason TEXT DEFAULT '', created_at REAL NOT NULL);

-- 分阶段流水线事件：transcode -> index -> rss
CREATE TABLE IF NOT EXISTS publish_events(
  id INTEGER PRIMARY KEY, episode_id INTEGER, stage TEXT NOT NULL,
  status TEXT NOT NULL, detail TEXT DEFAULT '', created_at REAL NOT NULL);

-- RSS 缓存：带水位线，任何发布面变化都会使缓存过期
CREATE TABLE IF NOT EXISTS rss_cache(
  channel_id INTEGER PRIMARY KEY, xml TEXT NOT NULL,
  built_at REAL NOT NULL, watermark TEXT NOT NULL);

-- 播放器回调：旧版本回调到达时记录但不应用
CREATE TABLE IF NOT EXISTS player_events(
  id INTEGER PRIMARY KEY, episode_id INTEGER NOT NULL, revision_id INTEGER NOT NULL,
  event TEXT NOT NULL, position_ms INTEGER DEFAULT 0, client_id TEXT DEFAULT '',
  applied INTEGER NOT NULL DEFAULT 0, note TEXT DEFAULT '', received_at REAL NOT NULL);

-- 商务表单：与公开档案权限分离，仅管理员可读
CREATE TABLE IF NOT EXISTS inquiries(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL,
  message TEXT NOT NULL, created_at REAL NOT NULL);

-- 全文索引只装“已发布且当前有效”的注释文本；trigram 分词支持中文子串检索
CREATE VIRTUAL TABLE IF NOT EXISTS seg_fts USING fts5(
  text, episode_id UNINDEXED, annotation_id UNINDEXED, type UNINDEXED,
  tokenize='trigram');
"""

def connect(path=None):
    db = sqlite3.connect(path or DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db

def init(db):
    db.executescript(SCHEMA)
    db.commit()

def now():
    return time.time()

def fresh(path=None):
    """测试用：全新数据库。"""
    p = path or ":memory:"
    db = connect(p)
    init(db)
    return db
