"""Management-side CRUD: episodes, guests, transcript, chapters, annotations, consents."""
from __future__ import annotations

from . import db


def slugify(s: str) -> str:
    out = []
    for ch in s.lower().strip():
        if ch.isalnum():
            out.append(ch)
        elif ch in " -_":
            out.append("-")
    s2 = "".join(out)
    while "--" in s2:
        s2 = s2.replace("--", "-")
    return s2.strip("-") or "item"


def unique_slug(table: str, name: str) -> str:
    base = slugify(name)
    slug = base
    i = 1
    while db.q1(f"SELECT id FROM {table} WHERE slug=?", (slug,)):
        i += 1
        slug = f"{base}-{i}"
    return slug


# ---- topics / guests -------------------------------------------------------

def create_topic(name: str, description: str = "") -> int:
    return db.execute("INSERT INTO topics(slug,name,description) VALUES (?,?,?)",
                      (unique_slug("topics", name), name, description))


def create_guest(name: str, bio: str = "", email: str = "") -> int:
    return db.execute("INSERT INTO guests(slug,name,bio,email) VALUES (?,?,?,?)",
                      (unique_slug("guests", name), name, bio, email))


def attach_guest(episode_id: int, guest_id: int, role: str = "guest"):
    db.execute(
        "INSERT OR IGNORE INTO episode_guests(episode_id,guest_id,role,created_at) VALUES (?,?,?,?)",
        (episode_id, guest_id, role, db.now()))


# ---- episodes --------------------------------------------------------------

def create_episode(title: str, host_id: int, topic_id: int, summary: str = "",
                   release_policy: str = "hold", immutable_media: int = 1) -> int:
    import uuid
    slug = unique_slug("episodes", title)
    return db.execute(
        """INSERT INTO episodes
           (guid,slug,host_id,topic_id,title,summary,status,release_policy,
            immutable_media,created_at,updated_at)
           VALUES (?,?,?,?,?,?,'draft',?,?,?,?)""",
        (str(uuid.uuid4()), slug, host_id, topic_id, title, summary,
         release_policy, immutable_media, db.now(), db.now()),
    )


def update_episode(episode_id: int, **fields):
    allowed = {"title", "summary", "topic_id", "release_policy", "immutable_media"}
    sets, args = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            args.append(v)
    if not sets:
        return
    sets.append("updated_at=?")
    args += [db.now(), episode_id]
    db.execute(f"UPDATE episodes SET {', '.join(sets)} WHERE id=?", args)


# ---- transcript lines (针对当前音轨版本) ------------------------------------

def current_media_id(episode_id: int):
    return db.q1("SELECT current_media_id FROM episodes WHERE id=?",
                 (episode_id,))["current_media_id"]


def add_transcript_line(episode_id: int, speaker: str, start_ms: int, end_ms: int, text: str):
    mid = current_media_id(episode_id)  # 文字稿先行时为 NULL
    rid = db.execute(
        """INSERT INTO transcript_lines
           (episode_id,media_id,speaker,start_ms,end_ms,text,status)
           VALUES (?,?,?,?,?,?,'active')""",
        (episode_id, mid, speaker, start_ms, end_ms, text))
    from . import services
    services.rebuild_fts()
    return rid


def update_transcript_line(line_id: int, text: str | None = None,
                           start_ms: int | None = None, end_ms: int | None = None):
    row = db.q1("SELECT * FROM transcript_lines WHERE id=?", (line_id,))
    if not row:
        raise ValueError("文字稿行不存在")
    db.execute(
        "UPDATE transcript_lines SET text=COALESCE(?,text), start_ms=COALESCE(?,start_ms), "
        "end_ms=COALESCE(?,end_ms) WHERE id=?",
        (text, start_ms, end_ms, line_id))
    from . import services
    services.rebuild_fts()


def delete_transcript_line(line_id: int):
    """删除文字稿行（例如删掉一段嘉宾发言，连同授权片段一起处理由调用方编排）。"""
    db.execute("DELETE FROM transcript_fts WHERE rowid=?", (line_id,))
    db.execute("DELETE FROM transcript_lines WHERE id=?", (line_id,))


# ---- chapters / annotations ------------------------------------------------

def add_chapter(episode_id: int, start_ms: int, title: str):
    return db.execute(
        "INSERT INTO chapters(episode_id,media_id,start_ms,title,status) VALUES (?,?,?,?,'active')",
        (episode_id, current_media_id(episode_id), start_ms, title))


def add_annotation(episode_id: int, start_ms: int, end_ms: int, body: str, kind: str = "note"):
    return db.execute(
        """INSERT INTO annotations(episode_id,media_id,start_ms,end_ms,body,kind,status)
           VALUES (?,?,?,?,?,?,'active')""",
        (episode_id, current_media_id(episode_id), start_ms, end_ms, body, kind))


# ---- consent ---------------------------------------------------------------

def set_consent(episode_id: int, guest_id: int, start_ms: int, end_ms: int,
                title: str = "", status: str = "granted"):
    mid = current_media_id(episode_id)
    return db.execute(
        """INSERT INTO consent_segments
           (episode_id,guest_id,media_id,start_ms,end_ms,title,consent_status,status,updated_at)
           VALUES (?,?,?,?,?,?,?,'active',?)""",
        (episode_id, guest_id, mid, start_ms, end_ms, title, status, db.now()))


def revoke_consent(segment_id: int):
    db.execute("UPDATE consent_segments SET consent_status='revoked', updated_at=? WHERE id=?",
               (db.now(), segment_id))
    row = db.q1("SELECT episode_id FROM consent_segments WHERE id=?", (segment_id,))
    # 撤销后该嘉宾内容立即从公开索引移除（未公开/已撤销片段不能被全文搜索命中）
    from . import services
    services.rebuild_fts()
    return row["episode_id"] if row else None


# ---- business inquiries (商务表单，独立权限) --------------------------------

def add_inquiry(name: str, contact: str, body: str, topic: str = "") -> int:
    return db.execute(
        "INSERT INTO business_inquiries(name,contact,topic,body,created_at) VALUES (?,?,?,?,?)",
        (name, contact, topic, body, db.now()))


def list_inquiries():
    return [dict(r) for r in db.q(
        "SELECT * FROM business_inquiries ORDER BY id DESC")]


def mark_inquiry_handled(iid: int, handled: int = 1):
    db.execute("UPDATE business_inquiries SET handled=? WHERE id=?", (handled, iid))
