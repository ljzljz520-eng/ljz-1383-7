"""Server-rendered HTML (zero-dependency string templates)."""
from __future__ import annotations

from html import escape as _e


def layout(title: str, body: str, admin: bool = False) -> str:
    nav = ('<a href="/">公开档案</a> | <a href="/admin">管理台</a> | <a href="/feed.xml">RSS</a>'
           if admin else
           '<a href="/">首页</a> | <a href="/search">搜索</a> | <a href="/feed.xml">RSS</a> | <a href="/business">商务合作</a>')
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<link rel="stylesheet" href="/static/app.css">
</head><body>
<header class="top"><div class="wrap"><span class="brand">🎙️ PodHost</span>
<nav>{nav}</nav></div></header>
<main class="wrap">{body}</main>
<footer class="wrap"><small>PodHost · 播客主持人主页 · 公开档案与商务表单权限分离</small></footer>
</body></html>"""


def home(topics, episodes):
    cards = "".join(
        f'<a class="card topic" href="/t/{_e(t["slug"])}"><h3>#{_e(t["name"])}</h3>'
        f'<p>{_e(t["description"])}</p><span class="count">{t["episode_count"]} 集</span></a>'
        for t in topics)
    latest = "".join(
        f'<li><a href="/e/{_e(ep["slug"])}">{_e(ep["title"])}</a>'
        f'<span class="muted">{_e(ep.get("topic_name") or "")}</span></li>'
        for ep in episodes[:10])
    body = f"""
    <section class="hero"><h1>按话题发现播客</h1>
      <form class="searchbar" action="/search" method="get">
        <input name="q" placeholder="搜索已发布节目的文字稿…" aria-label="搜索">
        <button>搜索</button></form>
      <p class="muted">全文搜索只覆盖已发布、且嘉宾已授权的内容；未公开访谈片段不会出现。</p>
    </section>
    <h2>话题</h2><div class="grid">{cards or '<p class="muted">暂无话题</p>'}</div>
    <h2>最新节目</h2><ul class="ep-list">{latest}</ul>"""
    return layout("PodHost · 播客主页", body)


def topic_page(topic, episodes):
    items = "".join(
        f'<li class="eprow"><a href="/e/{_e(ep["slug"])}"><strong>{_e(ep["title"])}</strong></a>'
        f'{"<span class=tag>仅文字稿</span>" if ep.get("text_only") else ""}'
        f'<p class="muted">{_e(ep["summary"][:140])}</p></li>' for ep in episodes)
    body = f"""<p><a href="/">← 全部话题</a></p>
    <h1>#{_e(topic['name'])}</h1><p>{_e(topic['description'])}</p>
    <ul class="ep-list">{items or '<p class="muted">该话题下还没有已发布节目。</p>'}</ul>"""
    return layout(f"#{topic['name']}", body)


def fmt_ms(v):
    if v is None:
        return "--:--"
    v = int(v)
    h, rem = divmod(v, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s = rem // 1000
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def episode_page(data: dict):
    if data.get("forbidden"):
        return layout("未发布", '<div class="notice">该单集尚未发布。</div><p><a href="/">返回</a></p>')
    ep = data["episode"]
    a = data["audio"]
    if a:
        player = f"""
        <div class="player" data-media-id="{a['media_id']}" data-etag="{_e(a['etag'] or '')}">
          <audio controls preload="none" src="{_e(a['url'])}"></audio>
          <div class="pmeta">
            <span class="tag">渠道实际版本 v{a['version_no']}</span>
            <span class="tag mode">{"固定媒体版本" if a["mode"]=="frozen" else "永久地址·可更新" if a["mode"]=="current" else "快照前"}</span>
            <span class="muted">{fmt_ms(a['duration_ms'])}</span>
          </div>
          <p class="muted" id="cb-status">播放器会把当前版本 ID/Etag 随进度回调；旧音轨回调将被拒绝。</p>
        </div>"""
    else:
        player = """<div class="player textfallback">
          <span class="tag warn">无可用音频</span>
          <p>本节目当前以 <strong>文字稿</strong> 形式公开（文字稿先行或许可撤下后的文字回退）。</p>
        </div>"""

    chapters = "".join(
        f'<li><button class="jump" data-t="{ch["start_ms"]}">{fmt_ms(ch["start_ms"])}</button> '
        f'{_e(ch["title"])}</li>' for ch in data["chapters"])
    lines = "".join(
        f'<tr class="tline" data-start="{l["start_ms"] or ""}"><td class="ts">{fmt_ms(l["start_ms"])}</td>'
        f'<td><span class="speaker {("guest" if l["speaker"]!="host" else "host")}">{_e("嘉宾" if l["speaker"]!="host" else "主持")}</span>'
        f'{_e(l["text"])}</td></tr>' for l in data["lines"])
    guests = ", ".join(_e(g["name"]) for g in data["guests"]) or "—"
    body = f"""
    <p><a href="/t/{_e(data['topic']['slug']) if data['topic'] else '/'}">← {_e(data['topic']['name'] if data['topic'] else '返回')}</a></p>
    <h1>{_e(ep['title'])}</h1>
    <p class="muted">{_e(ep['summary'])}</p>
    <p class="muted">嘉宾：{guests}</p>
    {player}
    <div class="cols">
      <section><h2>章节</h2><ul class="chapters">{chapters or '<li class=muted>暂无章节</li>'}</ul></section>
      <section><h2>文字稿</h2>
        <table class="transcript"><tbody>{lines or '<tr><td class=muted>暂无已授权文字稿</td></tr>'}</tbody></table>
      </section>
    </div>
    <script src="/static/player.js"></script>"""
    return layout(ep["title"], body)


def search_page(q: str, results):
    items = "".join(
        f'<li class="hit"><a href="/e/{_e(r["episode_slug"])}">{_e(r["title"])}</a> '
        f'<span class="muted">{fmt_ms(r["start_ms"])}</span>'
        f'<p>…{_e(r["text"][:200])}…</p></li>' for r in results)
    body = f"""<h1>全文搜索</h1>
    <form class="searchbar" action="/search"><input name="q" value="{_e(q)}"><button>搜索</button></form>
    <ul class="hits">{items}</ul>
    {'<p class="muted">没有匹配的已发布内容。</p>' if q and not results else ''}"""
    return layout("搜索", body)


def business_page(msg: str = ""):
    body = f"""<h1>商务合作</h1>
    <p class="muted">此表单仅进入商务收件箱，与公开档案、编辑后台完全分离，不能用于访问未公开内容。</p>
    {f'<p class="notice ok">{_e(msg)}</p>' if msg else ''}
    <form method="post" action="/business" class="form">
      <label>称呼<input name="name" required></label>
      <label>联系方式<input name="contact" required></label>
      <label>意向话题<input name="topic"></label>
      <label>内容<textarea name="body" rows="5" required></textarea></label>
      <button>提交</button>
    </form>"""
    return layout("商务合作", body)


def login_page(err: str = ""):
    body = f"""<h1>管理台登录</h1>
    {f'<p class="notice err">{_e(err)}</p>' if err else ''}
    <form method="post" action="/admin/login" class="form">
      <label>用户名<input name="username" required></label>
      <label>密码<input type="password" name="password" required></label>
      <button>登录</button>
    </form>"""
    return layout("登录", body)
