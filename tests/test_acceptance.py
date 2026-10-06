"""验收测试：五个指定场景 + 发布策略 + 权限/搜索隔离 + 固定版本/可更新地址对比。

每个测试类使用独立临时数据库。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, auth, admin as a, services, publish, public as public_svc  # noqa


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(os.path.join(self.tmp.name, "test.db"))
        with db.tx() as c:
            publish.get_or_create_default_channel(c)
        self._seed()

    def tearDown(self):
        db.get_conn().close()
        db._conn = None
        self.tmp.cleanup()

    def _seed(self):
        c = db.get_conn()
        c.execute("INSERT INTO hosts(slug,name,bio) VALUES('h','主持','')")
        self.tid = a.create_topic("AI", "人工智能")
        self.gid = a.create_guest("周远", "研究员")
        auth.create_user("admin", "pw")

    def make_episode(self, policy="hold", immutable=1, duration=60000, do_publish=False):
        eid = a.create_episode("测试单集", 1, self.tid, "摘要", policy, immutable)
        a.attach_guest(eid, self.gid)
        mid = services.add_media(eid, "https://cdn/a.mp3", duration, license="owned")
        a.add_transcript_line(eid, "host", 0, 8000, "主持人开场发言内容可搜索关键词alpha")
        a.add_transcript_line(eid, f"guest:{self.gid}", 8000, 15000,
                              "嘉宾独家访谈片段关键词beta未公开前不可搜到")
        a.add_chapter(eid, 0, "开场")
        a.add_chapter(eid, 8000, "嘉宾段落")
        a.add_annotation(eid, 9000, 12000, "核对数据", "factcheck")
        a.set_consent(eid, self.gid, 8000, 29000, "嘉宾授权")
        if do_publish:
            publish.advance_jobs(eid)
            publish.publish_episode(eid)
            publish.publish_to_channels(eid)
        return eid, mid


class TestDeleteGuestSpeech(BaseCase):
    """验收1：删掉一段嘉宾发言 → 时间码重定位，不能保留旧秒数；不能映射的注释进待修复。"""

    def test_cut_guest_segment(self):
        eid, mid = self.make_episode()
        # 删除嘉宾发言 10–15s
        res = services.reedit_episode(eid, "https://cdn/a-v2.mp3", [
            {"kind": "keep", "src_start": 0, "src_end": 10000},
            {"kind": "cut", "src_start": 10000, "src_end": 15000},
            {"kind": "keep", "src_start": 15000, "src_end": 60000},
        ])
        self.assertEqual(res["new_duration"], 55000)
        cur = db.q1("SELECT current_media_id FROM episodes WHERE id=?", (eid,))["current_media_id"]

        # 主持人开场行完整落在保留区 -> 平移（值仍为 0-8000）
        host_line = db.q1(
            "SELECT * FROM transcript_lines WHERE media_id=? AND speaker='host'", (cur,))
        self.assertEqual((host_line["start_ms"], host_line["end_ms"]), (0, 8000))
        self.assertEqual(host_line["status"], "active")

        # 嘉宾行 8–15s 横跨切口 -> broken：当前时间码置空，旧值保留供修复
        gl = db.q1(
            "SELECT * FROM transcript_lines WHERE media_id=? AND speaker LIKE 'guest:%'", (cur,))
        self.assertEqual(gl["status"], "broken")
        self.assertIsNone(gl["start_ms"])
        self.assertIsNone(gl["end_ms"])
        self.assertEqual((gl["orig_start_ms"], gl["orig_end_ms"]), (8000, 15000))
        # 绝不允许照搬旧秒数：broken 行不能还挂着旧时间码
        self.assertFalse(db.q1(
            "SELECT COUNT(*) c FROM transcript_lines WHERE status='broken' AND start_ms IS NOT NULL",
            ())["c"])

        # 章节 8000 落在保留区且 < 10000 -> 映射保留
        ch0 = db.q1("SELECT * FROM chapters WHERE media_id=? AND title='开场'", (cur,))
        self.assertEqual(ch0["status"], "active")
        self.assertEqual(ch0["start_ms"], 0)

        # 注释 9–12s 横跨切口 -> 待修复
        an = db.q1("SELECT * FROM annotations WHERE media_id=?", (cur,))
        self.assertEqual(an["status"], "broken")
        self.assertIsNone(an["start_ms"])

        # 待修复队列里有该注释和字幕
        rq = services.repair_queue(eid)
        self.assertTrue(any(x["id"] == an["id"] for x in rq["annotations"]))
        self.assertTrue(any(x["id"] == gl["id"] for x in rq["transcript"]))

        # 人工修复后重新激活
        services.fix_item("annotations", an["id"], 9000, 9500)
        self.assertEqual(db.q1("SELECT status FROM annotations WHERE id=?",
                               (an["id"],))["status"], "active")

        # 时长缩短 5s，后续映射点平移
        self.assertEqual(db.q1("SELECT duration_ms FROM media_assets WHERE id=?",
                               (cur,))["duration_ms"], 55000)


class TestSwapTrack(BaseCase):
    """验收2：换音轨（replace 段）→ 落入新区间的字幕/章节/授权不可映射，进待修复。"""

    def test_replace_track(self):
        eid, mid = self.make_episode()
        res = services.reedit_episode(eid, "https://cdn/a-replaced.mp3", [
            {"kind": "keep", "src_start": 0, "src_end": 8000},
            {"kind": "replace", "src_start": 8000, "src_end": 20000,
             "dst_start": 8000, "dst_end": 20000},
            {"kind": "keep", "src_start": 20000, "src_end": 60000},
        ])
        cur = res["new_media_id"]
        gl = db.q1(
            "SELECT * FROM transcript_lines WHERE media_id=? AND speaker LIKE 'guest:%'", (cur,))
        self.assertEqual(gl["status"], "broken")      # 嘉宾发言在被替换区
        host = db.q1(
            "SELECT * FROM transcript_lines WHERE media_id=? AND speaker='host'", (cur,))
        self.assertEqual(host["status"], "active")    # 开场行保留
        # 授权片段 8–29s 横跨 replace 起点 -> broken（不会把旧授权复制到新音轨）
        cs = db.q1("SELECT * FROM consent_segments WHERE media_id=?", (cur,))
        self.assertEqual(cs["status"], "broken")
        self.assertIsNone(cs["start_ms"])
        # 注释 9–12s 在替换区 -> broken
        self.assertEqual(db.q1(
            "SELECT status FROM annotations WHERE media_id=?", (cur,))["status"], "broken")


class TestScheduledRevocation(BaseCase):
    """验收3：定时上线时授权被撤销 → 到点执行器再次过闸门，保持 blocked，不会上线。"""

    def test_revoke_before_due(self):
        eid, mid = self.make_episode()
        publish.advance_jobs(eid)
        publish.publish_episode(eid, publish_at="2026-10-10T08:00:00Z")
        self.assertEqual(db.q1("SELECT status FROM episodes WHERE id=?", (eid,))["status"],
                         "scheduled")
        # 定时之后、到点之前，嘉宾撤销授权（针对当前版本上的 granted 片段）
        seg = db.q1("SELECT id FROM consent_segments WHERE media_id=? AND consent_status='granted'",
                    (mid,))
        a.revoke_consent(seg["id"])
        # 到点执行
        results = publish.run_scheduled("2026-10-10T08:00:00Z")
        self.assertEqual(results[0]["status"], "blocked")
        ep = db.q1("SELECT * FROM episodes WHERE id=?", (eid,))
        self.assertEqual(ep["status"], "blocked")
        self.assertIn("撤销", ep["block_reason"])
        # 未上线：公开页 404
        page = public_svc.episode_public(db.q1("SELECT slug FROM episodes WHERE id=?",
                                               (eid,))["slug"])
        self.assertTrue(page.get("forbidden"))

    def test_revoke_after_publish_takes_audio_down(self):
        """已发布后撤销音轨许可：音频立即撤下，渠道 enclosure 清空（文字回退）。"""
        eid, mid = self.make_episode(do_publish=True)
        page = public_svc.episode_public(db.q1("SELECT slug FROM episodes WHERE id=?",
                                               (eid,))["slug"])
        self.assertIsNotNone(page["audio"])
        services.revoke_license(mid, "版权争议")
        page = public_svc.episode_public(db.q1("SELECT slug FROM episodes WHERE id=?",
                                               (eid,))["slug"])
        # 许可撤销 -> 单集 blocked，公开页不再提供；渠道 enclosure 已清空
        self.assertTrue(page.get("forbidden"))
        self.assertNotIn("audio", page)
        ep = db.q1("SELECT status FROM episodes WHERE id=?", (eid,))
        self.assertEqual(ep["status"], "blocked")
        ce = db.q1("SELECT * FROM channel_episodes WHERE episode_id=?", (eid,))
        self.assertIsNone(ce["enclosure_url"])     # 渠道条目变为文字回退


class TestRssCache(BaseCase):
    """验收4：RSS 缓存未刷新（条件请求 304）；两种媒体模式的刷新语义不同。"""

    def test_frozen_keeps_old_version_in_feed(self):
        """固定媒体版本：重剪后渠道 feed 仍是旧 v1，历史引用不变，直到人工重新发布。"""
        eid, v1 = self.make_episode(do_publish=True)
        services.reedit_episode(eid, "https://cdn/a-v2.mp3", [
            {"kind": "keep", "src_start": 0, "src_end": 60000},
        ])
        ce = db.q1("SELECT * FROM channel_episodes WHERE episode_id=?", (eid,))
        self.assertEqual(ce["media_id"], v1)               # 渠道仍是 v1
        self.assertEqual(ce["media_mode"], "frozen")
        feed = publish.render_rss()
        self.assertIn("channel-media-version v1", feed)   # feed 呈现实际渠道版本
        self.assertNotIn("channel-media-version v2", feed)
        # 运营确认后重新发布 -> v2
        publish.republish_episode(eid)
        feed = publish.render_rss()
        self.assertIn("channel-media-version v2", feed)
        self.assertIn("?v=2", feed)                       # 版本化 URL，旧 URL 历史引用仍有效

    def test_current_url_updates_audio(self):
        """永久地址可更新：guid/enclosure URL 不变，但底层切到 v2，etag 变化驱动缓存刷新。"""
        eid, v1 = self.make_episode(do_publish=False, immutable=0)
        publish.advance_jobs(eid)
        publish.publish_episode(eid)
        publish.publish_to_channels(eid)
        ch_before = db.q1("SELECT etag FROM channels WHERE slug='main'")["etag"]
        services.reedit_episode(eid, "https://cdn/perm.mp3", [
            {"kind": "keep", "src_start": 0, "src_end": 60000},
        ])
        ce = db.q1("SELECT * FROM channel_episodes WHERE episode_id=?", (eid,))
        self.assertEqual(ce["media_mode"], "current")
        self.assertEqual(ce["enclosure_url"], "https://cdn/perm.mp3")  # 地址稳定
        self.assertNotEqual(ce["media_id"], v1)
        ch_after = db.q1("SELECT etag FROM channels WHERE slug='main'")["etag"]
        self.assertNotEqual(ch_before, ch_after)                        # etag 变 -> 缓存可刷新
        feed = publish.render_rss()
        self.assertIn("channel-media-version v2", feed)
        self.assertNotIn("?v=", feed)                    # 可更新模式不做版本化 URL


class TestStalePlayerCallback(BaseCase):
    """验收5：重剪后播放器旧回调到达 -> 拒绝（409），当前版本回调接受。"""

    def test_stale_callback_rejected_current_mode(self):
        # 可更新永久地址：重剪后旧音轨回调（旧秒数）必须被拒绝
        eid, v1 = self.make_episode(do_publish=False, immutable=0)
        publish.advance_jobs(eid)
        publish.publish_episode(eid)
        publish.publish_to_channels(eid)
        v1_etag = db.q1("SELECT etag FROM media_assets WHERE id=?", (v1,))["etag"]
        ok = publish.player_callback(eid, v1, 12000)
        self.assertTrue(ok["accepted"])
        services.reedit_episode(eid, "https://cdn/a-v2.mp3", [
            {"kind": "keep", "src_start": 0, "src_end": 60000}])
        v2 = db.q1("SELECT current_media_id FROM episodes WHERE id=?", (eid,))["current_media_id"]
        # 旧 media_id + 旧秒数晚到 -> 409
        stale = publish.player_callback(eid, v1, 12000)
        self.assertFalse(stale["accepted"])
        self.assertEqual(stale["code"], 409)
        # 新 id 但带旧 etag（客户端缓存旧副本）-> 409
        stale2 = publish.player_callback(eid, v2, 12000, client_etag=v1_etag)
        self.assertFalse(stale2["accepted"])
        # 当前版本 -> 接受
        self.assertTrue(publish.player_callback(eid, v2, 12000)["accepted"])
        self.assertGreaterEqual(db.q1(
            "SELECT COUNT(*) c FROM player_events WHERE accepted=0 AND episode_id=?",
            (eid,))["c"], 2)

    def test_frozen_mode_old_callback_etag_guards(self):
        # 固定媒体版本：渠道冻结在 v1，v1 回调仍合法；但版本化 URL 之外的旧 etag 拒绝
        eid, v1 = self.make_episode(do_publish=True)
        self.assertTrue(publish.player_callback(eid, v1, 12000)["accepted"])
        services.reedit_episode(eid, "https://cdn/a-v2.mp3", [
            {"kind": "keep", "src_start": 0, "src_end": 60000}])
        v2 = db.q1("SELECT current_media_id FROM episodes WHERE id=?", (eid,))["current_media_id"]
        # v1 仍是冻结渠道版本 -> 接受（订阅客户端可能尚未刷新）
        self.assertTrue(publish.player_callback(eid, v1, 12000)["accepted"])
        # v2 尚未重新发布 -> 不是渠道版本 -> 拒绝
        r = publish.player_callback(eid, v2, 12000)
        self.assertFalse(r["accepted"])
        # 运营把 v2 推到渠道后 -> v1 成为旧回调 -> 拒绝
        publish.republish_episode(eid)
        self.assertFalse(publish.player_callback(eid, v1, 12000)["accepted"])
        self.assertTrue(publish.player_callback(eid, v2, 12000)["accepted"])


class TestPublishingPolicy(BaseCase):
    """缺音频：hold 暂缓 vs transcript_first 可先公开文字稿（策略决定）。"""

    def test_hold_blocks_without_audio(self):
        eid = a.create_episode("无音频节目", 1, self.tid, "", "hold", 1)
        with self.assertRaises(publish.PublishBlocked):
            publish.publish_episode(eid)
        self.assertEqual(db.q1("SELECT status FROM episodes WHERE id=?", (eid,))["status"],
                         "blocked")

    def test_transcript_first_publishes_text(self):
        eid = a.create_episode("文字先行节目", 1, self.tid, "", "transcript_first", 1)
        # 无音轨也允许加文字稿：直接插一行（管理 UI 在无音轨时提示，这里验证发布路径）
        db.execute(
            "INSERT INTO transcript_lines(episode_id,media_id,speaker,start_ms,end_ms,text,status) "
            "VALUES (?, NULL, 'host', 0, 5000, ?, 'active')",
            (eid, "纯文字稿先行公开的内容gamma"))
        publish.publish_episode(eid)
        publish.publish_to_channels(eid)
        page = public_svc.episode_public(db.q1("SELECT slug FROM episodes WHERE id=?",
                                               (eid,))["slug"])
        self.assertIsNone(page["audio"])
        self.assertTrue(page["text_only"])
        self.assertIn("gamma", page["lines"][0]["text"])
        # RSS 条目无 enclosure（文字回退）
        feed = publish.render_rss()
        self.assertIn("text-only item", feed)


class TestStagesAndSearchIsolation(BaseCase):
    """分阶段流水线；未公开/未授权片段不能被全文搜索得到。"""

    def test_stages_gate_each_other(self):
        eid = a.create_episode("阶段节目", 1, self.tid, "", "hold", 1)
        services.add_media(eid, "https://cdn/x.mp3", 10000, license="owned")
        db.execute("UPDATE media_assets SET state='transcoding' WHERE episode_id=?", (eid,))
        publish.advance_jobs(eid)
        self.assertEqual(db.q1(
            "SELECT state FROM jobs WHERE episode_id=? AND stage='transcode'", (eid,))["state"],
            "blocked")
        db.execute("UPDATE media_assets SET state='ready' WHERE episode_id=?", (eid,))
        results = publish.advance_jobs(eid)
        states = {r["job"]: r["state"] for r in results}
        self.assertEqual(states["transcode"], "done")
        self.assertEqual(states["index"], "done")
        # 未发布 -> RSS 阶段保持 blocked
        self.assertEqual(states["rss"], "blocked")

    def test_unpublished_not_searchable(self):
        eid, mid = self.make_episode(do_publish=False)
        # 草稿状态：嘉宾关键词 beta 与主持人 alpha 都搜不到
        self.assertEqual(services.search_public("beta"), [])
        self.assertEqual(services.search_public("alpha"), [])
        publish.advance_jobs(eid)
        publish.publish_episode(eid)
        publish.publish_to_channels(eid)
        # 发布后：主持与已授权嘉宾内容可搜
        self.assertTrue(any(h["episode_id"] == eid for h in services.search_public("alpha")))
        hits = services.search_public("beta")
        self.assertTrue(hits and hits[0]["speaker"].startswith("guest:"))
        # 撤销嘉宾授权后立即从索引消失
        seg = db.q1("SELECT id FROM consent_segments WHERE media_id=? AND consent_status='granted'",
                    (mid,))
        a.revoke_consent(seg["id"])
        self.assertEqual(services.search_public("beta"), [])
        # 主持内容仍在
        self.assertTrue(services.search_public("alpha"))

    def test_broken_lines_not_searchable_after_reedit(self):
        eid, mid = self.make_episode(do_publish=True)
        self.assertTrue(services.search_public("beta"))
        services.reedit_episode(eid, "https://cdn/a-v2.mp3", [
            {"kind": "keep", "src_start": 0, "src_end": 8000},
            {"kind": "cut", "src_start": 8000, "src_end": 15000},
            {"kind": "keep", "src_start": 15000, "src_end": 60000},
        ])
        # 嘉宾行 broken，不能再被搜到
        self.assertEqual(services.search_public("beta"), [])


class TestPermissionSeparation(BaseCase):
    """商务表单与公开档案权限分离；未公开访谈不进搜索、不出现在公开页。"""

    def test_inquiries_admin_only(self):
        a.add_inquiry("某品牌", "bd@brand.com", "希望投放广告，包含未发布内容合作", "商务")
        # 公开搜索不会命中商务表单内容
        self.assertEqual(services.search_public("希望投放广告"), [])
        # 管理员可见
        rows = a.list_inquiries()
        self.assertEqual(len(rows), 1)
        # 未登录无公开 API 能读到 inquiries（数据层无任何公开查询入口）
        public_methods = [n for n in dir(public_svc) if "inquir" in n.lower()]
        self.assertEqual(public_methods, [])

    def test_guest_attachment_and_guests_page(self):
        eid, mid = self.make_episode(do_publish=True)
        page = public_svc.episode_public(db.q1("SELECT slug FROM episodes WHERE id=?",
                                               (eid,))["slug"])
        self.assertEqual(page["guests"][0]["name"], "周远")


if __name__ == "__main__":
    unittest.main(verbosity=2)
