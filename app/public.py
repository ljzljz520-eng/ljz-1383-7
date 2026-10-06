"""Public archive read models: browse by topic, episode page with channel version + text fallback."""
from __future__ import annotations

from . import db


def topics_overview():
    rows = db.q(
        """
        SELECT t.*, COUNT(e.id) AS episode_count
        FROM topics t LEFT JOIN episodes e
          ON e.topic_id=t.id AND e.status='published'
        GROUP BY t.id ORDER BY t.name
        """)
    return [dict(r) for r in rows]


def public_episodes_by_topic(topic_slug: str | None = None):
    sql = """
        SELECT e.*, t.name AS topic_name, t.slug AS topic_slug,
               m.url AS audio_url, m.version_no, m.duration_ms
        FROM episodes e
        JOIN topics t ON t.id=e.topic_id
        LEFT JOIN media_assets m ON m.id=e.current_media_id
        LEFT JOIN channel_episodes ce ON ce.episode_id=e.id
        WHERE e.status='published'
      """
    args: list = []
    if topic_slug:
        sql += " AND t.slug=?"
        args.append(topic_slug)
    sql += " ORDER BY e.published_at DESC"
    out = []
    for r in db.q(sql, args):
        d = dict(r)
        # 已发布但许可撤销 → 公开页不提供音频，只给文字（若策略允许）
        m = db.q1("SELECT license_status, state FROM media_assets WHERE id=?",
                  (r["current_media_id"],)) if r["current_media_id"] else None
        if not m or m["license_status"] != "active" or m["state"] != "ready":
            d["audio_url"] = None
            d["text_only"] = True
        else:
            d["text_only"] = False
        out.append(d)
    return out


def episode_public(slug: str):
    """公开单集页：呈现“实际渠道版本”的音轨（冻结版本），无音频时给文字回退。"""
    e = db.q1("SELECT * FROM episodes WHERE slug=?", (slug,))
    if e is None:
        return None
    e = dict(e)
    if e["status"] != "published":
        return {"forbidden": True, "episode": e}

    ch = db.q1("SELECT * FROM channels WHERE slug='main'")
    ce = db.q1("SELECT * FROM channel_episodes WHERE channel_id=? AND episode_id=?",
               (ch["id"], e["id"])) if ch else None

    audio = None
    channel_version = None
    if ce and ce["media_id"]:
        cm = db.q1("SELECT * FROM media_assets WHERE id=?", (ce["media_id"],))
        if cm and cm["license_status"] == "active" and cm["state"] == "ready":
            url = ce["enclosure_url"]
            if ce["media_mode"] == "frozen":
                url = f"{url}?v={cm['version_no']}"
            audio = {"url": url, "version_no": cm["version_no"],
                     "duration_ms": cm["duration_ms"], "etag": cm["etag"],
                     "media_id": cm["id"], "mode": ce["media_mode"]}
            channel_version = cm["version_no"]
    elif e["current_media_id"]:
        # 尚未同步到渠道快照时（理论上发布即快照），以当前版本兜底并标注
        cm = db.q1("SELECT * FROM media_assets WHERE id=?", (e["current_media_id"],))
        if cm and cm["license_status"] == "active" and cm["state"] == "ready":
            audio = {"url": cm["url"], "version_no": cm["version_no"],
                     "duration_ms": cm["duration_ms"], "etag": cm["etag"],
                     "media_id": cm["id"], "mode": "unsnapshotted"}
            channel_version = cm["version_no"]

    # 文字稿回退：即便无音频（文字稿先行 / 许可撤下），也读取已授权 active 行。
    # 文字稿先行时 current_media_id 为 NULL，文字行的 media_id 也是 NULL。
    media_id_for_text = (ce["media_id"] if ce and ce["media_id"] else e["current_media_id"])
    text_fallback_allowed = audio is not None or e["release_policy"] == "transcript_first"
    lines = []
    if media_id_for_text:
        lines = [dict(r) for r in db.q(
            """
            SELECT l.* FROM transcript_lines l
            WHERE l.media_id=? AND l.status='active'
              AND ( l.speaker='host'
                    OR EXISTS (
                        SELECT 1 FROM consent_segments c
                        WHERE c.episode_id=l.episode_id
                          AND c.guest_id=CAST(substr(l.speaker,7) AS INTEGER)
                          AND c.media_id=l.media_id
                          AND c.consent_status='granted' AND c.status='active'
                    ) )
            ORDER BY l.start_ms
            """, (media_id_for_text,))]
    elif not audio and text_fallback_allowed:
        # 无渠道音轨：文字稿先行（current_media_id 为 NULL）或许可撤下，
        # active 且授权的文字稿仍按策略公开
        lines = [dict(r) for r in db.q(
            """
            SELECT l.* FROM transcript_lines l
            JOIN episodes ep ON ep.id=l.episode_id
            WHERE l.episode_id=? AND l.status='active'
              AND l.media_id IS ep.current_media_id
              AND ( l.speaker='host'
                    OR EXISTS (
                        SELECT 1 FROM consent_segments c
                        WHERE c.episode_id=l.episode_id
                          AND c.guest_id=CAST(substr(l.speaker,7) AS INTEGER)
                          AND c.media_id=l.media_id
                          AND c.consent_status='granted' AND c.status='active'
                    ) )
            ORDER BY l.start_ms
            """, (e["id"],))]

    chapters = []
    if media_id_for_text:
        chapters = [dict(r) for r in db.q(
            "SELECT * FROM chapters WHERE media_id=? AND status='active' ORDER BY start_ms",
            (media_id_for_text,))]

    guests = [dict(r) for r in db.q(
        """SELECT g.* FROM episode_guests eg JOIN guests g ON g.id=eg.guest_id
           WHERE eg.episode_id=?""", (e["id"],))]
    topic = db.q1("SELECT * FROM topics WHERE id=?", (e["topic_id"],))

    return {
        "episode": e, "audio": audio, "lines": lines, "chapters": chapters,
        "guests": guests, "topic": dict(topic) if topic else None,
        "channel_version": channel_version,
        "text_only": audio is None and bool(lines),
        "transcript_first_note": audio is None and bool(lines),
    }
