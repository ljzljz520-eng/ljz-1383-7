"""Publishing: policy gate, scheduled release, channel versions, RSS cache, player callbacks."""
from __future__ import annotations

from . import db
from . import services


class PublishBlocked(Exception):
    pass


# ---------------------------------------------------------------------------
# 发布前闸门
# ---------------------------------------------------------------------------

def pre_publish_checks(conn, episode_id: int) -> list[str]:
    """返回阻断原因列表；空列表表示可以发布。"""
    problems: list[str] = []
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    m = conn.execute("SELECT * FROM media_assets WHERE id=?",
                     (ep["current_media_id"],)).fetchone() if ep["current_media_id"] else None

    # 1) 音频许可
    if m is not None:
        if m["license_status"] == "revoked":
            problems.append("音轨许可已被撤销")
        elif m["license_status"] == "expired":
            problems.append("音轨许可已过期")
    # 2) 嘉宾授权：active 片段里有 revoked 授权 -> 阻断；broken（待修复）也阻断
    revoked = conn.execute(
        "SELECT g.name FROM consent_segments c JOIN guests g ON g.id=c.guest_id "
        "WHERE c.episode_id=? AND c.media_id=? AND c.consent_status='revoked'",
        (episode_id, ep["current_media_id"]),
    ).fetchall()
    for r in revoked:
        problems.append(f"嘉宾授权已撤销: {r['name']}")
    broken_c = conn.execute(
        "SELECT 1 FROM consent_segments WHERE episode_id=? AND media_id=? AND status='broken' LIMIT 1",
        (episode_id, ep["current_media_id"]),
    ).fetchone()
    if broken_c:
        problems.append("存在无法映射、待修复的嘉宾授权片段")
    # 3) 音频与发布策略
    if m is None:
        if ep["release_policy"] == "hold":
            problems.append("缺少音频且策略为“暂缓发布”")
        # transcript_first：允许只发布文字稿
    else:
        if m["state"] != "ready":
            problems.append(f"音频尚未就绪（{m['state']}）——转码阶段未完成")
    # 4) 转码/RSS 流水线阶段（transcript_first 且无音频时跳过 transcode/rss-enclosure）
    if m is not None:
        t = conn.execute(
            "SELECT state FROM jobs WHERE episode_id=? AND stage='transcode' ORDER BY id DESC LIMIT 1",
            (episode_id,)).fetchone()
        if t and t["state"] not in ("done", "skipped"):
            problems.append("转码尚未完成")
    return problems


def publish_episode(episode_id: int, publish_at: str | None = None,
                    force: bool = False) -> dict:
    """发布 / 定时上线。force=False 时走全部闸门（定时上线时再次检查）。"""
    with db.tx() as conn:
        ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        if ep is None:
            raise ValueError("单集不存在")
        problems = pre_publish_checks(conn, episode_id)
        if problems and not force:
            conn.execute(
                "UPDATE episodes SET status='blocked', block_reason=?, updated_at=? WHERE id=?",
                ("; ".join(problems), db.now(), episode_id),
            )
            reason = "; ".join(problems)
        elif publish_at:
            conn.execute(
                "UPDATE episodes SET status='scheduled', publish_at=?, block_reason=NULL, updated_at=? WHERE id=?",
                (publish_at, db.now(), episode_id),
            )
            reason = None
        else:
            conn.execute(
                "UPDATE episodes SET status='published', published_at=COALESCE(published_at,?), "
                "publish_at=NULL, block_reason=NULL, updated_at=? WHERE id=?",
                (db.now(), db.now(), episode_id),
            )
            services.rebuild_fts(conn)
            # 发布那一刻冻结“实际渠道版本”：
            # fixed 模式钉住当前音轨；current 模式写入永久地址（后续可更新）
            ch = get_or_create_default_channel(conn)
            mode = "frozen" if ep["immutable_media"] else "current"
            snapshot_to_channel(conn, episode_id, ch["id"], mode)
            _bump_feed_etag(conn, ch["id"])
            conn.execute(
                "UPDATE jobs SET state='done', detail='已发布 RSS', updated_at=? "
                "WHERE episode_id=? AND stage='rss'", (db.now(), episode_id))
            reason = None
    # 事务提交后再抛出，保证 blocked 状态落库
    if problems and not force:
        raise PublishBlocked(reason)
    if publish_at and reason is None:
        return {"status": "scheduled", "publish_at": publish_at}
    return {"status": "published", "at": db.now()}


def run_scheduled(now_iso: str | None = None) -> list[dict]:
    """定时上线执行器：到点再次过闸门——授权在此期间被撤销则保持 blocked。"""
    results = []
    due = db.q(
        "SELECT * FROM episodes WHERE status='scheduled' AND publish_at IS NOT NULL AND publish_at<=?",
        (now_iso or db.now(),),
    )
    for ep in due:
        try:
            r = publish_episode(ep["id"])
            results.append({"episode_id": ep["id"], **r})
        except PublishBlocked as exc:
            results.append({"episode_id": ep["id"], "status": "blocked", "reason": str(exc)})
    return results


# ---------------------------------------------------------------------------
# 渠道版本（实际渠道版本）
# ---------------------------------------------------------------------------

def _enqueue_rss(conn, episode_id: int):
    row = conn.execute(
        "SELECT id FROM jobs WHERE episode_id=? AND stage='rss' AND state IN ('pending','running') "
        "ORDER BY id DESC LIMIT 1", (episode_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO jobs(episode_id,stage,state,detail,created_at,updated_at) "
            "VALUES (?, 'rss','pending','',?,?)", (episode_id, db.now(), db.now()))


def get_or_create_default_channel(conn):
    ch = conn.execute("SELECT * FROM channels WHERE slug='main'").fetchone()
    if ch is None:
        cid = conn.execute(
            "INSERT INTO channels(slug,name,kind,etag,last_build) VALUES('main','主频道','rss',NULL,NULL)"
        ).lastrowid
        ch = conn.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
    return ch


def snapshot_to_channel(conn, episode_id: int, ch_id: int, mode: str):
    """发布到渠道时冻结实际版本。返回 (changed: bool)。"""
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    m = conn.execute("SELECT * FROM media_assets WHERE id=?",
                     (ep["current_media_id"],)).fetchone() if ep["current_media_id"] else None
    existing = conn.execute(
        "SELECT * FROM channel_episodes WHERE channel_id=? AND episode_id=?",
        (ch_id, episode_id)).fetchone()

    media_id = m["id"] if (m and m["license_status"] == "active" and m["state"] == "ready") else None
    url = m["url"] if media_id else None
    guid = existing["rss_guid"] if existing else f"ep-{episode_id}@podhost"

    if mode == "frozen":
        # 固定媒体版本：渠道条目永久钉在发布那一刻的音轨上，之后重剪不改变它，
        # 除非发布者明确“以新版本重新发布”。
        if existing and existing["media_id"] is not None:
            return False
        eff_media, eff_url, eff_mode = (existing["media_id"], existing["enclosure_url"], "frozen") \
            if existing else (media_id, url, "frozen")
    else:
        # 可更新永久地址：enclosure URL/guid 不变，底层音轨可更新（etag 变化驱动缓存刷新）。
        eff_media, eff_url, eff_mode = media_id, url, "current"

    if existing:
        conn.execute(
            "UPDATE channel_episodes SET media_id=?, enclosure_url=?, media_mode=?, refreshed_at=? "
            "WHERE id=?",
            (eff_media, eff_url, eff_mode, db.now(), existing["id"]))
    else:
        conn.execute(
            """INSERT INTO channel_episodes
               (channel_id,episode_id,media_id,enclosure_url,rss_guid,media_mode,published_at,refreshed_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (ch_id, episode_id, eff_media, eff_url, guid, eff_mode, db.now(), db.now()))
    return True


def refresh_published_media(conn, episode_id: int, new_media_id: int) -> dict:
    """重剪后处理已发布单集的渠道版本。

    frozen 模式：已冻结的渠道条目不动（历史引用/缓存/订阅客户端仍拿到旧版本），
                排队等待人工“以新版本重新发布”。
    current 模式：永久地址指向新音轨，刷新渠道快照与 etag；订阅客户端刷新时拿到新内容。
    """
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if ep["status"] != "published":
        return {"published": False}
    ch = get_or_create_default_channel(conn)
    ce = conn.execute("SELECT * FROM channel_episodes WHERE channel_id=? AND episode_id=?",
                      (ch["id"], episode_id)).fetchone()
    mode = "current" if ep["immutable_media"] == 0 else "frozen"
    if ce and ce["media_mode"] == "frozen":
        # 固定版本：保持旧版本；只产生 RSS 待办供运营决定
        conn.execute(
            "INSERT INTO jobs(episode_id,stage,state,detail,created_at,updated_at) "
            "VALUES (?, 'rss','pending','固定版本：等待确认是否以新版本重新发布',?,?)",
            (episode_id, db.now(), db.now()))
        return {"published": True, "channel": "frozen", "channel_media_id": ce["media_id"],
                "note": "渠道保留旧固定版本；订阅客户端与历史引用不变，直到人工重新发布"}
    snapshot_to_channel(conn, episode_id, ch["id"], "current" if mode == "current" else "frozen")
    _bump_feed_etag(conn, ch["id"])
    conn.execute(
        "INSERT INTO jobs(episode_id,stage,state,detail,created_at,updated_at) "
        "VALUES (?, 'rss','done','永久地址已指向新音轨，feed etag 已更新',?,?)",
        (episode_id, db.now(), db.now()))
    return {"published": True, "channel": "current", "channel_media_id": new_media_id,
            "note": "永久地址不变但音频已更新；客户端刷新后按新 etag 获取"}


def republish_episode(episode_id: int):
    """固定版本模式下，运营明确把新版本推到渠道（旧的历史引用仍能通过版本 URL 访问）。"""
    with db.tx() as conn:
        ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        ch = get_or_create_default_channel(conn)
        snapshot_to_channel(conn, episode_id, ch["id"], "frozen")
        # 强制把冻结指针更新为当前音轨
        ce = conn.execute("SELECT * FROM channel_episodes WHERE channel_id=? AND episode_id=?",
                          (ch["id"], episode_id)).fetchone()
        m = conn.execute("SELECT * FROM media_assets WHERE id=?",
                         (ep["current_media_id"],)).fetchone()
        conn.execute("UPDATE channel_episodes SET media_id=?, enclosure_url=?, media_mode='frozen', "
                     "refreshed_at=? WHERE id=?",
                     (m["id"] if m else None, m["url"] if m else None, db.now(), ce["id"]))
        _bump_feed_etag(conn, ch["id"])
        return {"channel_media_id": m["id"] if m else None}


def publish_to_channels(episode_id: int):
    with db.tx() as conn:
        ch = get_or_create_default_channel(conn)
        ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        mode = "frozen" if ep["immutable_media"] else "current"
        changed = snapshot_to_channel(conn, episode_id, ch["id"], mode)
        _bump_feed_etag(conn, ch["id"])
        conn.execute("UPDATE jobs SET state='done', detail='已发布到 RSS', updated_at=? "
                     "WHERE episode_id=? AND stage='rss' AND state IN ('pending','running')",
                     (db.now(), episode_id))
        return {"changed": changed, "mode": mode}


def _bump_feed_etag(conn, channel_id: int):
    import hashlib
    rows = conn.execute(
        "SELECT ce.media_id, ce.enclosure_url, ce.refreshed_at FROM channel_episodes ce "
        "WHERE ce.channel_id=? ORDER BY ce.episode_id", (channel_id,)).fetchall()
    digest = hashlib.sha1("|".join(
        f"{r['media_id']}:{r['enclosure_url']}:{r['refreshed_at']}" for r in rows).encode()
    ).hexdigest()[:16]
    conn.execute("UPDATE channels SET etag=?, last_build=? WHERE id=?",
                 (f'"{digest}"', db.now(), channel_id))
    return f'"{digest}"'


# ---------------------------------------------------------------------------
# RSS（含缓存头：etag / last-modified；客户端条件请求返回 304）
# ---------------------------------------------------------------------------

def feed_state(channel_slug: str = "main"):
    ch = db.q1("SELECT * FROM channels WHERE slug=?", (channel_slug,))
    if ch is None:
        return None
    rows = db.q(
        """
        SELECT ce.*, e.title, e.summary, e.slug AS episode_slug, e.status
        FROM channel_episodes ce
        JOIN episodes e ON e.id=ce.episode_id
        WHERE ce.channel_id=? ORDER BY ce.published_at DESC
        """, (ch["id"],))
    return ch, [dict(r) for r in rows]


def render_rss(channel_slug: str = "main") -> str:
    state = feed_state(channel_slug)
    if state is None:
        return '<?xml version="1.0"?><rss version="2.0"><channel><title>empty</title></channel></rss>'
    ch, items = state
    parts = ['<?xml version="1.0"?>',
             '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>',
             f'<title>{_esc(ch["name"])}</title>',
             f'<lastBuildDate>{_esc(ch["last_build"] or "")}</lastBuildDate>']
    for it in items:
        # 网页呈现“实际渠道版本”：渠道条目里冻结的是谁，feed 里就是谁
        media = db.q1("SELECT * FROM media_assets WHERE id=?",
                      (it["media_id"],)) if it["media_id"] else None
        parts.append("<item>")
        parts.append(f"<title>{_esc(it['title'])}</title>")
        parts.append(f"<guid isPermaLink=\"false\">{_esc(it['rss_guid'])}</guid>")
        parts.append(f"<pubDate>{_esc(it['published_at'])}</pubDate>")
        if media and it["enclosure_url"]:
            # 固定版本用版本化 URL（不可变）；current 模式 URL 稳定，靠 etag 通知变化
            url = it["enclosure_url"]
            if it["media_mode"] == "frozen":
                url = f"{url}?v={media['version_no']}"
            parts.append(
                f'<enclosure url="{_esc(url)}" length="0" type="audio/mpeg"/>')
            parts.append(f"<itunes:duration>{media['duration_ms']//1000}</itunes:duration>")
            parts.append(f"<!-- channel-media-version v{media['version_no']} mode={it['media_mode']} -->")
        else:
            # 文字回退：没有 enclosure 的条目（文字稿先行或许可撤销后撤下音频）
            parts.append("<!-- text-only item: transcript fallback, no enclosure -->")
        parts.append(f"<link>/e/{_esc(it['episode_slug'])}</link>")
        parts.append(f"<description>{_esc(it['summary'])}</description>")
        parts.append("</item>")
    parts.append("</channel></rss>")
    return "".join(parts)


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ---------------------------------------------------------------------------
# 流水线阶段
# ---------------------------------------------------------------------------

def advance_jobs(episode_id: int | None = None):
    """分阶段完成：transcode -> index -> rss。前一阶段没做完，后一阶段保持 blocked。"""
    results = []
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT j.*, m.state AS media_state FROM jobs j "
            "JOIN episodes e ON e.id=j.episode_id "
            "LEFT JOIN media_assets m ON m.id=e.current_media_id "
            "WHERE j.id IN (SELECT MAX(id) FROM jobs GROUP BY episode_id, stage)" +
            (" AND j.episode_id=?" if episode_id else "") +
            " ORDER BY j.episode_id, j.id",
            (episode_id,) if episode_id else ()).fetchall()
        # 分阶段：transcode 未完成时，index/rss 保持 blocked
        done_tc: set[int] = set()
        done_idx: set[int] = set()
        for j in rows:
            detail, state = "", "done"
            if j["stage"] == "transcode":
                if j["media_state"] in (None, "withdrawn"):
                    state = "skipped"
                    detail = "无音频，跳过转码"
                elif j["media_state"] == "transcoding":
                    state, detail = "blocked", "等待转码完成"
                else:
                    detail = "转码完成"
            elif j["stage"] == "index":
                prev = conn.execute(
                    "SELECT state FROM jobs WHERE id<(SELECT MAX(id) FROM jobs) AND episode_id=? "
                    "AND stage='transcode' ORDER BY id DESC LIMIT 1",
                    (j["episode_id"],)).fetchone()
                services.rebuild_fts(conn)
                detail = "全文索引已按公开状态重建"
            elif j["stage"] == "rss":
                if j["episode_id"] not in done_idx:
                    state, detail = "blocked", "等待全文索引阶段完成"
                elif ep0 := conn.execute("SELECT * FROM episodes WHERE id=?",
                                         (j["episode_id"],)).fetchone():
                    if ep0["status"] == "published":
                        # 渠道“实际版本”的冻结/更新只由发布动作负责；这里不重复快照，
                        # 避免绕过固定媒体版本语义
                        ce = conn.execute(
                            "SELECT 1 FROM channel_episodes WHERE episode_id=?",
                            (j["episode_id"],)).fetchone()
                        if ce:
                            detail = "已发布 RSS"
                        else:
                            state, detail = "blocked", "等待发布动作写入渠道快照"
                    else:
                        state, detail = "blocked", "单集未发布，RSS 暂缓"
            conn.execute("UPDATE jobs SET state=?, detail=?, updated_at=? WHERE id=?",
                         (state, detail, db.now(), j["id"]))
            results.append({"job": j["stage"], "state": state, "detail": detail})
    return results


# ---------------------------------------------------------------------------
# 播放器回调：拒绝旧版本回调
# ---------------------------------------------------------------------------

def player_callback(episode_id: int, media_id: int, position_ms: int,
                    client_etag: str | None = None, channel_slug: str = "main") -> dict:
    """播放器进度回调。能否接受取决于“实际渠道版本”。

    - frozen 模式：渠道仍冻结在旧版本，旧版本回调在重新发布前仍有效；
      但 etag 不匹配（客户端缓存了被替换前的副本）仍拒绝。
    - current 模式：永久地址已指向新音轨，旧 media_id 的回调一律拒绝——
      旧秒数对新时间线没有意义，绝不能拿旧 position 定位新音频。
    """
    ep = db.q1("SELECT * FROM episodes WHERE id=?", (episode_id,))
    if ep is None:
        return _log(episode_id, media_id, position_ms, False, "episode not found", 404)
    ch = db.q1("SELECT * FROM channels WHERE slug=?", (channel_slug,))
    ce = db.q1("SELECT * FROM channel_episodes WHERE channel_id=? AND episode_id=?",
               (ch["id"], episode_id)) if ch else None
    channel_media = ce["media_id"] if ce else None
    mode = ce["media_mode"] if ce else ("frozen" if ep["immutable_media"] else "current")

    if mode == "frozen":
        if media_id != channel_media:
            return _log(episode_id, media_id, position_ms, False,
                        "stale callback: not the frozen channel version", 409)
    else:  # current：只有当前音轨版本合法
        if media_id != ep["current_media_id"]:
            return _log(episode_id, media_id, position_ms, False,
                        "stale callback: media version superseded by re-edit", 409)
    if client_etag:
        cur = db.q1("SELECT etag FROM media_assets WHERE id=?", (media_id,))
        if cur and cur["etag"] and client_etag != cur["etag"]:
            return _log(episode_id, media_id, position_ms, False,
                        "stale callback: client etag does not match current version", 409)
    return _log(episode_id, media_id, position_ms, True, "", 200)


def _log(episode_id, media_id, pos, accepted, reason, code):
    db.execute(
        "INSERT INTO player_events(episode_id,media_id,position_ms,accepted,reject_reason,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (episode_id, media_id, pos, 1 if accepted else 0, reason, db.now()))
    return {"accepted": accepted, "reason": reason, "code": code}
