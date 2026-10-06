"""Domain services: episodes, media revisions, re-edit remapping, FTS indexing."""
from __future__ import annotations

import os

from . import db
from .mapping import normalize, new_duration, map_point, map_interval, InvalidEditMap

# ---------------------------------------------------------------------------
# FTS helpers —— 索引是“发布状态的投影”，未发布/未授权内容绝不进入索引
# ---------------------------------------------------------------------------

def _segment_consent_ok(conn, episode_id, guest_id) -> bool:
    row = conn.execute(
        "SELECT consent_status, status FROM consent_segments "
        "WHERE episode_id=? AND guest_id=? ORDER BY id DESC LIMIT 1",
        (episode_id, guest_id),
    ).fetchone()
    if row is None:
        return True  # 主持人自己的话无需嘉宾授权
    return row["consent_status"] == "granted" and row["status"] != "broken"


def _indexable_lines(conn):
    """只有 已发布 + active + 嘉宾已授权 的行才可被全文索引。"""
    return conn.execute(
        """
        SELECT l.id AS line_id, l.episode_id, l.text
        FROM transcript_lines l
        JOIN episodes e ON e.id = l.episode_id
        WHERE l.status='active'
          AND l.media_id = e.current_media_id
          AND e.status='published'
          AND ( l.speaker='host'
                OR EXISTS (
                    -- 授权是“嘉宾×单集”级别状态；逐版本映射产生的 broken 授权
                    -- 没有有效时间码，不能让对应嘉宾内容公开
                    SELECT 1 FROM consent_segments c
                    WHERE c.episode_id=l.episode_id
                      AND c.guest_id = CAST(substr(l.speaker,7) AS INTEGER)
                      AND c.media_id = l.media_id
                      AND c.consent_status='granted' AND c.status='active'
                ) )
        """
    ).fetchall()


def rebuild_fts(conn=None):
    own = conn is None
    conn = conn or db.get_conn()
    conn.execute("DELETE FROM transcript_fts")
    for r in _indexable_lines(conn):
        conn.execute(
            "INSERT INTO transcript_fts(rowid, text, episode_id, line_id) VALUES (?,?,?,?)",
            (r["line_id"], r["text"], r["episode_id"], r["line_id"]),
        )
    return True


def search_public(query: str, limit: int = 50):
    """公开全文搜索：FTS 本身只装了可公开的行；再加一层 published 过滤。"""
    q = (query or "").strip()
    if not q:
        return []
    rows = db.q(
        """
        SELECT f.episode_id, f.line_id, f.text, e.title, e.slug AS episode_slug,
               l.start_ms, l.speaker
        FROM transcript_fts f
        JOIN transcript_lines l ON l.id = f.line_id
        JOIN episodes e ON e.id = f.episode_id
        WHERE transcript_fts MATCH ? AND e.status='published' AND l.status='active'
        ORDER BY rank LIMIT ?
        """,
        (q, limit),
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Media revisions
# ---------------------------------------------------------------------------

def add_media(episode_id: int, url: str, duration_ms: int,
              license: str = "unknown", make_current: bool = True) -> int:
    with db.tx() as conn:
        ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        if ep is None:
            raise ValueError("单集不存在")
        v = conn.execute(
            "SELECT COALESCE(MAX(version_no),0)+1 AS v FROM media_assets WHERE episode_id=?",
            (episode_id,),
        ).fetchone()["v"]
        mid = conn.execute(
            """INSERT INTO media_assets
               (episode_id,version_no,url,duration_ms,state,license,license_status,
                is_current,etag,created_at)
               VALUES (?,?,?,?, 'ready', ?, 'active', ?, ?, ?)""",
            (episode_id, v, url, duration_ms, license,
             1 if make_current else 0, _etag(v), db.now()),
        ).lastrowid
        if make_current:
            conn.execute(
                "UPDATE media_assets SET is_current=0 WHERE episode_id=? AND id<>?",
                (episode_id, mid),
            )
            conn.execute("UPDATE episodes SET current_media_id=?, updated_at=? WHERE id=?",
                         (mid, db.now(), episode_id))
        # 分阶段流水线：新版本先转码，再索引，再 RSS
        for stage in ("transcode", "index", "rss"):
            conn.execute(
                "INSERT INTO jobs(episode_id,stage,state,detail,created_at,updated_at) "
                "VALUES (?,?, 'pending','',?,?)",
                (episode_id, stage, db.now(), db.now()),
            )
        return mid


def _etag(v: int) -> str:
    return f'v{v}-{db.now()}'


def set_license(media_id: int, license: str, status: str = "active", note: str = ""):
    with db.tx() as conn:
        conn.execute(
            "UPDATE media_assets SET license=?, license_status=?, license_note=? WHERE id=?",
            (license, status, note, media_id),
        )


def revoke_license(media_id: int, reason: str = "license revoked"):
    """撤销音轨许可。已发布单集立即撤下音频（保留文字稿与否按策略）。"""
    with db.tx() as conn:
        conn.execute(
            "UPDATE media_assets SET license_status='revoked', license_note=? WHERE id=?",
            (reason, media_id),
        )
        m = conn.execute("SELECT * FROM media_assets WHERE id=?", (media_id,)).fetchone()
        if m:
            conn.execute(
                "UPDATE episodes SET status='blocked', block_reason=?, updated_at=? WHERE id=?",
                (f"audio license revoked: {reason}", db.now(), m["episode_id"]),
            )
        # 冻结在该音轨上的渠道条目撤回 enclosure（文字回退由策略决定）
        conn.execute(
            "UPDATE channel_episodes SET media_id=NULL, enclosure_url=NULL, refreshed_at=? "
            "WHERE media_id=?",
            (db.now(), media_id),
        )
        rebuild_fts(conn)


# ---------------------------------------------------------------------------
# Re-edit: 新音轨 + 编辑映射 + 时间码重定位
# ---------------------------------------------------------------------------

def reedit_episode(episode_id: int, new_url: str, segments: list[dict],
                   license: str = "owned", note: str = "") -> dict:
    """执行一次重新剪辑。

    1) 生成新音轨版本；2) 保存编辑映射；3) 字幕/章节/授权片段/注释全部
    经过映射重定位到新时间线；无法映射的进入待修复（保留旧值、清空当前时间码）。
    """
    with db.tx() as conn:
        ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        if ep is None:
            raise ValueError("单集不存在")
        old = conn.execute(
            "SELECT * FROM media_assets WHERE id=?", (ep["current_media_id"],)
        ).fetchone()
        if old is None:
            raise ValueError("单集还没有音轨，无法重剪；请直接新增音轨")
        try:
            segs = normalize(segments, old["duration_ms"])
        except InvalidEditMap as exc:
            raise ValueError(f"编辑映射无效: {exc}")
        dur = new_duration(segs)

        v = conn.execute(
            "SELECT COALESCE(MAX(version_no),0)+1 AS v FROM media_assets WHERE episode_id=?",
            (episode_id,),
        ).fetchone()["v"]
        new_id = conn.execute(
            """INSERT INTO media_assets
               (episode_id,version_no,url,duration_ms,state,license,license_status,
                is_current,etag,created_at)
               VALUES (?,?,?,?, 'ready', ?, 'active', 1, ?, ?)""",
            (episode_id, v, new_url, dur, license, _etag(v), db.now()),
        ).lastrowid
        conn.execute("UPDATE media_assets SET is_current=0 WHERE id=?", (old["id"],))
        conn.execute("UPDATE episodes SET current_media_id=?, updated_at=? WHERE id=?",
                     (new_id, db.now(), episode_id))

        map_id = conn.execute(
            "INSERT INTO edit_maps(episode_id,from_media_id,to_media_id,note,applied_at,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (episode_id, old["id"], new_id, note, db.now(), db.now()),
        ).lastrowid
        for s in segs:
            conn.execute(
                "INSERT INTO edit_segments(map_id,kind,src_start,src_end,dst_start,dst_end) "
                "VALUES (?,?,?,?,?,?)",
                (map_id, s.kind, s.src_start, s.src_end, s.dst_start, s.dst_end),
            )

        result = {"new_media_id": new_id, "map_id": map_id,
                  "old_duration": old["duration_ms"], "new_duration": dur}

        # --- 文字稿行 ---
        moved = broken = 0
        for line in conn.execute(
            "SELECT * FROM transcript_lines WHERE media_id=? AND status='active'",
            (old["id"],),
        ).fetchall():
            iv = map_interval(line["start_ms"], line["end_ms"], segs)
            if iv:
                conn.execute(
                    """INSERT INTO transcript_lines
                       (episode_id,media_id,speaker,start_ms,end_ms,
                        orig_start_ms,orig_end_ms,text,status)
                       VALUES (?,?,?,?,?,?,?,?,'active')""",
                    (episode_id, new_id, line["speaker"], iv[0], iv[1],
                     line["start_ms"], line["end_ms"], line["text"]),
                )
                moved += 1
            else:
                conn.execute(
                    """INSERT INTO transcript_lines
                       (episode_id,media_id,speaker,start_ms,end_ms,
                        orig_start_ms,orig_end_ms,text,status)
                       VALUES (?,?,?,NULL,NULL,?,?,?, 'broken')""",
                    (episode_id, new_id, line["speaker"],
                     line["start_ms"], line["end_ms"], line["text"]),
                )
                broken += 1
        result["transcript"] = {"moved": moved, "broken": broken}

        # --- 章节 ---
        moved = broken = 0
        for ch in conn.execute(
            "SELECT * FROM chapters WHERE media_id=? AND status='active'", (old["id"],)
        ).fetchall():
            pt = map_point(ch["start_ms"], segs)
            if pt is not None:
                conn.execute(
                    "INSERT INTO chapters(episode_id,media_id,start_ms,orig_start_ms,title,status) "
                    "VALUES (?,?,?,?,?, 'active')",
                    (episode_id, new_id, pt, ch["start_ms"], ch["title"]),
                )
                moved += 1
            else:
                conn.execute(
                    "INSERT INTO chapters(episode_id,media_id,start_ms,orig_start_ms,title,status) "
                    "VALUES (?,?,NULL,?,?, 'broken')",
                    (episode_id, new_id, ch["start_ms"], ch["title"]),
                )
                broken += 1
        result["chapters"] = {"moved": moved, "broken": broken}

        # --- 嘉宾授权片段 ---
        moved = broken = 0
        for c in conn.execute(
            "SELECT * FROM consent_segments WHERE media_id=? AND status='active'",
            (old["id"],),
        ).fetchall():
            iv = map_interval(c["start_ms"], c["end_ms"], segs)
            if iv:
                conn.execute(
                    """INSERT INTO consent_segments
                       (episode_id,guest_id,media_id,start_ms,end_ms,
                        orig_start_ms,orig_end_ms,title,consent_status,status,note,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?, 'active', ?, ?)""",
                    (episode_id, c["guest_id"], new_id, iv[0], iv[1],
                     c["start_ms"], c["end_ms"], c["title"], c["consent_status"],
                     c["note"], db.now()),
                )
                moved += 1
            else:
                # 被删/被换区间上的授权片段无法重定位 → 待修复（不复制旧授权到新秒数）
                conn.execute(
                    """INSERT INTO consent_segments
                       (episode_id,guest_id,media_id,start_ms,end_ms,
                        orig_start_ms,orig_end_ms,title,consent_status,status,note,updated_at)
                       VALUES (?,?,?,NULL,NULL,?,?,?,?,'broken',?,?)""",
                    (episode_id, c["guest_id"], new_id,
                     c["start_ms"], c["end_ms"], c["title"], c["consent_status"],
                     "无法映射，授权片段待人工修复", db.now()),
                )
                broken += 1
        result["consents"] = {"moved": moved, "broken": broken}

        # --- 注释 ---
        moved = broken = 0
        for a in conn.execute(
            "SELECT * FROM annotations WHERE media_id=? AND status='active'", (old["id"],)
        ).fetchall():
            iv = map_interval(a["start_ms"], a["end_ms"], segs)
            if iv:
                conn.execute(
                    """INSERT INTO annotations
                       (episode_id,media_id,start_ms,end_ms,orig_start_ms,orig_end_ms,
                        body,kind,status)
                       VALUES (?,?,?,?,?,?,?,?, 'active')""",
                    (episode_id, new_id, iv[0], iv[1], a["start_ms"], a["end_ms"],
                     a["body"], a["kind"]),
                )
                moved += 1
            else:
                conn.execute(
                    """INSERT INTO annotations
                       (episode_id,media_id,start_ms,end_ms,orig_start_ms,orig_end_ms,
                        body,kind,status)
                       VALUES (?,?,NULL,NULL,?,?,?,?, 'broken')""",
                    (episode_id, new_id,
                     a["start_ms"], a["end_ms"], a["body"], a["kind"]),
                )
                broken += 1
        result["annotations"] = {"moved": moved, "broken": broken}

        # 新版本进入分阶段流水线
        for stage in ("transcode", "index", "rss"):
            conn.execute(
                "INSERT INTO jobs(episode_id,stage,state,detail,created_at,updated_at) "
                "VALUES (?,?, 'pending','',?,?)",
                (episode_id, stage, db.now(), db.now()),
            )

        # 重新发布处理（见 publish.refresh_published_media）
        from . import publish
        result["refresh"] = publish.refresh_published_media(conn, episode_id, new_id)
        rebuild_fts(conn)
        return result


def repair_queue(episode_id: int | None = None):
    """待修复清单：所有 broken 的注释/章节/授权片段/字幕。"""
    out = {"transcript": [], "chapters": [], "consents": [], "annotations": []}
    where, args = "", []
    if episode_id is not None:
        where, args = " WHERE episode_id=?", [episode_id]
    for table, key in (
        ("transcript_lines", "transcript"),
        ("chapters", "chapters"),
        ("consent_segments", "consents"),
        ("annotations", "annotations"),
    ):
        out[key] = [dict(r) for r in db.q(
            f"SELECT * FROM {table} WHERE status='broken'" +
            (" AND episode_id=?" if episode_id is not None else ""), args)]
    return out


def fix_item(kind: str, item_id: int, start_ms: int, end_ms: int | None = None):
    """人工修复待修复项：重新赋予当前版本上的时间码。"""
    table = {"transcript": "transcript_lines", "chapters": "chapters",
             "consents": "consent_segments", "annotations": "annotations"}[kind]
    with db.tx() as conn:
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
        if row is None or row["status"] != "broken":
            raise ValueError("待修复项不存在或已修复")
        if kind == "chapters":
            conn.execute(f"UPDATE {table} SET start_ms=?, status='active' WHERE id=?",
                         (start_ms, item_id))
        else:
            conn.execute(f"UPDATE {table} SET start_ms=?, end_ms=?, status='active' WHERE id=?",
                         (start_ms, end_ms if end_ms is not None else start_ms, item_id))
        if kind == "consents":
            conn.execute(f"UPDATE {table} SET note='' WHERE id=?", (item_id,))
        rebuild_fts(conn)
        return True
