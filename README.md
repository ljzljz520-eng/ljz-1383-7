# PodHost — 播客主持人主页与发布管理系统

访客按**话题**浏览、在线收听并查看同步文字稿；主持人在 Web 管理台编辑
**单集、嘉宾、文字稿、章节、注释与嘉宾授权片段**；服务端 SQLite 持久化
**音轨修订、许可、发布计划与渠道版本**。

零第三方依赖：Python 3.10+ 标准库（`http.server` 线程服务器 + `sqlite3`，
FTS5 `trigram` 分词器支持中文子串检索）。

## 运行

```bash
python3 scripts/seed.py        # 初始化库 + 演示数据（管理员 admin / admin123）
python3 -m app.main 8000       # http://127.0.0.1:8000
python3 -m unittest discover -s tests -v   # 26 个测试
```

- 公开站：`/`（话题）、`/t/<话题>`、`/e/<单集>`（播放器+章节+文字稿）、
  `/search?q=`（全文搜索）、`/business`（商务表单）、`/feed.xml`（RSS）
- 管理台：`/admin`（Cookie 会话，PBKDF2 口令哈希）

## 核心问题：重新剪辑后时间码不能原封保留

重新剪辑在旧时间线上声明一组 `keep / cut / replace` 段（必须从 0 连续覆盖
到旧时长）：

| 段类型 | 含义 | 旧时间码命运 |
|---|---|---|
| `keep` | 音频保留，整体平移到新位置 | 点/区间经公式 `dst = dst_start + (t-src_start)` **重定位** |
| `cut` | 删除（如删掉一段嘉宾发言） | 落入其中的字幕/章节/授权/注释 → **无法映射** |
| `replace` | 换入新音轨（换音轨） | 新内容语义不连续，旧注释一律 **无法映射** |

`app/mapping.py`：

- 区间只有**完整落在保留区域**才重定位；横跨切口/换轨点（有正长度相交）即失败；
- 无法映射的项目：新版本上 `start_ms/end_ms` 置空、`status='broken'`，
  旧值保留在 `orig_start_ms/orig_end_ms` 供人工修复，进入 **待修复队列**
  （`/admin/repair`）。系统**绝不复制旧秒数到新版本**；
- 边界约定：点等于段起点归属该段（切到 cut/replace 起点不可映射）；
  区间右端点按“结束侧”判定（整段保留时 `[0,8000)` 的终点 8000 合法）。

重剪在一个事务内完成：新音轨版本 → 保存 edit_map/edit_segments →
字幕、章节、嘉宾授权片段、注释全部重映射 → 重建公开索引 → 已发布单集按
媒体模式刷新渠道（见下）。

## 固定媒体版本 vs 可更新永久单集地址

单集 `guid` 永远不变；`immutable_media` 决定两种发布模型，发布那一刻把
“**实际渠道版本**”冻结进 `channel_episodes`。

| 维度 | 固定媒体版本（默认，frozen） | 永久地址允许更新音频（current） |
|---|---|---|
| enclosure URL | 版本化：`a.mp3?v=2`，旧 URL 永久可取 | URL 与 guid 终身不变 |
| 重剪后渠道 | **仍指向发布时的旧版本**，历史引用/深链继续可用 | 立即指向新音轨 |
| 上线新版本 | 必须人工“以新版本重新发布” | 自动更新 |
| HTTP 缓存 | 旧 URL 不可变，可长期强缓存 | feed/资源 `etag` 变化，条件请求失效 |
| 订阅客户端 | 未刷新也继续拿到旧文件（字节一致）；刷新后才看到新条目 | 刷新时凭新 `etag`/`Last-Modified` 拉新音频 |
| 历史引用 | 旧秒数对旧版本永远自洽 | 旧书签/旧进度指向的内容语义已变，靠 etag 与版本回调拦截 |
| 旧播放器回调 | 重新发布前 v1 仍是渠道版本→接受；重新发布后 v1→409 | 重剪后旧 `media_id` 立即 409；etag 不匹配也 409 |

RSS：`/feed.xml` 返回 `ETag/Last-Modified`，客户端带 `If-None-Match`
且未变化时 **304 空体**——这就是“RSS 缓存未刷新”的真实表现。frozen 重剪
不 bump feed etag；current 重剪 bump etag。播放器旧回调记录在
`player_events` 审计表，**不会拿旧 position 去定位新时间线**。

## 发布闸门、分阶段流水线与缺音频策略

发布（含定时到点）前检查：音轨许可未撤销/过期、当前版本上无 `revoked`
或 `broken` 的嘉宾授权片段、音频转码就绪。任一不过 → `status='blocked'`
并记录 `block_reason`。**定时上线在到点时再次过闸**：授权在等待期被撤销
则保持 blocked、绝不上线。

流水线（`jobs` 表）按阶段推进、前阶段门控后阶段：
`transcode → index(全文) → rss`。无音频时转码跳过。

缺音频的发布由单集**策略**决定（管理台可选）：

- `hold`（默认）：暂缓发布；
- `transcript_first`：可先公开文字稿，公开页与 RSS 以**文字回退**呈现
  （feed 中为无 enclosure 的 text-only 条目，页面显示“无可用音频”）。

许可在发布后被撤销：单集立即 blocked，渠道 enclosure 清空（保留文字回退），
音频不再公开。

## 权限分离与搜索隔离

- 商务表单 `/business` 写入独立的 `business_inquiries` 表，只有登录管理员
  能在 `/admin/inquiries` 看到；数据层没有任何公开查询入口，全文索引也
  不包含它。
- FTS5 索引是“发布状态的投影”：`rebuild_fts` 只装入 **已发布单集 ×
  active 字幕 × 嘉宾授权 granted 且 active（且属于当前音轨版本）** 的行。
  草稿、待修复字幕、撤销授权、被 cut 的嘉宾发言都搜不到（测试覆盖）。

## 验收场景（均在 tests/ 中自动化）

1. **删掉一段嘉宾发言**：cut 10–15s → 主持人开场行平移保留，嘉宾行与
   横跨切口的注释进入待修复、当前时间码为空、旧秒数留存；可人工修复。
2. **换音轨**：replace 段内字幕/授权/注释全部 broken，不把旧授权复制到新音轨。
3. **定时上线时授权被撤销**：到点执行器复查闸门 → blocked，内容不公开、搜不到。
4. **RSS 缓存未刷新**：etag 未变返回 304；frozen 重剪 feed 仍是 v1 且 etag 不变，
   人工重新发布后才变 v2；current 模式 URL/guid 不变但 etag 变化。
5. **播放器旧回调到达**：current 模式重剪后旧 media_id → 409；frozen 模式
   重新发布前后按渠道版本裁决；etag 不匹配 → 409；审计留痕。

另含：缺音频 hold 阻断 vs transcript_first 文字公开、分阶段门控、
商务/搜索权限隔离、公开页呈现实际渠道版本与文字回退。

## 代码地图

```
app/db.py        表结构（音轨修订/编辑映射/授权/渠道版本/jobs/FTS5）+ 事务
app/mapping.py   时间码编辑映射引擎（纯函数，可单测）
app/services.py  音轨版本、重剪事务、待修复、FTS 索引与公开搜索、许可撤销
app/publish.py   发布闸门、定时执行器、渠道快照、RSS/etag、流水线、回调裁决
app/public.py    公开只读模型（话题浏览、实际渠道版本、文字回退）
app/admin.py     管理台 CRUD（单集/嘉宾/文字稿/章节/注释/授权/商务）
app/auth.py      会话与口令
app/views.py     公开页面；app/admin_views.py 管理台页面
app/server.py    路由；app/main.py HTTP 入口；app/static/ 播放器与样式
scripts/seed.py  演示数据；tests/ 26 个测试
```
