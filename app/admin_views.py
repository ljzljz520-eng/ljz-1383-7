"""Management console HTML views."""
from __future__ import annotations

from html import escape as e
from .views import layout, fmt_ms


def shell(content: str) -> str:
    nav = ('<a href="/">查看公开站</a> | <a href="/admin">控制台</a> | '
           '<a href="/admin/inquiries">商务收件箱</a> | <a href="/admin/logout">退出</a>')
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>管理台 · PodHost</title><link rel="stylesheet" href="/static/app.css"></head>
<body class="admin"><header class="top dark"><div class="wrap">
<span class="brand">🛠️ PodHost 管理台</span><nav>{nav}</nav></div></header>
<main class="wrap">{content}</main></body></html>"""


def dashboard(episodes, topics, guests, scheduled, broken_count):
    rows = "".join(
        f"""<tr>
        <td><a href="/admin/episodes/{ep['id']}">{e(ep['title'])}</a></td>
        <td><span class="status {ep['status']}">{ep['status']}</span></td>
        <td>{e(ep.get('topic_name') or '—')}</td>
        <td>v{ep['version_no'] or '—'}</td>
        <td>{'固定' if ep['immutable_media'] else '可更新地址'}</td>
        <td>{e(ep['publish_at'] or '')}</td>
        </tr>""" for ep in episodes)
    sched = "".join(f"<li>#{s['id']} {e(s['title'])} → {e(s['publish_at'])}</li>"
                    for s in scheduled)
    body = f"""
    <h1>控制台</h1>
    <div class="stat-row">
      <a class="stat" href="/admin/repair"><b>{broken_count}</b><span>待修复映射项</span></a>
      <a class="stat" href="#sched"><b>{len(scheduled)}</b><span>定时上线</span></a>
    </div>
    <section class="box"><h2>新建单集</h2>
      <form method="post" action="/admin/episodes" class="form inline">
        <input name="title" placeholder="单集标题" required>
        <select name="topic_id">{''.join(f'<option value="{t["id"]}">{e(t["name"])}</option>' for t in topics)}</select>
        <select name="release_policy">
          <option value="hold">缺音频：暂缓发布</option>
          <option value="transcript_first">缺音频：先公开文字稿</option>
        </select>
        <select name="immutable_media">
          <option value="1">固定媒体版本发布</option>
          <option value="0">永久地址·允许更新音频</option>
        </select>
        <button>创建</button>
      </form>
    </section>
    <section class="box"><h2>单集</h2>
      <table class="tbl"><thead><tr><th>标题</th><th>状态</th><th>话题</th><th>版本</th><th>模式</th><th>定时</th></tr></thead>
      <tbody>{rows}</tbody></table>
    </section>
    <section class="box" id="sched"><h2>定时上线</h2><ul>{sched or '<li class=muted>无</li>'}</ul>
      <form method="post" action="/admin/run-scheduled"><button>模拟到点执行（检查授权闸门）</button></form>
    </section>
    <section class="box"><h2>嘉宾</h2>
      <form method="post" action="/admin/guests" class="form inline">
        <input name="name" placeholder="嘉宾姓名" required>
        <input name="bio" placeholder="简介">
        <button>新增嘉宾</button></form>
      <p>{' · '.join(f'{e(g["name"])}' for g in guests) or '—'}</p>
    </section>
    <section class="box"><h2>话题</h2>
      <form method="post" action="/admin/topics" class="form inline">
        <input name="name" placeholder="新话题" required>
        <input name="description" placeholder="描述">
        <button>新增话题</button></form>
    </section>"""
    return shell(body)


def episode_edit(d, guests, topics, rq):
    ep, medias = d["episode"], d["medias"]
    cur = next((m for m in medias if m["id"] == ep["current_media_id"]), None)

    media_rows = "".join(
        f"""<tr class="{'current' if m['is_current'] else ''}">
        <td>v{m['version_no']}</td>
        <td>{e(m['url'])}</td>
        <td>{m['duration_ms']//1000}s</td>
        <td>{e(m['license'])} / <span class="lic {m['license_status']}">{m['license_status']}</span></td>
        <td>{e(m['etag'] or '')}</td><td>{m['state']}</td>
        <td>{'当前' if m['is_current'] else ''}</td></tr>""" for m in medias)

    cons_rows = "".join(
        f"""<tr class="{c['status']}"><td>#{c['id']}</td>
        <td>{e(next((g['name'] for g in guests if g['id']==c['guest_id']), '?'))}</td>
        <td>{fmt_ms(c['start_ms'])}–{fmt_ms(c['end_ms'])}</td>
        <td><span class="lic {c['consent_status']}">{c['consent_status']}</span></td>
        <td>{e(c['status'])}</td><td>{e(c['title'])}</td>
        <td>{'<form method=post action="/admin/consent/'+str(c['id'])+'/revoke"><button class=small>撤销授权</button></form>' if c['consent_status']=='granted' else ''}</td>
        </tr>""" for c in d["consents"])

    lines_rows = "".join(
        f"""<tr class="{l['status']}"><td>#{l['id']}</td><td>{e(l['speaker'])}</td>
        <td>{fmt_ms(l['start_ms'])}–{fmt_ms(l['end_ms'])}</td><td>{e(l['text'][:80])}</td>
        <td>{l['status']}</td></tr>""" for l in d["lines"])

    chap_rows = "".join(
        f"<li class='{ch['status']}'>{fmt_ms(ch['start_ms'])} {e(ch['title'])}</li>"
        for ch in d["chapters"])
    anno_rows = "".join(
        f"<li class='{a['status']}'>{fmt_ms(a['start_ms'])}–{fmt_ms(a['end_ms'])} {e(a['body'])}</li>"
        for a in d["annotations"])

    jobs_rows = "".join(
        f"<li><b>{j['stage']}</b>: <span class='status {j['state']}'>{j['state']}</span> {e(j['detail'])}</li>"
        for j in d["jobs"])

    ce = d["channel_episode"]
    ch_info = ""
    if ce:
        ch_info = f"""<p>渠道条目：模式 <b>{e(ce['media_mode'])}</b>，
        冻结音轨 #{ce['media_id']}，GUID <code>{e(ce['rss_guid'])}</code>，
        enclosure {e(ce['enclosure_url'] or '（无·文字回退）')}</p>"""

    repair_summary = sum(len(v) for v in rq.values())

    body = f"""
    <p><a href="/admin">← 控制台</a></p>
    <h1>{e(ep['title'])} <span class="status {ep['status']}">{ep['status']}</span></h1>
    {f'<p class="notice err">{e(ep["block_reason"])}</p>' if ep['block_reason'] else ''}
    <p class="muted">GUID <code>{e(ep['guid'])}</code>（永久不变） ·
    发布策略：{e(ep['release_policy'])} · {'固定媒体版本' if ep['immutable_media'] else '永久地址可更新'}</p>
    {ch_info}

    <div class="grid2">
    <section class="box"><h2>① 音轨修订</h2>
      <table class="tbl sm"><thead><tr><th>版本</th><th>URL</th><th>时长</th><th>许可</th><th>etag</th><th>状态</th><th></th></tr></thead>
      <tbody>{media_rows or '<tr><td colspan=7 class=muted>还没有音轨</td></tr>'}</tbody></table>
      <form method="post" action="/admin/episodes/{ep['id']}/media" class="form inline">
        <input name="url" placeholder="新音轨 URL（直接换音轨，不做映射）" required>
        <input name="duration_ms" type="number" placeholder="时长ms" value="60000">
        <select name="license"><option>owned</option><option>licensed</option>
          <option>cc0</option><option>cc-by</option><option>unknown</option></select>
        <button class="small">新增版本</button></form>
    </section>

    <section class="box"><h2>② 重新剪辑（编辑映射 → 时间码重定位）</h2>
      <p class="muted">每行一段：kind,src_start,src_end[,dst_start,dst_end]（毫秒，半开区间，需连续覆盖旧时长）。
      <br>例：删掉嘉宾发言 10–15s：
      <code>keep,0,10000</code> <code>cut,10000,15000</code> <code>keep,15000,{cur['duration_ms'] if cur else 0}</code></p>
      <form method="post" action="/admin/episodes/{ep['id']}/reedit" class="form">
        <input name="new_url" placeholder="新音轨 URL" required>
        <textarea name="segments" rows="3" placeholder="keep,0,10000&#10;cut,10000,15000&#10;keep,15000,{cur['duration_ms'] if cur else 0}"></textarea>
        <button>执行重剪并映射</button></form>
    </section>
    </div>

    <div class="grid2">
    <section class="box"><h2>③ 文字稿（当前版本 #{cur['id'] if cur else '—'}）</h2>
      <table class="tbl sm"><tbody>{lines_rows or '<tr><td class=muted>空</td></tr>'}</tbody></table>
      <form method="post" action="/admin/episodes/{ep['id']}/transcript" class="form inline">
        <select name="speaker"><option value="host">主持</option>
          {''.join(f'<option value="guest:{g["id"]}">嘉宾:{e(g["name"])}</option>' for g in guests)}</select>
        <input name="start_ms" type="number" placeholder="起ms"><input name="end_ms" type="number" placeholder="止ms">
        <input name="text" placeholder="文字内容" required style="flex:2">
        <button class="small">加一行</button></form>
    </section>

    <section class="box"><h2>④ 嘉宾与授权片段</h2>
      <form method="post" action="/admin/episodes/{ep['id']}/guests" class="form inline">
        <select name="guest_id">{''.join(f'<option value="{g["id"]}">{e(g["name"])}</option>' for g in guests)}</select>
        <button class="small">加入单集</button></form>
      <table class="tbl sm"><tbody>{cons_rows or '<tr><td class=muted>无授权片段</td></tr>'}</tbody></table>
      <form method="post" action="/admin/episodes/{ep['id']}/consent" class="form inline">
        <select name="guest_id">{''.join(f'<option value="{g["id"]}">{e(g["name"])}</option>' for g in guests)}</select>
        <input name="start_ms" type="number" placeholder="起ms"><input name="end_ms" type="number" placeholder="止ms">
        <input name="title" placeholder="片段标题"><button class="small">登记授权</button></form>
    </section>
    </div>

    <div class="grid2">
    <section class="box"><h2>章节 / 注释</h2>
      <ul class="plain">{chap_rows or '<li class=muted>无章节</li>'}</ul>
      <form method="post" action="/admin/episodes/{ep['id']}/chapters" class="form inline">
        <input name="start_ms" type="number" placeholder="ms"><input name="title" placeholder="章节名">
        <button class="small">加章节</button></form>
      <hr><ul class="plain">{anno_rows or '<li class=muted>无注释</li>'}</ul>
      <form method="post" action="/admin/episodes/{ep['id']}/annotations" class="form inline">
        <input name="start_ms" type="number" placeholder="起ms"><input name="end_ms" type="number" placeholder="止ms">
        <input name="title" placeholder="注释内容"><button class="small">加注释</button></form>
    </section>

    <section class="box"><h2>⑤ 流水线 & 发布</h2>
      <ul class="plain">{jobs_rows or '<li class=muted>无任务</li>'}</ul>
      <form method="post" action="/admin/episodes/{ep['id']}/advance"><button class="small">推进阶段（转码→索引→RSS）</button></form>
      <hr>
      <form method="post" action="/admin/episodes/{ep['id']}/publish" class="form inline">
        <button>立即发布（过许可/授权闸门）</button></form>
      <form method="post" action="/admin/episodes/{ep['id']}/schedule" class="form inline">
        <input name="publish_at" placeholder="2026-10-10T08:00:00Z">
        <button class="small">定时上线</button></form>
      <form method="post" action="/admin/episodes/{ep['id']}/republish" class="form inline">
        <button class="small">固定版本：以当前音轨重新发布到渠道</button></form>
      <form method="post" action="/admin/episodes/{ep['id']}/revoke-license" class="form inline">
        <button class="small danger">模拟：撤销当前音轨许可</button></form>
    </section>
    </div>

    <section class="box warnbox"><h2>⑥ 待修复（{repair_summary}）</h2>
      <p class="muted">重剪后无法映射的字幕/章节/授权/注释进入这里，旧秒数不被保留。</p>
      {repair_html(rq) if repair_summary else '<p class="muted">无</p>'}
    </section>
    """
    return shell(body)


def repair_html(rq):
    out = []
    for kind, label in (("consents", "嘉宾授权片段"), ("transcript", "字幕"),
                        ("chapters", "章节"), ("annotations", "注释")):
        for item in rq[kind]:
            old = f'{item.get("orig_start_ms")}–{item.get("orig_end_ms", item.get("orig_start_ms"))}'
            name = item.get("title") or item.get("text") or item.get("body") or ""
            out.append(
                f"""<form class="repair-row" method="post" action="/admin/repair/{kind}/{item['id']}">
                <span class="tag warn">待修复</span> <b>{label}</b>
                旧时间码 {old}ms · {e(str(name)[:60])}
                → 新起 <input name="start_ms" type="number" placeholder="ms">
                新止 <input name="end_ms" type="number" placeholder="ms">
                <button class="small">修复</button></form>""")
    return "".join(out)


def repair_page(rq):
    n = sum(len(v) for v in rq.values())
    return shell(f"<h1>待修复队列（{n}）</h1><p><a href='/admin'>← 控制台</a></p>"
                 + (repair_html(rq) if n else '<p class=muted>全部映射正常。</p>'))


def inquiries_page(items):
    rows = "".join(
        f"<tr><td>{i['id']}</td><td>{e(i['name'])}</td><td>{e(i['contact'])}</td>"
        f"<td>{e(i['topic'])}</td><td>{e(i['body'][:120])}</td>"
        f"<td>{'已处理' if i['handled'] else '新'}</td></tr>" for i in items)
    return shell(f"<h1>商务收件箱</h1><p><a href='/admin'>← 控制台</a></p>"
                 f"<p class='muted'>仅登录管理员可见；公开访客与全文搜索均无法触及。</p>"
                 f"<table class='tbl'><tbody>{rows or '<tr><td class=muted>空</td></tr>'}</tbody></table>")
