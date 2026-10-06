"""HTTP 层：公开站点（按话题浏览/收听、文字回退、搜索、商务表单、RSS）+ 管理后台（单集/嘉宾/文字稿/授权/计划/待修复）。"""
import json, os, re, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from . import core
from .core import esc

ADMIN_KEY = os.environ.get("ADMIN_KEY", "admin-dev-key")

def fmt_ms(ms):
    s = ms // 1000
    return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"

PAGE = """<!doctype html><html lang=zh><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{title}</title><style>
body{{font-family:system-ui,sans-serif;max-width:960px;margin:0 auto;padding:16px;line-height:1.6;color:#222}}
a{{color:#0b5fff;text-decoration:none}} a:hover{{text-decoration:underline}}
nav a{{margin-right:12px}} .card{{border:1px solid #ddd;border-radius:8px;padding:12px;margin:10px 0}}
.badge{{display:inline-block;background:#eef;border-radius:4px;padding:1px 8px;font-size:12px;margin-left:6px}}
.warn{{background:#fff3cd;border:1px solid #ffe08a;padding:8px;border-radius:6px}}
.err{{background:#f8d7da;border:1px solid #f1aeb5;padding:8px;border-radius:6px}}
table{{border-collapse:collapse;width:100%}} td,th{{border:1px solid #ddd;padding:6px;font-size:14px}}
input,textarea,select{{width:100%;padding:6px;margin:4px 0;box-sizing:border-box}} button{{padding:6px 14px}}
.cue{{font-size:14px;color:#444}} .cue b{{color:#999;font-weight:normal;margin-right:8px}}
.pending{{background:#fff3cd}} audio{{width:100%}}
</style></head><body>
<nav><a href="/">首页</a><a href="/search">搜索</a><a href="/contact">商务合作</a><a href="/rss/public.xml">RSS</a><a href="/admin">管理</a></nav>
{body}</body></html>"""

def page(title, body):
    return PAGE.format(title=esc(title), body=body).encode()

class App(BaseHTTPRequestHandler):
    db = None
    lock = threading.RLock()
    def log_message(self, *a): pass

    # ---------- 基础 ----------
    def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        if isinstance(body, str): body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        ct = self.headers.get("Content-Type", "")
        if "json" in ct:
            return json.loads(raw or b"{}")
        return {k: v[0] for k, v in parse_qs(raw.decode()).items()}

    def _admin_ok(self, q):
        return self.headers.get("X-Admin-Key") == ADMIN_KEY or q.get("key", [""])[0] == ADMIN_KEY

    def do_GET(self):
        with type(self).lock:
            return self._get()

    def do_POST(self):
        with type(self).lock:
            return self._post()

    def _get(self):
        u = urlparse(self.path); q = parse_qs(u.query); p = u.path
        try:
            if p == "/": return self._home()
            if p.startswith("/topic/"): return self._topic(p.split("/")[2])
            if p.startswith("/episode/"): return self._episode(p.split("/")[2], q)
            if p == "/search": return self._search(q)
            if p == "/contact": return self._contact()
            if p.startswith("/rss/") and p.endswith(".xml"): return self._rss(p[5:-4])
            if p.startswith("/api/player/"): return self._player_api(p.split("/")[3], q)
            if p == "/api/inquiries": return self._inquiries(q)
            if p.startswith("/admin"): return self._admin(p, q)
            self._send(404, page("404", "<h1>404</h1>"))
        except Exception as e:
            self._send(500, page("错误", f"<h1>500</h1><pre>{esc(repr(e))}</pre>"))

    def _post(self):
        u = urlparse(self.path); q = parse_qs(u.query); p = u.path
        try:
            d = self._body()
            if p == "/api/inquiries": return self._post_inquiry(d)
            if p == "/api/player/events": return self._json(core.player_event(
                self.db, int(d.get("episode_id")), int(d.get("revision_id")),
                d.get("event", "progress"), int(d.get("position_ms", 0)), d.get("client_id", "")))
            if p.startswith("/api/admin/"): return self._admin_api(p, d, q)
            self._send(404, page("404", "<h1>404</h1>"))
        except Exception as e:
            self._send(500, page("错误", f"<h1>500</h1><pre>{esc(repr(e))}</pre>"))

    # ---------- 公开页 ----------
    def _home(self):
        topics = self.db.execute("SELECT * FROM topics ORDER BY name").fetchall()
        eps = self.db.execute(
            "SELECT e.*, cv.revision_id, cv.transcript_only FROM episodes e"
            " JOIN channel_versions cv ON cv.episode_id=e.id JOIN channels c ON c.id=cv.channel_id"
            " WHERE e.status='published' AND c.code='public' ORDER BY cv.published_at DESC").fetchall()
        chips = " ".join(f'<a class=badge href="/topic/{esc(t["slug"])}">{esc(t["name"])}</a>' for t in topics)
        cards = ""
        for e in eps:
            ver = self._version_badge(e)
            cards += (f'<div class=card><a href="/episode/{esc(e["slug"])}"><b>{esc(e["title"])}</b></a>{ver}'
                      f'<br><small>{esc(e["summary"])}</small></div>')
        self._send(200, page("播客主页", f"<h1>播客</h1><p>{chips}</p><h2>最新单集</h2>{cards or '<p>暂无</p>'}"))

    def _version_badge(self, e):
        if e["transcript_only"] or not e["revision_id"]:
            return '<span class=badge>文字稿版</span>'
        r = self.db.execute("SELECT version FROM audio_revisions WHERE id=?", (e["revision_id"],)).fetchone()
        return f'<span class=badge>音频 v{r["version"]}</span>' if r else ""

    def _topic(self, slug):
        t = self.db.execute("SELECT * FROM topics WHERE slug=?", (slug,)).fetchone()
        if not t: return self._send(404, page("404", "<h1>话题不存在</h1>"))
        eps = self.db.execute(
            "SELECT e.*, cv.revision_id, cv.transcript_only FROM episodes e"
            " JOIN episode_topics et ON et.episode_id=e.id"
            " JOIN channel_versions cv ON cv.episode_id=e.id JOIN channels c ON c.id=cv.channel_id"
            " WHERE et.topic_id=? AND e.status='published' AND c.code='public'", (t["id"],)).fetchall()
        cards = "".join(f'<div class=card><a href="/episode/{esc(e["slug"])}">{esc(e["title"])}</a>{self._version_badge(e)}</div>' for e in eps)
        self._send(200, page(t["name"], f"<h1>话题：{esc(t['name'])}</h1>{cards or '<p>暂无单集</p>'}"))

    def _episode(self, slug, q):
        e = self.db.execute("SELECT * FROM episodes WHERE slug=?", (slug,)).fetchone()
        if not e or e["status"] != "published":
            return self._send(404, page("未发布", "<h1>单集不存在或未公开</h1>"))
        channel = q.get("channel", ["public"])[0]
        cv = self.db.execute("SELECT cv.*, c.code FROM channel_versions cv JOIN channels c ON c.id=cv.channel_id"
                             " WHERE cv.episode_id=? AND c.code=?", (e["id"], channel)).fetchone()
        if not cv:
            cv = self.db.execute("SELECT cv.*, c.code FROM channel_versions cv JOIN channels c ON c.id=cv.channel_id"
                                 " WHERE cv.episode_id=? AND c.code='public'", (e["id"],)).fetchone()
        guests = self.db.execute("SELECT g.* FROM guests g JOIN episode_guests eg ON eg.guest_id=g.id"
                                 " WHERE eg.episode_id=?", (e["id"],)).fetchall()
        rev = self.db.execute("SELECT * FROM audio_revisions WHERE id=?", (cv["revision_id"],)).fetchone() \
              if cv["revision_id"] else core.current_revision(self.db, e["id"])
        ann = self.db.execute("SELECT * FROM annotations WHERE revision_id=? AND status='active'"
                              " ORDER BY start_ms", (rev["id"],)).fetchall() if rev else []
        chapters = [a for a in ann if a["type"] == "chapter"]
        subs = [a for a in ann if a["type"] == "subtitle"]
        clips = [a for a in ann if a["type"] == "guest_clip"]
        # 实际渠道版本呈现
        if cv["transcript_only"] or not cv["revision_id"]:
            media = '<div class=warn>本期音频暂未上架，当前为<b>文字稿回退</b>版本。</div>'
            ver_info = f"渠道 {esc(cv['code'])} · 文字稿版"
        else:
            media = (f'<audio controls src="{esc(rev["media_url"])}"></audio>' if rev["media_url"]
                     else '<div class=warn>音频转码中，暂以文字呈现。</div>')
            ver_info = f"渠道 {esc(cv['code'])} · 音频版本 v{rev['version']} · 时长 {fmt_ms(rev['duration_ms'])}"
        ch_html = "".join(f'<div class=cue><b>{fmt_ms(a["start_ms"])}</b>◆ {esc(a["text"])}</div>' for a in chapters)
        sub_html = "".join(f'<div class=cue><b>{fmt_ms(a["start_ms"])}–{fmt_ms(a["end_ms"])}</b>{esc(a["text"])}</div>' for a in subs)
        clip_html = "".join(f'<div class=cue><b>{fmt_ms(a["start_ms"])}–{fmt_ms(a["end_ms"])}</b>授权片段：{esc(a["text"])}</div>' for a in clips)
        g_html = " ".join(f'<span class=badge>{esc(g["name"])}</span>' for g in guests)
        pending = self.db.execute("SELECT COUNT(*) n FROM annotations WHERE episode_id=? AND status='pending_repair'",
                                  (e["id"],)).fetchone()["n"]
        body = (f"<h1>{esc(e['title'])}</h1><p><small>{ver_info}</small></p><p>{g_html}</p>"
                f"<p>{esc(e['summary'])}</p>{media}"
                f"<h2>章节</h2>{ch_html or '<p>无</p>'}<h2>文字稿</h2>{sub_html or '<p>无</p>'}"
                f"<h2>嘉宾授权片段</h2>{clip_html or '<p>无</p>'}"
                + (f'<p class=warn>有 {pending} 条注释待修复（重新剪辑后未能自动映射）。</p>' if pending else ""))
        self._send(200, page(e["title"], body))

    def _player_api(self, slug, q):
        e = self.db.execute("SELECT * FROM episodes WHERE slug=? AND status='published'", (slug,)).fetchone()
        if not e: return self._json({"error": "not_found"}, 404)
        channel = q.get("channel", ["public"])[0]
        cv = self.db.execute("SELECT cv.* FROM channel_versions cv JOIN channels c ON c.id=cv.channel_id"
                             " WHERE cv.episode_id=? AND c.code=?", (e["id"], channel)).fetchone()
        if not cv or not cv["revision_id"]:
            return self._json({"episode_id": e["id"], "transcript_only": True})
        rev = self.db.execute("SELECT * FROM audio_revisions WHERE id=?", (cv["revision_id"],)).fetchone()
        ann = self.db.execute("SELECT type,start_ms,end_ms,text FROM annotations WHERE revision_id=?"
                              " AND status='active' ORDER BY start_ms", (rev["id"],)).fetchall()
        self._json({"episode_id": e["id"], "revision_id": rev["id"], "version": rev["version"],
                    "media_url": rev["media_url"], "duration_ms": rev["duration_ms"],
                    "subtitles": [dict(a) for a in ann if a["type"] == "subtitle"],
                    "chapters": [dict(a) for a in ann if a["type"] == "chapter"]})

    def _search(self, q):
        kw = q.get("q", [""])[0].strip()
        res = core.search(self.db, kw) if kw else []
        items = "".join(f'<div class=card><a href="/episode/{esc(r["slug"])}">{esc(r["title"])}</a>'
                        f'<br><small>{r["type"]}: {r["snippet"]}</small></div>' for r in res)
        body = (f'<h1>搜索</h1><form><input name=q value="{esc(kw)}" placeholder="搜索已发布内容…"><button>搜索</button></form>'
                + (items or ("<p>无结果</p>" if kw else "")))
        self._send(200, page("搜索", body))

    def _contact(self):
        self._send(200, page("商务合作", """
<h1>商务合作</h1><p>此表单进入商务收件箱，与公开档案权限分离，内容不会公开。</p>
<form method=post action="/api/inquiries">
<input name=name placeholder="称呼" required><input name=email placeholder="邮箱" required>
<textarea name=message placeholder="合作意向" required></textarea><button>提交</button></form>"""))

    def _post_inquiry(self, d):
        if not (d.get("name") and d.get("email") and d.get("message")):
            return self._json({"error": "missing fields"}, 400)
        self.db.execute("INSERT INTO inquiries(name,email,message,created_at) VALUES(?,?,?,?)",
                        (d["name"], d["email"], d["message"], core.now()))
        self.db.commit()
        self._send(200, page("已提交", "<h1>已收到，我们会尽快联系你。</h1><p><a href='/'>返回首页</a></p>"))

    def _inquiries(self, q):
        if not self._admin_ok(q): return self._json({"error": "forbidden"}, 403)
        rows = self.db.execute("SELECT * FROM inquiries ORDER BY id DESC").fetchall()
        self._json([dict(r) for r in rows])

    def _rss(self, channel):
        xml, hit = core.get_rss(self.db, channel)
        self._send(200, xml, "application/rss+xml; charset=utf-8", {"X-Cache": "HIT" if hit else "MISS"})

    # ---------- 管理后台 ----------
    def _admin(self, p, q):
        if not self._admin_ok(q):
            return self._send(403, page("需要密钥", "<h1>403</h1><p>请提供管理密钥 ?key=…</p>"))
        k = f"?key={esc(q.get('key',[ADMIN_KEY])[0])}"
        if p == "/admin":
            eps = self.db.execute("SELECT * FROM episodes ORDER BY id DESC").fetchall()
            rows = "".join(f"<tr><td>{e['id']}</td><td><a href='/admin/episodes/{e['id']}{k}'>{esc(e['title'])}</a></td>"
                           f"<td>{e['status']}</td><td>{esc(e['policy_code'])}</td></tr>" for e in eps)
            pend = self.db.execute("SELECT COUNT(*) n FROM annotations WHERE status='pending_repair'").fetchone()["n"]
            inq = self.db.execute("SELECT COUNT(*) n FROM inquiries").fetchone()["n"]
            body = (f"<h1>管理后台</h1><p><a href='/admin/pending{k}'>待修复注释 ({pend})</a> · "
                    f"<a href='/admin/inquiries{k}'>商务收件箱 ({inq})</a> · "
                    f"<a href='/admin/events{k}'>流水线/回调事件</a> · <a href='/admin/new{k}'>新建单集</a></p>"
                    f"<table><tr><th>ID</th><th>单集</th><th>状态</th><th>策略</th></tr>{rows}</table>")
            return self._send(200, page("管理", body))
        m = re.match(r"^/admin/episodes/(\d+)$", p)
        if m: return self._admin_episode(int(m.group(1)), k)
        if p == "/admin/new":
            return self._send(200, page("新建", f"""<h1>新建单集</h1><form method=post action="/api/admin/episodes{k}">
标题<input name=title required>slug<input name=slug required>简介<textarea name=summary></textarea>
策略<select name=policy_code><option value=hold>缺音频→暂缓发布</option><option value=transcript_public>缺音频→公开文字稿</option></select>
<button>创建</button></form>"""))
        if p == "/admin/pending":
            rows = self.db.execute("SELECT a.*, e.title FROM annotations a JOIN episodes e ON e.id=a.episode_id"
                                   " WHERE a.status='pending_repair' ORDER BY a.id").fetchall()
            trs = "".join(f"<tr class=pending><td>{a['id']}</td><td>{esc(a['title'])}</td><td>{a['type']}</td>"
                          f"<td>{esc(a['reason'])}</td><td>{fmt_ms(a['orig_start_ms'])}–{fmt_ms(a['orig_end_ms'])}</td>"
                          f"<td>{esc(a['text'])}</td><td>"
                          f"<form style='display:inline' method=post action='/api/admin/annotations/{a['id']}/resolve{k}'>"
                          f"<input name=start_ms size=6 placeholder=新起ms><input name=end_ms size=6 placeholder=新止ms>"
                          f"<button name=action value=fix>修复</button>"
                          f"<button name=action value=drop>丢弃</button></form></td></tr>" for a in rows)
            return self._send(200, page("待修复", f"<h1>待修复注释</h1><table><tr><th>ID</th><th>单集</th><th>类型</th>"
                                          f"<th>原因</th><th>旧时间码</th><th>文本</th><th>操作</th></tr>{trs}</table>"))
        if p == "/admin/inquiries":
            rows = self.db.execute("SELECT * FROM inquiries ORDER BY id DESC").fetchall()
            trs = "".join(f"<tr><td>{r['id']}</td><td>{esc(r['name'])}</td><td>{esc(r['email'])}</td>"
                          f"<td>{esc(r['message'])}</td></tr>" for r in rows)
            return self._send(200, page("商务收件箱", f"<h1>商务收件箱（仅管理员）</h1><table>{trs}</table>"))
        if p == "/admin/events":
            ev = self.db.execute("SELECT * FROM publish_events ORDER BY id DESC LIMIT 50").fetchall()
            pe = self.db.execute("SELECT * FROM player_events ORDER BY id DESC LIMIT 50").fetchall()
            t1 = "".join(f"<tr><td>{e['id']}</td><td>{e['episode_id']}</td><td>{e['stage']}</td>"
                         f"<td>{e['status']}</td><td>{esc(e['detail'])}</td></tr>" for e in ev)
            t2 = "".join(f"<tr><td>{r['id']}</td><td>{r['episode_id']}</td><td>rev {r['revision_id']}</td>"
                         f"<td>{r['event']}</td><td>{'应用' if r['applied'] else '忽略'}</td><td>{esc(r['note'])}</td></tr>" for r in pe)
            return self._send(200, page("事件", f"<h1>流水线事件</h1><table>{t1}</table><h1>播放器回调</h1><table>{t2}</table>"))
        self._send(404, page("404", "<h1>404</h1>"))

    def _admin_episode(self, eid, k):
        e = self.db.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()
        if not e: return self._send(404, page("404", "<h1>404</h1>"))
        revs = self.db.execute("SELECT * FROM audio_revisions WHERE episode_id=? ORDER BY version", (eid,)).fetchall()
        guests = self.db.execute("SELECT g.*, l.status lic, l.id lid FROM episode_guests eg JOIN guests g ON g.id=eg.guest_id"
                                 " LEFT JOIN licenses l ON l.guest_id=g.id AND l.episode_id=? AND l.scope='full'"
                                 " WHERE eg.episode_id=?", (eid, eid)).fetchall()
        rev_rows = "".join(f"<tr><td>v{r['version']}</td><td>{r['state']}</td><td>{esc(r['media_url'] or '')}</td>"
                           f"<td>{fmt_ms(r['duration_ms'])}</td></tr>" for r in revs)
        g_rows = "".join(f"<tr><td>{esc(g['name'])}</td><td>{g['lic'] or '无授权'}</td><td>"
                         f"<form style='display:inline' method=post action='/api/admin/licenses{k}'>"
                         f"<input type=hidden name=guest_id value={g['id']}><input type=hidden name=episode_id value={eid}>"
                         f"<button name=status value=granted>授权</button>"
                         f"<button name=status value=revoked>撤销</button></form></td></tr>" for g in guests)
        body = f"""<h1>单集 #{eid} {esc(e['title'])}</h1><p>状态:{e['status']} 策略:{esc(e['policy_code'])}</p>
<h2>音轨修订</h2><table><tr><th>版本</th><th>状态</th><th>媒体</th><th>时长</th></tr>{rev_rows}</table>
<h2>登记新修订（重新剪辑/换音轨）</h2>
<form method=post action="/api/admin/episodes/{eid}/revisions{k}">
媒体URL<input name=media_url placeholder="留空=缺音频">时长ms<input name=duration_ms value="0">
编辑映射 ops(JSON)<textarea name=ops placeholder='[{{"op":"delete","start":10000,"end":15000}}]'>[]</textarea>
<button>登记并重定位注释</button></form>
<h2>文字稿/注释（最新修订）</h2>
<form method=post action="/api/admin/episodes/{eid}/annotations{k}">
类型<select name=type><option>subtitle</option><option>chapter</option><option>guest_clip</option></select>
起ms<input name=start_ms value="0">止ms<input name=end_ms value="1000">文本<input name=text>
<button>添加</button></form>
<h2>嘉宾与授权</h2><table><tr><th>嘉宾</th><th>授权</th><th>操作</th></tr>{g_rows}</table>
<form method=post action="/api/admin/episodes/{eid}/guests{k}">嘉宾ID<input name=guest_id><button>添加嘉宾</button></form>
<h2>发布</h2>
<form style='display:inline' method=post action="/api/admin/episodes/{eid}/publish{k}"><button>立即发布</button></form>
<form style='display:inline' method=post action="/api/admin/episodes/{eid}/schedule{k}">
<input name=run_at placeholder="unix时间戳" size=12><button>定时上线</button></form>
<form style='display:inline' method=post action="/api/admin/episodes/{eid}/promote{k}">
<input name=revision_id placeholder="修订ID" size=6><button>切换渠道版本</button></form>
<p><a href="/admin{k}">← 返回</a></p>"""
        self._send(200, page(f"单集 {eid}", body))

    def _admin_api(self, p, d, q):
        if not self._admin_ok(q): return self._json({"error": "forbidden"}, 403)
        def back(eid=None):
            k = f"?key={esc(q.get('key',[ADMIN_KEY])[0])}"
            tgt = f"/admin/episodes/{eid}{k}" if eid else f"/admin{k}"
            self._send(303, b"", headers={"Location": tgt})
        m = re.match(r"^/api/admin/episodes/(\d+)/revisions", p)
        if m:
            eid = int(m.group(1))
            ops = json.loads(d.get("ops") or "[]")
            core.add_revision(self.db, eid, d.get("media_url") or None,
                              int(d.get("duration_ms") or 0), ops)
            return back(eid)
        m = re.match(r"^/api/admin/episodes/(\d+)/annotations", p)
        if m:
            eid = int(m.group(1))
            rev = core.current_revision(self.db, eid)
            if not rev: rev_id = core.add_revision(self.db, eid)
            else: rev_id = rev["id"]
            core.add_annotation(self.db, eid, rev_id, d.get("type", "subtitle"),
                                int(d.get("start_ms", 0)), int(d.get("end_ms", 0)), d.get("text", ""))
            return back(eid)
        m = re.match(r"^/api/admin/episodes/(\d+)/guests", p)
        if m:
            eid = int(m.group(1))
            self.db.execute("INSERT OR IGNORE INTO episode_guests(episode_id,guest_id) VALUES(?,?)",
                            (eid, int(d["guest_id"]))); self.db.commit(); return back(eid)
        m = re.match(r"^/api/admin/episodes/(\d+)/publish", p)
        if m:
            r = core.publish_episode(self.db, int(m.group(1))); return back(int(m.group(1)))
        m = re.match(r"^/api/admin/episodes/(\d+)/schedule", p)
        if m:
            core.schedule_publish(self.db, int(m.group(1)), float(d.get("run_at") or core.now())); return back(int(m.group(1)))
        m = re.match(r"^/api/admin/episodes/(\d+)/promote", p)
        if m:
            core.promote_revision(self.db, int(m.group(1)), int(d["revision_id"])); return back(int(m.group(1)))
        m = re.match(r"^/api/admin/annotations/(\d+)/resolve", p)
        if m:
            aid = int(m.group(1))
            if d.get("action") == "drop":
                core.resolve_annotation(self.db, aid, "drop")
            else:
                core.resolve_annotation(self.db, aid, "fix", int(d.get("start_ms") or 0),
                                        int(d.get("end_ms") or 0), d.get("text") or None)
            k = f"?key={esc(q.get('key',[ADMIN_KEY])[0])}"
            return self._send(303, b"", headers={"Location": f"/admin/pending{k}"})
        if p == "/api/admin/episodes":
            eid = core.create_episode(self.db, d["slug"], d["title"], d.get("summary", ""),
                                      d.get("policy_code", "hold"))
            return back(eid)
        if p == "/api/admin/licenses":
            core.set_license(self.db, int(d["guest_id"]), int(d["episode_id"]), d["status"]); return back(int(d["episode_id"]))
        if p == "/api/admin/scheduler/run":
            return self._json(core.run_scheduler(self.db))
        self._json({"error": "not_found"}, 404)

def make_server(db, port=8000):
    cls = type("BoundApp", (App,), {"db": db, "lock": threading.RLock()})
    return ThreadingHTTPServer(("127.0.0.1", port), cls)
