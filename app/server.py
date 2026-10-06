"""Zero-dependency HTTP server wiring all routes (public + admin + API + RSS)."""
from __future__ import annotations

import json
import os
import re
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse

from . import db, auth, admin as admin_svc, services, publish, public as public_svc
from . import views, admin_views

STATIC = {
    "/static/app.css": ("text/css; charset=utf-8", "app.css"),
    "/static/player.js": ("application/javascript; charset=utf-8", "player.js"),
}


def parse_segments(raw: str) -> list[dict]:
    segs = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        seg = {"kind": parts[0], "src_start": int(parts[1]), "src_end": int(parts[2])}
        if len(parts) >= 5:
            seg["dst_start"] = int(parts[3]); seg["dst_end"] = int(parts[4])
        segs.append(seg)
    return segs


class App:
    def __init__(self):
        self.routes = []

    def route(self, method, pattern):
        rx = "^" + re.sub(r"<(\w+)>", r"(?P<\1>[^/]+)", pattern) + "$"
        def deco(fn):
            self.routes.append((method, re.compile(rx), fn))
            return fn
        return deco

    def handle(self, req):
        path = urlparse(req["path"]).path
        for method, rx, fn in self.routes:
            if method != req["method"]:
                continue
            m = rx.match(path)
            if m:
                return fn(req, **m.groupdict())
        if req["path"] in STATIC and req["method"] == "GET":
            ctype, fname = STATIC[req["path"]]
            with open(os.path.join(os.path.dirname(__file__), "static", fname), "rb") as f:
                return 200, {"Content-Type": ctype}, f.read()
        return 404, {"Content-Type": "text/plain; charset=utf-8"}, "not found".encode()


def html(body: str, status: int = 200, extra_headers=None):
    h = {"Content-Type": "text/html; charset=utf-8"}
    if extra_headers:
        h.update(extra_headers)
    return status, h, body.encode()


def redirect(to, cookies=None):
    h = {"Location": to}
    if cookies:
        h["Set-Cookie"] = cookies
    return 303, h, b""


app = App()


# ---------------- public ----------------------------------------------------

@app.route("GET", "/")
def r_home(req):
    topics = public_svc.topics_overview()
    eps = public_svc.public_episodes_by_topic()
    return html(views.home(topics, eps))


@app.route("GET", "/t/<slug>")
def r_topic(req, slug):
    row = db.q1("SELECT * FROM topics WHERE slug=?", (slug,))
    if not row:
        return html(views.layout("没有该话题", "<p>话题不存在。</p>"), 404)
    return html(views.topic_page(dict(row), public_svc.public_episodes_by_topic(slug)))


@app.route("GET", "/e/<slug>")
def r_episode(req, slug):
    data = public_svc.episode_public(slug)
    if data is None:
        return html(views.layout("未找到", "<p>单集不存在。</p>"), 404)
    if data.get("forbidden"):
        return html(views.episode_page(data), 404)
    return html(views.episode_page(data))


@app.route("GET", "/search")
def r_search(req):
    q = req["query"].get("q", [""])[0]
    results = services.search_public(q) if q else []
    return html(views.search_page(q, results))


@app.route("GET", "/business")
def r_business(req):
    return html(views.business_page())


@app.route("POST", "/business")
def r_business_post(req):
    f = req["form"]
    admin_svc.add_inquiry(f.get("name", [""])[0], f.get("contact", [""])[0],
                          f.get("body", [""])[0], f.get("topic", [""])[0])
    return html(views.business_page("已收到，商务团队会尽快联系。"))


@app.route("GET", "/feed.xml")
def r_feed(req):
    state = publish.feed_state("main")
    ch = state[0] if state else None
    etag = ch["etag"] if ch and ch["etag"] else '"empty"'
    inm = req["headers"].get("If-None-Match")
    headers = {"Content-Type": "application/rss+xml; charset=utf-8",
               "ETag": etag,
               "Cache-Control": "public, max-age=900"}
    if ch and ch["last_build"]:
        headers["Last-Modified"] = ch["last_build"]
    # 客户端/中间缓存条件请求：etag 未变 -> 304（RSS 缓存未刷新即如此表现）
    if inm and inm == etag:
        return 304, headers, b""
    return 200, headers, publish.render_rss("main").encode()


# ---------------- player callback API ---------------------------------------

@app.route("POST", "/api/episodes/<eid>/progress")
def r_progress(req, eid):
    payload = {}
    try:
        payload = json.loads(req["body"].decode() or "{}")
    except Exception:
        payload = dict(parse_qs(req["body"].decode()))
        payload = {k: (v[0] if isinstance(v, list) else v) for k, v in payload.items()}
    media_id = int(payload.get("media_id", 0))
    pos = int(payload.get("position_ms", 0))
    cetag = payload.get("etag")
    r = publish.player_callback(int(eid), media_id, pos, cetag)
    return r["code"], {"Content-Type": "application/json"}, json.dumps(r).encode()


# ---------------- admin auth ------------------------------------------------

def current_user(req):
    cookie = SimpleCookie()
    if "Cookie" in req["headers"]:
        cookie.load(req["headers"]["Cookie"])
    token = cookie.get("session")
    return auth.session_user(token.value if token else None), (token.value if token else None)


def require_admin(fn):
    def wrapper(req, **kw):
        user, _ = current_user(req)
        if not user:
            return html(views.login_page("请先登录"), 401)
        req["user"] = user
        return fn(req, **kw)
    wrapper.__name__ = fn.__name__
    return wrapper


@app.route("GET", "/admin/login")
def r_login(req, **kw):
    return html(views.login_page())


@app.route("POST", "/admin/login")
def r_login_post(req):
    f = req["form"]
    uid = auth.verify(f.get("username", [""])[0], f.get("password", [""])[0])
    if not uid:
        return html(views.login_page("用户名或密码错误"), 401)
    token = auth.create_session(uid)
    return redirect("/admin", f"session={token}; HttpOnly; Path=/; SameSite=Lax")


@app.route("GET", "/admin/logout")
def r_logout(req):
    _, token = current_user(req)
    if token:
        auth.destroy_session(token)
    return redirect("/", "session=; Max-Age=0; Path=/")


# ---------------- admin pages -----------------------------------------------

@app.route("GET", "/admin")
@require_admin
def r_admin(req):
    episodes = [dict(r) for r in db.q(
        """SELECT e.*, t.name AS topic_name, m.version_no FROM episodes e
           LEFT JOIN topics t ON t.id=e.topic_id
           LEFT JOIN media_assets m ON m.id=e.current_media_id
           ORDER BY e.id DESC""")]
    topics = [dict(r) for r in db.q("SELECT * FROM topics ORDER BY name")]
    guests = [dict(r) for r in db.q("SELECT * FROM guests ORDER BY name")]
    scheduled = [dict(r) for r in db.q(
        "SELECT id,title,publish_at FROM episodes WHERE status='scheduled' ORDER BY publish_at")]
    broken = db.q1(
        "SELECT (SELECT COUNT(*) FROM transcript_lines WHERE status='broken')"
        "+(SELECT COUNT(*) FROM chapters WHERE status='broken')"
        "+(SELECT COUNT(*) FROM consent_segments WHERE status='broken')"
        "+(SELECT COUNT(*) FROM annotations WHERE status='broken') AS c")["c"]
    return html(admin_views.dashboard(episodes, topics, guests, scheduled, broken))


@app.route("POST", "/admin/topics")
@require_admin
def r_new_topic(req):
    f = req["form"]
    admin_svc.create_topic(f.get("name", [""])[0], f.get("description", [""])[0])
    return redirect("/admin")


@app.route("POST", "/admin/guests")
@require_admin
def r_new_guest(req):
    f = req["form"]
    admin_svc.create_guest(f.get("name", [""])[0], f.get("bio", [""])[0])
    return redirect("/admin")


@app.route("POST", "/admin/episodes")
@require_admin
def r_new_episode(req):
    f = req["form"]
    eid = admin_svc.create_episode(
        f.get("title", [""])[0], 1, int(f.get("topic_id", ["1"])[0]),
        release_policy=f.get("release_policy", ["hold"])[0],
        immutable_media=int(f.get("immutable_media", ["1"])[0]))
    return redirect(f"/admin/episodes/{eid}")


def _episode_detail(episode_id: int):
    ep = db.q1("SELECT * FROM episodes WHERE id=?", (episode_id,))
    if not ep:
        return None
    ep = dict(ep)
    medias = [dict(r) for r in db.q(
        "SELECT * FROM media_assets WHERE episode_id=? ORDER BY version_no", (episode_id,))]
    consents = [dict(r) for r in db.q(
        "SELECT * FROM consent_segments WHERE episode_id=? AND media_id=? ORDER BY start_ms",
        (episode_id, ep["current_media_id"]))]
    lines = [dict(r) for r in db.q(
        "SELECT * FROM transcript_lines WHERE episode_id=? AND media_id=? ORDER BY start_ms",
        (episode_id, ep["current_media_id"]))]
    chapters = [dict(r) for r in db.q(
        "SELECT * FROM chapters WHERE episode_id=? AND media_id=? ORDER BY start_ms",
        (episode_id, ep["current_media_id"]))]
    annotations = [dict(r) for r in db.q(
        "SELECT * FROM annotations WHERE episode_id=? AND media_id=? ORDER BY start_ms",
        (episode_id, ep["current_media_id"]))]
    jobs = [dict(r) for r in db.q(
        "SELECT * FROM jobs WHERE episode_id=? ORDER BY id DESC LIMIT 9", (episode_id,))]
    ch = db.q1("SELECT * FROM channels WHERE slug='main'")
    ce = db.q1("SELECT * FROM channel_episodes WHERE channel_id=? AND episode_id=?",
               (ch["id"], episode_id)) if ch else None
    return {"episode": ep, "medias": medias, "consents": consents, "lines": lines,
            "chapters": chapters, "annotations": annotations, "jobs": jobs,
            "channel_episode": dict(ce) if ce else None}


@app.route("GET", "/admin/episodes/<eid>")
@require_admin
def r_ep(req, eid):
    d = _episode_detail(int(eid))
    if not d:
        return 404, {}, b"not found"
    guests = [dict(r) for r in db.q("SELECT * FROM guests ORDER BY name")]
    topics = [dict(r) for r in db.q("SELECT * FROM topics ORDER BY name")]
    rq = services.repair_queue(int(eid))
    return html(admin_views.episode_edit(d, guests, topics, rq))


@app.route("POST", "/admin/episodes/<eid>/media")
@require_admin
def r_add_media(req, eid):
    f = req["form"]
    services.add_media(int(eid), f["url"][0], int(f["duration_ms"][0]),
                       license=f.get("license", ["owned"])[0])
    return redirect(f"/admin/episodes/{eid}")


@app.route("POST", "/admin/episodes/<eid>/reedit")
@require_admin
def r_reedit(req, eid):
    f = req["form"]
    try:
        services.reedit_episode(int(eid), f["new_url"][0], parse_segments(f["segments"][0]))
    except ValueError as exc:
        return html(admin_views.shell(f"<p class='notice err'>{exc}</p>"
                                     f"<p><a href='/admin/episodes/{eid}'>返回</a></p>"), 400)
    return redirect(f"/admin/episodes/{eid}")


@app.route("POST", "/admin/episodes/<eid>/transcript")
@require_admin
def r_add_line(req, eid):
    f = req["form"]
    admin_svc.add_transcript_line(int(eid), f["speaker"][0],
                                  int(f["start_ms"][0]), int(f["end_ms"][0]), f["text"][0])
    return redirect(f"/admin/episodes/{eid}")


@app.route("POST", "/admin/episodes/<eid>/chapters")
@require_admin
def r_add_chapter(req, eid):
    f = req["form"]
    admin_svc.add_chapter(int(eid), int(f["start_ms"][0]), f["title"][0])
    return redirect(f"/admin/episodes/{eid}")


@app.route("POST", "/admin/episodes/<eid>/annotations")
@require_admin
def r_add_anno(req, eid):
    f = req["form"]
    admin_svc.add_annotation(int(eid), int(f["start_ms"][0]), int(f["end_ms"][0]),
                             f.get("title", [""])[0])
    return redirect(f"/admin/episodes/{eid}")


@app.route("POST", "/admin/episodes/<eid>/guests")
@require_admin
def r_attach_guest(req, eid):
    admin_svc.attach_guest(int(eid), int(req["form"]["guest_id"][0]))
    return redirect(f"/admin/episodes/{eid}")


@app.route("POST", "/admin/episodes/<eid>/consent")
@require_admin
def r_consent(req, eid):
    f = req["form"]
    admin_svc.set_consent(int(eid), int(f["guest_id"][0]), int(f["start_ms"][0]),
                          int(f["end_ms"][0]), f.get("title", [""])[0])
    return redirect(f"/admin/episodes/{eid}")


@app.route("POST", "/admin/consent/<cid>/revoke")
@require_admin
def r_revoke_consent(req, cid):
    eid = admin_svc.revoke_consent(int(cid))
    return redirect(f"/admin/episodes/{eid}")


@app.route("POST", "/admin/episodes/<eid>/revoke-license")
@require_admin
def r_revoke_license(req, eid):
    mid = admin_svc.current_media_id(int(eid))
    services.revoke_license(mid, "模拟撤销")
    return redirect(f"/admin/episodes/{eid}")


@app.route("POST", "/admin/episodes/<eid>/advance")
@require_admin
def r_advance(req, eid):
    publish.advance_jobs(int(eid))
    return redirect(f"/admin/episodes/{eid}")


@app.route("POST", "/admin/episodes/<eid>/publish")
@require_admin
def r_publish(req, eid):
    try:
        publish.publish_episode(int(eid))
    except publish.PublishBlocked as exc:
        return html(admin_views.shell(
            f"<p class='notice err'>发布被阻断：{exc}</p>"
            f"<p><a href='/admin/episodes/{eid}'>返回</a></p>"), 409)
    return redirect(f"/admin/episodes/{eid}")


@app.route("POST", "/admin/episodes/<eid>/schedule")
@require_admin
def r_schedule(req, eid):
    at = req["form"]["publish_at"][0]
    try:
        publish.publish_episode(int(eid), publish_at=at)
    except publish.PublishBlocked as exc:
        return html(admin_views.shell(
            f"<p class='notice err'>定时失败：{exc}</p>"
            f"<p><a href='/admin/episodes/{eid}'>返回</a></p>"), 409)
    return redirect(f"/admin/episodes/{eid}")


@app.route("POST", "/admin/episodes/<eid>/republish")
@require_admin
def r_republish(req, eid):
    publish.republish_episode(int(eid))
    return redirect(f"/admin/episodes/{eid}")


@app.route("POST", "/admin/run-scheduled")
@require_admin
def r_run_scheduled(req):
    publish.run_scheduled()
    return redirect("/admin")


@app.route("GET", "/admin/repair")
@require_admin
def r_repair(req):
    return html(admin_views.repair_page(services.repair_queue()))


@app.route("POST", "/admin/repair/<kind>/<iid>")
@require_admin
def r_fix(req, kind, iid):
    f = req["form"]
    end = f.get("end_ms", [""])[0]
    services.fix_item(kind, int(iid), int(f["start_ms"][0]), int(end) if end else None)
    return redirect("/admin/repair")


@app.route("GET", "/admin/inquiries")
@require_admin
def r_inquiries(req):
    return html(admin_views.inquiries_page(admin_svc.list_inquiries()))


@app.route("GET", "/api/current-episode")
def r_current_episode(req):
    slug = req["query"].get("slug", [""])[0]
    row = db.q1("SELECT id, current_media_id FROM episodes WHERE slug=?", (slug,))
    if not row:
        return 404, {"Content-Type": "application/json"}, b'{"error":"not found"}'
    return 200, {"Content-Type": "application/json"}, json.dumps(dict(row)).encode()
