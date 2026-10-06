"""HTTP-level tests via the App router directly (no sockets)."""
import json
import os
import sys
import tempfile
import unittest
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, auth, admin as a, services, publish  # noqa
from app.server import app  # noqa


def req(method, path, form=None, headers=None, query=None):
    body = urlencode(form or {}).encode()
    h = {"Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(body))}
    h.update(headers or {})
    r = {"method": method, "path": path, "headers": h, "body": body,
         "query": query or {}, "form": {k: [str(v)] for k, v in (form or {}).items()}}
    return app.handle(r)


def json_req(method, path, payload, headers=None):
    body = json.dumps(payload).encode()
    h = {"Content-Type": "application/json", "Content-Length": str(len(body))}
    h.update(headers or {})
    r = {"method": method, "path": path, "headers": h, "body": body, "query": {}, "form": {}}
    return app.handle(r)


class HttpCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db.init_db(os.path.join(cls.tmp.name, "http.db"))
        with db.tx() as c:
            publish.get_or_create_default_channel(c)
        c = db.get_conn()
        c.execute("INSERT INTO hosts(slug,name,bio) VALUES('h','主持','')")
        cls.tid = a.create_topic("AI", "人工智能")
        cls.gid = a.create_guest("周远", "研究员")
        auth.create_user("admin", "pw")
        cls.eid, cls.v1 = cls._episode(do_publish=True)

    @classmethod
    def _episode(cls, do_publish=True, immutable=1):
        eid = a.create_episode("HTTP 单集", 1, cls.tid, "", "hold", immutable)
        a.attach_guest(eid, cls.gid)
        mid = services.add_media(eid, "https://cdn/http.mp3", 60000, license="owned")
        a.add_transcript_line(eid, "host", 0, 5000, "开场内容delta")
        a.add_transcript_line(eid, f"guest:{cls.gid}", 8000, 15000, "嘉宾片段epsilon")
        a.set_consent(eid, cls.gid, 8000, 15000, "授权")
        if do_publish:
            publish.advance_jobs(eid)
            publish.publish_episode(eid)
            publish.publish_to_channels(eid)
        return eid, mid

    @classmethod
    def tearDownClass(cls):
        db.get_conn().close()
        db._conn = None
        cls.tmp.cleanup()

    def test_01_public_pages(self):
        for p in ("/", "/t/ai", "/search?q=delta", "/business"):
            st, _, body = req("GET", p)
            self.assertEqual(st, 200, p)
            self.assertIn(b"PodHost", body)
        st, _, body = req("GET", "/feed.xml")
        self.assertEqual(st, 200)
        self.assertIn(b"<rss", body)

    def test_02_admin_requires_login(self):
        st, _, _ = req("GET", "/admin")
        self.assertEqual(st, 401)
        st, _, _ = req("GET", "/admin/inquiries")
        self.assertEqual(st, 401)

    def test_03_login_and_inquiries(self):
        st, headers, _ = req("POST", "/admin/login", {"username": "admin", "password": "pw"})
        self.assertEqual(st, 303)
        cookie = headers["Set-Cookie"].split(";")[0]
        st, _, body = req("GET", "/admin/inquiries", headers={"Cookie": cookie})
        self.assertEqual(st, 200)
        # 匿名提交商务表单
        st2, _, _ = req("POST", "/business",
                        {"name": "品牌方", "contact": "x@y", "body": "赞助未发布节目？"})
        self.assertEqual(st2, 200)
        # 商务内容不出现在公开搜索
        st3, _, body = req("GET", "/search", query={"q": ["赞助"]})
        self.assertNotIn("赞助未发布节目", body.decode())
        # 登录后可见
        _, _, body = req("GET", "/admin/inquiries", headers={"Cookie": cookie})
        self.assertIn("赞助未发布节目", body.decode())
        return cookie

    def test_04_rss_conditional_304_then_etag_bump(self):
        st, h1, body1 = req("GET", "/feed.xml")
        etag = h1["ETag"]
        self.assertEqual(st, 200)
        # 客户端带 If-None-Match：缓存仍新鲜 -> 304 空体（“RSS 缓存未刷新”）
        st, h2, body2 = req("GET", "/feed.xml", headers={"If-None-Match": etag})
        self.assertEqual(st, 304)
        self.assertEqual(body2, b"")
        # 可更新地址模式单集重剪 -> etag 变化，条件请求不再 304
        eid2, mid2 = self.__class__._episode(do_publish=True, immutable=0)
        services.reedit_episode(eid2, "https://cdn/http.mp3", [
            {"kind": "keep", "src_start": 0, "src_end": 60000}])
        st, h3, body3 = req("GET", "/feed.xml", headers={"If-None-Match": etag})
        self.assertEqual(st, 200)
        self.assertNotEqual(h3["ETag"], etag)

    def test_05_player_stale_callback_api(self):
        slug = db.q1("SELECT slug FROM episodes WHERE id=?", (self.eid,))["slug"]
        st, _, body = req("GET", f"/api/current-episode?slug={slug}",
                          query={"slug": [slug]})
        self.assertEqual(st, 200)
        # 重剪产生新版本
        services.reedit_episode(self.eid, "https://cdn/http-v2.mp3", [
            {"kind": "keep", "src_start": 0, "src_end": 60000}])
        v2 = db.q1("SELECT current_media_id FROM episodes WHERE id=?",
                   (self.eid,))["current_media_id"]
        # frozen 渠道版本 v1 仍有效；重新发布后旧回调被拒
        st, _, body = json_req("POST", f"/api/episodes/{self.eid}/progress",
                               {"media_id": self.v1, "position_ms": 12345})
        self.assertEqual(st, 200)
        publish.republish_episode(self.eid)
        st, _, body = json_req("POST", f"/api/episodes/{self.eid}/progress",
                               {"media_id": self.v1, "position_ms": 12345})
        self.assertEqual(st, 409)
        self.assertFalse(json.loads(body)["accepted"])
        # 新回调
        st, _, body = json_req("POST", f"/api/episodes/{self.eid}/progress",
                               {"media_id": v2, "position_ms": 12345})
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(body)["accepted"])

    def test_06_public_page_shows_channel_version(self):
        slug = db.q1("SELECT slug FROM episodes WHERE id=?", (self.eid,))["slug"]
        st, _, body = req("GET", f"/e/{slug}")
        self.assertEqual(st, 200)
        text = body.decode()
        # 固定版本：经过 05 的重新发布后显示 v2；否则显示 v1——总之是“实际渠道版本”
        self.assertTrue("渠道实际版本 v" in text)
        self.assertIn("固定媒体版本", text)

    def test_07_search_does_not_leak_unpublished(self):
        eid2, _ = self.__class__._episode(do_publish=False)
        # 给草稿单集追加一条独有关键词的嘉宾行
        a.add_transcript_line(eid2, f"guest:{self.gid}", 20000, 25000,
                              "草稿独家保密片段zetakappa")
        seg = a.set_consent(eid2, self.gid, 20000, 25000, "授权")
        _, _, body = req("GET", "/search", query={"q": ["zetakappa"]})
        self.assertNotIn("草稿独家保密片段", body.decode())


if __name__ == "__main__":
    unittest.main(verbosity=2)
