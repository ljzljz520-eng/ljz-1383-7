"""Create demo data: host, topics, guests, an episode with transcript/consent/chapters, admin user."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, auth, admin as a, services, publish  # noqa


def run():
    db.init_db()
    conn = db.get_conn()
    if conn.execute("SELECT COUNT(*) c FROM hosts").fetchone()["c"] == 0:
        conn.execute("INSERT INTO hosts(slug,name,bio) VALUES('lin-xia','林夏','科技与社会话题主持')")
        a.create_topic("人工智能", "AI 如何改变工作与创作")
        a.create_topic("城市观察", "城市、交通与公共生活")
        a.create_topic("独立开发", "独立开发者的产品与生活")
        a.create_guest("周远", "研究员，关注 AI 与劳动", "zhou@example.com")
        a.create_guest("阿南", "城市摄影师", "anan@example.com")
        auth.create_user("admin", "admin123")

    host = conn.execute("SELECT * FROM hosts WHERE slug='lin-xia'").fetchone()
    topic_ai = conn.execute("SELECT * FROM topics WHERE slug='人工智能'").fetchone()
    guest = conn.execute("SELECT * FROM guests WHERE name='周远'").fetchone()

    if not conn.execute("SELECT 1 FROM episodes WHERE slug='ai-and-work'").fetchone():
        eid = a.create_episode("AI 与我们的工作", host["id"], topic_ai["id"],
                               "和研究员周远聊聊自动化、创作与那些不会消失的工作。",
                               release_policy="hold", immutable_media=1)
        a.attach_guest(eid, guest["id"])
        mid = services.add_media(eid, "https://cdn.example.com/audio/ai-and-work.mp3",
                                 60_000, license="owned")
        a.add_chapter(eid, 0, "开场")
        a.add_chapter(eid, 8_000, "自动化会先替代谁")
        a.add_chapter(eid, 30_000, "创作与判断")
        a.add_transcript_line(eid, "host", 0, 8_000,
                              "欢迎收听。今天我们聊聊人工智能正在怎样改变日常的工作。")
        a.add_transcript_line(eid, f"guest:{guest['id']}", 8_000, 15_000,
                              "我认为最先被改变的是高度结构化、可度量的重复性任务。")
        a.add_transcript_line(eid, "host", 15_000, 22_000,
                              "那么创造性工作呢？比如写作者和设计师。")
        a.add_transcript_line(eid, f"guest:{guest['id']}", 22_000, 29_000,
                              "创作里真正稀缺的是判断和品味，而不是生成本身。")
        a.add_transcript_line(eid, "host", 30_000, 60_000,
                              "最后给还在焦虑的听众一个建议吧。")
        a.set_consent(eid, guest["id"], 8_000, 29_000, title="周远全部发言（授权）")
        a.add_annotation(eid, 10_000, 12_000, "这里补一个数据来源", "factcheck")
        print("seeded episode", eid)
    else:
        print("episode already exists")
    # 确保默认频道存在
    with db.tx() as c:
        publish.get_or_create_default_channel(c)
    print("done. login: admin / admin123")


if __name__ == "__main__":
    run()
