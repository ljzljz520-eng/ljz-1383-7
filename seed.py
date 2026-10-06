"""演示数据：话题、嘉宾、单集、修订、注释、授权。"""
from podcast.db import fresh
from podcast import core

def seed(db):
    db.execute("INSERT INTO topics(slug,name) VALUES('tech','技术'),('life','生活'),('interview','访谈')")
    db.execute("INSERT INTO channels(code,name) VALUES('public','公开渠道'),('member','会员渠道')")
    db.execute("INSERT INTO policies(code,allow_transcript_only,description) VALUES"
               "('hold',0,'缺音频时暂缓发布'),('transcript_public',1,'缺音频时公开文字稿')")
    db.execute("INSERT INTO guests(name,bio,contact_email) VALUES"
               "('林晓','独立开发者','lin@example.com'),('陈默','科幻作家','chen@example.com')")
    tech = db.execute("SELECT id FROM topics WHERE slug='tech'").fetchone()["id"]
    itv = db.execute("SELECT id FROM topics WHERE slug='interview'").fetchone()["id"]

    e1 = core.create_episode(db, "ep001-remote-work", "远程办公的第三年",
                             "和林晓聊聊分布式团队。", "transcript_public", [tech], [1])
    r1 = core.add_revision(db, e1, "/media/ep001-v1.mp3", 3600_000)
    core.add_annotation(db, e1, r1, "chapter", 0, 600_000, "开场与近况")
    core.add_annotation(db, e1, r1, "subtitle", 1_000, 4_000, "大家好，欢迎来到节目。")
    core.add_annotation(db, e1, r1, "subtitle", 600_000, 604_000, "远程办公最大的变化是异步沟通。")
    core.add_annotation(db, e1, r1, "guest_clip", 600_000, 660_000, "林晓谈异步沟通", guest_id=1)
    core.set_license(db, 1, e1, "granted")
    core.publish_episode(db, e1)

    e2 = core.create_episode(db, "ep002-scifi", "科幻写作现场",
                             "陈默的创作方法。", "hold", [itv], [2])
    r2 = core.add_revision(db, e2, "/media/ep002-v1.mp3", 2700_000)
    core.add_annotation(db, e2, r2, "subtitle", 500, 3_000, "今天请到了作家陈默。")
    core.add_annotation(db, e2, r2, "chapter", 0, 900_000, "创作起点")
    core.set_license(db, 2, e2, "granted")
    core.publish_episode(db, e2)
    db.commit()
    return db

if __name__ == "__main__":
    import os
    from podcast.db import connect, init, DB_PATH
    if os.path.exists(DB_PATH): os.remove(DB_PATH)
    db = connect(); init(db); seed(db)
    print("seeded ->", DB_PATH)
