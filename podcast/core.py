"""领域逻辑：单集/修订/重定位/分阶段发布流水线/调度/授权/RSS/搜索/播放器回调。"""
import json, time
from .edits import relocate_range

def now(): return time.time()

# ---------- 基础创建 ----------
def create_episode(db, slug, title, summary="", policy_code="hold", topic_ids=(), guest_ids=()):
    t = now()
    cur = db.execute("INSERT INTO episodes(slug,title,summary,status,policy_code,created_at,updated_at)"
                     " VALUES(?,?,?,'draft',?,?,?)", (slug, title, summary, policy_code, t, t))
    eid = cur.lastrowid
    for tid in topic_ids:
        db.execute("INSERT OR IGNORE INTO episode_topics(episode_id,topic_id) VALUES(?,?)", (eid, tid))
    for gid in guest_ids:
        db.execute("INSERT OR IGNORE INTO episode_guests(episode_id,guest_id) VALUES(?,?)", (eid, gid))
    db.commit(); return eid

def add_revision(db, episode_id, media_url=None, duration_ms=0, ops=None, state=None):
    """登记新音轨修订；若给出 ops 则把上一修订的活动注释经编辑映射重定位。"""
    prev = db.execute("SELECT * FROM audio_revisions WHERE episode_id=? ORDER BY version DESC LIMIT 1",
                      (episode_id,)).fetchone()
    version = (prev["version"] + 1) if prev else 1
    st = state or ("ready" if media_url else "missing")
    cur = db.execute("INSERT INTO audio_revisions(episode_id,version,media_url,duration_ms,state,supersedes,created_at)"
                     " VALUES(?,?,?,?,?,?,?)",
                     (episode_id, version, media_url, duration_ms, st, prev["id"] if prev else None, now()))
    rid = cur.lastrowid
    if prev and ops is not None:
        db.execute("INSERT INTO edit_maps(episode_id,from_revision,to_revision,ops_json,created_at)"
                   " VALUES(?,?,?,?,?)", (episode_id, prev["id"], rid, json.dumps(ops), now()))
        relocate_annotations(db, episode_id, prev["id"], rid, ops)
    db.commit(); return rid

def add_annotation(db, episode_id, revision_id, type_, start_ms, end_ms, text="", guest_id=None, license_id=None):
    cur = db.execute("INSERT INTO annotations(episode_id,revision_id,type,start_ms,end_ms,text,guest_id,license_id,status,created_at)"
                     " VALUES(?,?,?,?,?,?,?,?,'active',?)",
                     (episode_id, revision_id, type_, start_ms, end_ms, text, guest_id, license_id, now()))
    db.commit(); return cur.lastrowid

def relocate_annotations(db, episode_id, from_rid, to_rid, ops):
    """把 from 修订上的活动注释映射到 to 修订；无法映射的进入待修复。"""
    rows = db.execute("SELECT * FROM annotations WHERE episode_id=? AND revision_id=? AND status='active'",
                      (episode_id, from_rid)).fetchall()
    for r in rows:
        res, reason = relocate_range(r["start_ms"], r["end_ms"], ops)
        if res:
            ns, ne = res
            db.execute("INSERT INTO annotations(episode_id,revision_id,type,start_ms,end_ms,text,guest_id,license_id,status,created_at)"
                       " VALUES(?,?,?,?,?,?,?,?,'active',?)",
                       (episode_id, to_rid, r["type"], ns, ne, r["text"], r["guest_id"], r["license_id"], now()))
        else:
            db.execute("INSERT INTO annotations(episode_id,revision_id,type,start_ms,end_ms,text,guest_id,license_id,"
                       "status,reason,orig_start_ms,orig_end_ms,created_at)"
                       " VALUES(?,?,?,?,?,?,?,?,'pending_repair',?,?,?,?)",
                       (episode_id, to_rid, r["type"], r["start_ms"], r["end_ms"], r["text"],
                        r["guest_id"], r["license_id"], reason, r["start_ms"], r["end_ms"], now()))

def resolve_annotation(db, annotation_id, action, start_ms=None, end_ms=None, text=None):
    """待修复队列处理：action = fix（人工给定新时间码）| drop。"""
    a = db.execute("SELECT * FROM annotations WHERE id=?", (annotation_id,)).fetchone()
    if not a or a["status"] != "pending_repair":
        raise ValueError("not pending")
    if action == "drop":
        db.execute("UPDATE annotations SET status='dropped' WHERE id=?", (annotation_id,))
    else:
        db.execute("UPDATE annotations SET status='active', start_ms=?, end_ms=?, text=COALESCE(?,text), reason=''"
                   " WHERE id=?", (start_ms, end_ms, text, annotation_id))
    ep = a["episode_id"]
    db.commit()
    reindex_episode(db, ep)   # 字幕变化 -> 重建该集索引并使 RSS 过期
    bump_channels(db, ep)

# ---------- 授权 ----------
def set_license(db, guest_id, episode_id, status, scope="full", note=""):
    row = db.execute("SELECT id FROM licenses WHERE guest_id=? AND episode_id=? AND scope=?",
                     (guest_id, episode_id, scope)).fetchone()
    if row:
        db.execute("UPDATE licenses SET status=?, note=?, updated_at=? WHERE id=?",
                   (status, note, now(), row["id"])); lid = row["id"]
    else:
        cur = db.execute("INSERT INTO licenses(guest_id,episode_id,scope,status,note,updated_at) VALUES(?,?,?,?,?,?)",
                         (guest_id, episode_id, scope, status, note, now())); lid = cur.lastrowid
    db.commit()
    if status == "revoked":
        ep = db.execute("SELECT status FROM episodes WHERE id=?", (episode_id,)).fetchone()
        if ep and ep["status"] == "published":     # 已上线期间被撤销 -> 立即下架
            db.execute("UPDATE episodes SET status='held', updated_at=? WHERE id=?", (now(), episode_id))
            db.execute("DELETE FROM channel_versions WHERE episode_id=?", (episode_id,))
            log_event(db, episode_id, "license", "blocked", "license revoked after publish -> episode held")
            reindex_episode(db, episode_id); bump_channels(db, episode_id)
            db.commit()
    return lid

def license_ok(db, episode_id):
    guests = db.execute("SELECT guest_id FROM episode_guests WHERE episode_id=?", (episode_id,)).fetchall()
    missing = []
    for g in guests:
        lic = db.execute("SELECT status FROM licenses WHERE guest_id=? AND episode_id=? AND scope='full'",
                         (episode_id and g["guest_id"], episode_id)).fetchone()
        if not lic or lic["status"] != "granted":
            missing.append(g["guest_id"])
    return (len(missing) == 0), missing

# ---------- 分阶段流水线 ----------
def log_event(db, episode_id, stage, status, detail=""):
    db.execute("INSERT INTO publish_events(episode_id,stage,status,detail,created_at) VALUES(?,?,?,?,?)",
               (episode_id, stage, status, detail, now()))

def stage_transcode(db, episode_id, revision_id):
    """转码阶段（模拟）：有媒体地址则 ready，否则 missing。"""
    if revision_id is None:
        log_event(db, episode_id, "transcode", "skipped", "no revision"); return "missing"
    r = db.execute("SELECT * FROM audio_revisions WHERE id=?", (revision_id,)).fetchone()
    st = "ready" if r["media_url"] else "missing"
    db.execute("UPDATE audio_revisions SET state=? WHERE id=?", (st, revision_id))
    log_event(db, episode_id, "transcode", "done", f"revision {r['version']} -> {st}")
    return st

def stage_index(db, episode_id):
    """全文索引阶段：只索引当前公开渠道有效修订的 active 注释。"""
    reindex_episode(db, episode_id)
    log_event(db, episode_id, "index", "done", "fts rebuilt for episode")

def stage_rss(db, episode_id):
    """RSS 阶段：递增渠道代次使缓存过期（懒重建）。"""
    bump_channels(db, episode_id)
    log_event(db, episode_id, "rss", "done", "channel generation bumped, cache invalidated")

def run_pipeline(db, episode_id, revision_id):
    stage_transcode(db, episode_id, revision_id)
    stage_index(db, episode_id)
    stage_rss(db, episode_id)
    db.commit()

# ---------- 发布 ----------
def current_revision(db, episode_id):
    return db.execute("SELECT * FROM audio_revisions WHERE episode_id=? ORDER BY version DESC LIMIT 1",
                      (episode_id,)).fetchone()

def publish_episode(db, episode_id, channel_code="public"):
    """上线闸门：授权检查 -> 缺音频策略（公开文字稿 or 暂缓）-> 写渠道版本 -> 分阶段流水线。"""
    ep = db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    ok, missing = license_ok(db, episode_id)
    if not ok:
        log_event(db, episode_id, "publish", "blocked", f"license missing/revoked for guests {missing}")
        db.commit()
        return {"published": False, "reason": "license_revoked", "guests": missing}
    rev = current_revision(db, episode_id)
    pol = db.execute("SELECT * FROM policies WHERE code=?", (ep["policy_code"],)).fetchone()
    allow_text = bool(pol and pol["allow_transcript_only"])
    if not rev or rev["state"] != "ready":
        if not allow_text:
            db.execute("UPDATE episodes SET status='held', updated_at=? WHERE id=?", (now(), episode_id))
            log_event(db, episode_id, "publish", "blocked", "audio missing and policy=hold")
            db.commit()
            return {"published": False, "reason": "audio_missing_held"}
        ch = db.execute("SELECT id FROM channels WHERE code=?", (channel_code,)).fetchone()
        db.execute("INSERT INTO channel_versions(channel_id,episode_id,revision_id,transcript_only,published_at)"
                   " VALUES(?,?,NULL,1,?) ON CONFLICT(channel_id,episode_id) DO UPDATE SET"
                   " revision_id=NULL, transcript_only=1, published_at=excluded.published_at",
                   (ch["id"], episode_id, now()))
        db.execute("UPDATE episodes SET status='published', updated_at=? WHERE id=?", (now(), episode_id))
        run_pipeline(db, episode_id, rev["id"] if rev else None)
        log_event(db, episode_id, "publish", "done", "transcript-only (policy)")
        db.commit()
        return {"published": True, "transcript_only": True}
    ch = db.execute("SELECT id FROM channels WHERE code=?", (channel_code,)).fetchone()
    db.execute("INSERT INTO channel_versions(channel_id,episode_id,revision_id,transcript_only,published_at)"
               " VALUES(?,?,?,0,?) ON CONFLICT(channel_id,episode_id) DO UPDATE SET"
               " revision_id=excluded.revision_id, transcript_only=0, published_at=excluded.published_at",
               (ch["id"], episode_id, rev["id"], now()))
    db.execute("UPDATE episodes SET status='published', updated_at=? WHERE id=?", (now(), episode_id))
    run_pipeline(db, episode_id, rev["id"])
    log_event(db, episode_id, "publish", "done", f"revision v{rev['version']} on {channel_code}")
    db.commit()
    return {"published": True, "revision": rev["id"], "version": rev["version"]}

def schedule_publish(db, episode_id, run_at, channel_code="public"):
    db.execute("INSERT INTO publish_schedules(episode_id,run_at,channel_code,status,created_at) VALUES(?,?,?,'pending',?)",
               (episode_id, run_at, channel_code, now()))
    db.execute("UPDATE episodes SET status='scheduled', publish_at=?, updated_at=? WHERE id=?",
               (run_at, now(), episode_id))
    db.commit()

def run_scheduler(db, at=None):
    """处理到期的发布计划；授权被撤销则 blocked，不上线。"""
    at = at or now()
    due = db.execute("SELECT * FROM publish_schedules WHERE status='pending' AND run_at<=?", (at,)).fetchall()
    results = []
    for s in due:
        ok, missing = license_ok(db, s["episode_id"])
        if not ok:
            db.execute("UPDATE publish_schedules SET status='blocked', blocked_reason=? WHERE id=?",
                       (f"license revoked/missing for guests {missing}", s["id"]))
            db.execute("UPDATE episodes SET status='held', updated_at=? WHERE id=?", (now(), s["episode_id"]))
            log_event(db, s["episode_id"], "schedule", "blocked", "license revoked before scheduled launch")
            results.append({"schedule": s["id"], "status": "blocked"})
        else:
            r = publish_episode(db, s["episode_id"], s["channel_code"])
            st = "done" if r["published"] else "blocked"
            db.execute("UPDATE publish_schedules SET status=?, blocked_reason=? WHERE id=?",
                       (st, "" if r["published"] else r.get("reason", ""), s["id"]))
            results.append({"schedule": s["id"], "status": st})
    db.commit(); return results

# ---------- 换版本上线（换音轨）----------
def promote_revision(db, episode_id, revision_id, channel_code="public"):
    """把已发布单集切到指定修订：渠道版本指向新修订，重跑流水线，旧播放器回调随之失效。"""
    ep = db.execute("SELECT status FROM episodes WHERE id=?", (episode_id,)).fetchone()
    ch = db.execute("SELECT id FROM channels WHERE code=?", (channel_code,)).fetchone()
    db.execute("INSERT INTO channel_versions(channel_id,episode_id,revision_id,transcript_only,published_at)"
               " VALUES(?,?,?,0,?) ON CONFLICT(channel_id,episode_id) DO UPDATE SET"
               " revision_id=excluded.revision_id, transcript_only=0, published_at=excluded.published_at",
               (ch["id"], episode_id, revision_id, now()))
    db.execute("UPDATE episodes SET updated_at=? WHERE id=?", (now(), episode_id))
    run_pipeline(db, episode_id, revision_id)
    log_event(db, episode_id, "publish", "done", f"promoted revision {revision_id} on {channel_code}")
    db.commit()
    return ep["status"] == "published"

# ---------- 索引 / 搜索 ----------
def reindex_episode(db, episode_id):
    db.execute("DELETE FROM seg_fts WHERE episode_id=?", (episode_id,))
    ep = db.execute("SELECT status FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if ep and ep["status"] == "published":
        cv = db.execute("SELECT cv.* FROM channel_versions cv JOIN channels c ON c.id=cv.channel_id"
                        " WHERE cv.episode_id=? AND c.code='public'", (episode_id,)).fetchone()
        if cv:
            if cv["revision_id"]:
                rows = db.execute("SELECT id,type,text FROM annotations WHERE revision_id=? AND status='active'",
                                  (cv["revision_id"],)).fetchall()
            else:   # 纯文字稿上线：索引最新修订的文字
                rev = current_revision(db, episode_id)
                rows = db.execute("SELECT id,type,text FROM annotations WHERE revision_id=? AND status='active'",
                                  (rev["id"],)).fetchall() if rev else []
            for r in rows:
                db.execute("INSERT INTO seg_fts(text,episode_id,annotation_id,type) VALUES(?,?,?,?)",
                           (r["text"], episode_id, r["id"], r["type"]))
    db.commit()

def search(db, q, limit=50):
    """公开搜索：FTS 命中后再按当前发布状态过滤，未公开访谈片段不可见。"""
    rows = db.execute(
        "SELECT f.episode_id, f.annotation_id, f.type, snippet(seg_fts,0,'[',']','…',12) AS snip"
        " FROM seg_fts f WHERE seg_fts MATCH ? LIMIT ?", (q, limit * 3)).fetchall()
    out = []
    for r in rows:
        ep = db.execute("SELECT id,slug,title,status FROM episodes WHERE id=?", (r["episode_id"],)).fetchone()
        if not ep or ep["status"] != "published":
            continue
        cv = db.execute("SELECT cv.revision_id FROM channel_versions cv JOIN channels c ON c.id=cv.channel_id"
                        " WHERE cv.episode_id=? AND c.code='public'", (ep["id"],)).fetchone()
        if not cv:
            continue
        a = db.execute("SELECT status FROM annotations WHERE id=?", (r["annotation_id"],)).fetchone()
        if not a or a["status"] != "active":
            continue
        out.append({"episode_id": ep["id"], "slug": ep["slug"], "title": ep["title"],
                    "type": r["type"], "snippet": r["snip"]})
        if len(out) >= limit:
            break
    return out

# ---------- RSS ----------
def rss_watermark(db, channel_code):
    row = db.execute(
        "SELECT COUNT(*) n, COALESCE(MAX(e.updated_at),0) m, COALESCE(SUM(cv.revision_id),0) s"
        " FROM channel_versions cv JOIN episodes e ON e.id=cv.episode_id"
        " JOIN channels c ON c.id=cv.channel_id WHERE c.code=? AND e.status='published'",
        (channel_code,)).fetchone()
    ch = db.execute("SELECT generation FROM channels WHERE code=?", (channel_code,)).fetchone()
    return f"{row['n']}:{row['m']:.3f}:{row['s']}:{ch['generation']}"

def bump_channels(db, episode_id=None):
    db.execute("UPDATE channels SET generation=generation+1")
    db.commit()

def build_rss(db, channel_code):
    ch = db.execute("SELECT * FROM channels WHERE code=?", (channel_code,)).fetchone()
    rows = db.execute(
        "SELECT e.*, cv.revision_id, cv.transcript_only, cv.published_at FROM channel_versions cv"
        " JOIN episodes e ON e.id=cv.episode_id WHERE cv.channel_id=? AND e.status='published'"
        " ORDER BY cv.published_at DESC", (ch["id"],)).fetchall()
    items = []
    for e in rows:
        if e["transcript_only"] or not e["revision_id"]:
            body = f"<p>{esc(e['summary'])}</p><p>[本期暂以文字稿形式发布]</p>"
            enclosure = ""
        else:
            rev = db.execute("SELECT * FROM audio_revisions WHERE id=?", (e["revision_id"],)).fetchone()
            body = f"<p>{esc(e['summary'])}</p>"
            enclosure = (f'<enclosure url="{esc(rev["media_url"])}" type="audio/mpeg" length="1"/>'
                         if rev["media_url"] else "")
        items.append(f"<item><title>{esc(e['title'])}</title><guid>{esc(e['slug'])}</guid>"
                     f"<description><![CDATA[{body}]]></description>{enclosure}"
                     f"<pubDate>{time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime(e['published_at']))}</pubDate></item>")
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<rss version="2.0"><channel><title>Podcast ({esc(channel_code)})</title>'
            + "".join(items) + "</channel></rss>")

def get_rss(db, channel_code):
    """带水位线的缓存：发布面任何变化（含缓存被篡改）都会触发重建。"""
    ch = db.execute("SELECT * FROM channels WHERE code=?", (channel_code,)).fetchone()
    wm = rss_watermark(db, channel_code)
    cached = db.execute("SELECT * FROM rss_cache WHERE channel_id=?", (ch["id"],)).fetchone()
    if cached and cached["watermark"] == wm:
        return cached["xml"], True
    xml = build_rss(db, channel_code)
    db.execute("INSERT INTO rss_cache(channel_id,xml,built_at,watermark) VALUES(?,?,?,?)"
               " ON CONFLICT(channel_id) DO UPDATE SET xml=excluded.xml,built_at=excluded.built_at,watermark=excluded.watermark",
               (ch["id"], xml, now(), wm))
    db.commit()
    return xml, False

# ---------- 播放器回调 ----------
def player_event(db, episode_id, revision_id, event, position_ms=0, client_id="", channel_code="public"):
    cv = db.execute("SELECT cv.revision_id FROM channel_versions cv JOIN channels c ON c.id=cv.channel_id"
                    " WHERE cv.episode_id=? AND c.code=?", (episode_id, channel_code)).fetchone()
    current = cv["revision_id"] if cv else None
    applied = 1 if (current is not None and revision_id == current) else 0
    note = "" if applied else f"stale_revision(current={current})"
    db.execute("INSERT INTO player_events(episode_id,revision_id,event,position_ms,client_id,applied,note,received_at)"
               " VALUES(?,?,?,?,?,?,?,?)",
               (episode_id, revision_id, event, position_ms, client_id, applied, note, now()))
    db.commit()
    return {"applied": bool(applied), "note": note}

def esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))
