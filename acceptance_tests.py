#!/usr/bin/env python3
"""验收测试（HTTP 级，真实起服务）：
 1. 删掉一段嘉宾发言 -> 注释经编辑映射重定位，无法映射的进待修复
 2. 换音轨 -> 渠道版本切换，播放器拿到新媒体，注释不保留旧秒数
 3. 定时上线时授权被撤销 -> 计划 blocked，单集不上线
 4. RSS 缓存未刷新（缓存被写脏）-> 水位线校验自动重建
 5. 播放器旧回调到达 -> 记录但不应用
 6. 缺音频策略：公开文字稿 vs 暂缓发布
 7. 权限：全文搜索搜不到未公开访谈；商务表单仅管理员可读
"""
import json, os, sys, tempfile, threading, time, urllib.request

os.environ["PODCAST_DB"] = tempfile.mktemp(suffix=".db")
from podcast.db import connect, init
from podcast import core
from podcast.web import make_server, ADMIN_KEY

PORT = 8931
BASE = f"http://127.0.0.1:{PORT}"
K = f"?key={ADMIN_KEY}"
PASS = []

def check(name, cond, extra=""):
    PASS.append((name, bool(cond)))
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{extra}]" if extra and not cond else ""))

def http(path, data=None, headers=None, method=None):
    req = urllib.request.Request(BASE + path, method=method or ("POST" if data is not None else "GET"))
    if data is not None:
        body = json.dumps(data).encode()
        req.add_header("Content-Type", "application/json")
        req.data = body
    for k, v in (headers or {}).items(): req.add_header(k, v)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)

def setup():
    db = connect(); init(db)
    db.execute("INSERT INTO topics(slug,name) VALUES('interview','访谈')")
    db.execute("INSERT INTO channels(code,name) VALUES('public','公开'),('member','会员')")
    db.execute("INSERT INTO policies(code,allow_transcript_only,description) VALUES"
               "('hold',0,'暂缓'),('transcript_public',1,'公开文字稿')")
    db.execute("INSERT INTO guests(name) VALUES('嘉宾A'),('嘉宾B')")
    db.commit()
    srv = make_server(db, PORT)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return db

db = setup()
time.sleep(0.2)

print("场景1: 删掉一段嘉宾发言 -> 编辑映射重定位 + 待修复")
e1 = core.create_episode(db, "ep-cut", "剪辑测试集", policy_code="transcript_public", guest_ids=[1])
r1 = core.add_revision(db, e1, "/media/cut-v1.mp3", 120_000)
core.add_annotation(db, e1, r1, "subtitle", 1_000, 3_000, "开场白")            # 删除点之前
core.add_annotation(db, e1, r1, "subtitle", 11_000, 14_000, "嘉宾敏感发言")      # 落入删除区
core.add_annotation(db, e1, r1, "subtitle", 60_000, 63_000, "删除点之后的内容")  # 应平移 -5000
core.add_annotation(db, e1, r1, "chapter", 5_000, 50_000, "横跨删除点的章节")    # 横跨 -> 待修复
core.add_annotation(db, e1, r1, "guest_clip", 10_000, 15_000, "嘉宾授权片段", guest_id=1)  # 与删除区重叠
core.set_license(db, 1, e1, "granted")
core.publish_episode(db, e1)
r2 = core.add_revision(db, e1, "/media/cut-v2.mp3", 115_000,
                       ops=[{"op": "delete", "start": 10_000, "end": 15_000}])
core.promote_revision(db, e1, r2)
ann = db.execute("SELECT * FROM annotations WHERE revision_id=? ORDER BY id", (r2,)).fetchall()
by_text = {a["text"]: a for a in ann}
check("删除点前的字幕保持原位置", by_text["开场白"]["status"] == "active"
      and (by_text["开场白"]["start_ms"], by_text["开场白"]["end_ms"]) == (1000, 3000))
check("删除点后的字幕平移 -5000ms（非原封保留旧秒数）",
      by_text["删除点之后的内容"]["status"] == "active"
      and (by_text["删除点之后的内容"]["start_ms"], by_text["删除点之后的内容"]["end_ms"]) == (55_000, 58_000))
check("落入删除区的字幕进入待修复", by_text["嘉宾敏感发言"]["status"] == "pending_repair"
      and by_text["嘉宾敏感发言"]["reason"] == "source_deleted")
check("横跨删除点的章节进入待修复", by_text["横跨删除点的章节"]["status"] == "pending_repair"
      and by_text["横跨删除点的章节"]["reason"] == "partial_overlap")
check("被删的嘉宾授权片段进入待修复且保留旧时间码供人工核对",
      by_text["嘉宾授权片段"]["status"] == "pending_repair"
      and by_text["嘉宾授权片段"]["orig_start_ms"] == 10_000)
s, body, _ = http("/episode/ep-cut")
check("单集页提示存在待修复注释", "待修复" in body)
core.resolve_annotation(db, by_text["嘉宾授权片段"]["id"], "drop")
check("待修复可被人工丢弃", db.execute("SELECT status FROM annotations WHERE id=?",
      (by_text["嘉宾授权片段"]["id"],)).fetchone()["status"] == "dropped")

print("场景2: 换音轨（同长替换）-> 渠道版本切换")
s, p1, _ = http("/api/player/ep-cut")
v1_rev = json.loads(p1)["revision_id"]
r3 = core.add_revision(db, e1, "/media/cut-v3-remaster.mp3", 115_000, ops=[])
core.promote_revision(db, e1, r3)
s, p2, _ = http("/api/player/ep-cut")
p2 = json.loads(p2)
check("播放器拿到新修订版本", p2["revision_id"] == r3 and p2["version"] == 3)
check("媒体地址已更新", p2["media_url"] == "/media/cut-v3-remaster.mp3")
sub = [x for x in p2["subtitles"] if x["text"] == "删除点之后的内容"][0]
check("换轨后字幕仍是重定位后的秒数", (sub["start_ms"], sub["end_ms"]) == (55_000, 58_000))
s, body, _ = http("/episode/ep-cut")
check("网页呈现实际渠道版本 v3", "音频版本 v3" in body)

print("场景3: 定时上线时授权被撤销 -> 计划 blocked")
e3 = core.create_episode(db, "ep-sched", "定时上线集", guest_ids=[2])
core.add_revision(db, e3, "/media/sched-v1.mp3", 60_000)
core.set_license(db, 2, e3, "granted")
run_at = time.time() + 0.05
core.schedule_publish(db, e3, run_at)
core.set_license(db, 2, e3, "revoked", note="嘉宾临时撤回授权")
time.sleep(0.1)
res = core.run_scheduler(db)
ep3 = db.execute("SELECT status FROM episodes WHERE id=?", (e3,)).fetchone()["status"]
check("调度结果被阻塞", res[0]["status"] == "blocked")
check("单集未上线", ep3 == "held")
s, rss, _ = http("/rss/public.xml")
check("RSS 中不含被阻塞单集", "定时上线集" not in rss)
ev = db.execute("SELECT * FROM publish_events WHERE episode_id=? AND stage='schedule'",
                (e3,)).fetchone()
check("阻塞原因已记录", ev and "license" in ev["detail"])

print("场景4: RSS 缓存未刷新 -> 水位线校验自动重建")
xml1, _ = core.get_rss(db, "public")
xml2, hit2 = core.get_rss(db, "public")
check("内容未变时命中缓存 HIT", hit2 is True and xml2 == xml1)
# 模拟“缓存未刷新”：内容已被改动（直接改库，未走失效流程、代次未递增）
db.execute("UPDATE episodes SET summary='简介已更新v2', updated_at=? WHERE id=?",
           (time.time() + 1, e1))
db.commit()
xml3, hit3 = core.get_rss(db, "public")
check("过期缓存被水位线识别并重建", hit3 is False and "简介已更新v2" in xml3)
core.set_license(db, 2, e3, "granted")
core.publish_episode(db, e3)
xml4, _ = core.get_rss(db, "public")
check("新发布后 RSS 自动包含新单集", "定时上线集" in xml4)

print("场景5: 播放器旧回调到达 -> 记录不应用")
s, cur, _ = http("/api/player/ep-cut")
cur = json.loads(cur)
s, r_old, _ = http("/api/player/events", {"episode_id": e1, "revision_id": v1_rev,
                                          "event": "progress", "position_ms": 99999, "client_id": "old-player"})
check("旧修订回调不被应用", json.loads(r_old)["applied"] is False)
s, r_new, _ = http("/api/player/events", {"episode_id": e1, "revision_id": cur["revision_id"],
                                          "event": "progress", "position_ms": 5000, "client_id": "new-player"})
check("当前修订回调被应用", json.loads(r_new)["applied"] is True)
n_applied = db.execute("SELECT COUNT(*) n FROM player_events WHERE episode_id=? AND applied=1", (e1,)).fetchone()["n"]
n_stale = db.execute("SELECT COUNT(*) n FROM player_events WHERE episode_id=? AND applied=0", (e1,)).fetchone()["n"]
check("统计只计入有效回调", n_applied == 1 and n_stale == 1)

print("场景6: 缺音频策略 -> 公开文字稿 vs 暂缓发布")
e6a = core.create_episode(db, "ep-notext-hold", "无音频-暂缓", policy_code="hold", guest_ids=[1])
core.add_revision(db, e6a, None, 0)  # 缺音频
core.set_license(db, 1, e6a, "granted")
ra = core.publish_episode(db, e6a)
check("hold 策略：缺音频暂缓发布", ra["published"] is False and ra["reason"] == "audio_missing_held")
e6b = core.create_episode(db, "ep-textonly", "无音频-文字稿", policy_code="transcript_public", guest_ids=[1])
rv6 = core.add_revision(db, e6b, None, 0)
core.add_annotation(db, e6b, rv6, "subtitle", 0, 2000, "这期只有文字稿。")
core.set_license(db, 1, e6b, "granted")
rb = core.publish_episode(db, e6b)
s, body, _ = http("/episode/ep-textonly")
check("transcript_public 策略：文字稿公开上线", rb.get("transcript_only") is True and "文字稿回退" in body)

print("场景7: 权限分离 -> 搜索不可见未公开内容，商务表单仅管理员")
e7 = core.create_episode(db, "ep-secret", "未公开访谈", policy_code="hold", guest_ids=[2])
rv7 = core.add_revision(db, e7, "/media/secret.mp3", 60_000)
core.add_annotation(db, e7, rv7, "subtitle", 0, 3000, "绝密未公开访谈片段关键词：独角兽机密")
db.commit()
s, body, _ = http("/search?q=" + urllib.parse.quote("独角兽机密"))
check("全文搜索搜不到未公开访谈片段", "/episode/ep-secret" not in body and "绝密" not in body)
s, body_pub, _ = http("/search?q=" + urllib.parse.quote("只有文字稿"))
check("正向对照：已发布文字稿可被搜到", "/episode/ep-textonly" in body_pub)
http("/api/inquiries", {"name": "广告主", "email": "ad@x.com", "message": "投放咨询"})
s, _, _ = http("/api/inquiries")
check("公开读取商务表单被拒绝", s == 403)
s, inq, _ = http("/api/inquiries", headers={"X-Admin-Key": ADMIN_KEY})
check("管理员可读商务收件箱", s == 200 and "投放咨询" in inq)
s, body, _ = http("/episode/ep-secret")
check("未发布单集页面对外 404", s == 404)

print()
fails = [n for n, ok in PASS if not ok]
print(f"结果: {len(PASS)-len(fails)}/{len(PASS)} 通过" + (f"，失败: {fails}" if fails else ""))
sys.exit(1 if fails else 0)
