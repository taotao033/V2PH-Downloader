# scripts/

V2PH-Downloader 的本地归档辅助脚本。

**用途范围：仅供本机离线归档。** 这些脚本刻意限制了并发和下载速率，并在每个 URL 之间加了 sleep。**请勿调高**——v2ph 按账号 / IP 限流，激进同步只有一个结局：你的账号被封、整个项目被针对、cookie 登录绕过被打补丁，大家一起完。

如果你打算把这些内容**二次架站对外提供服务**——**请停下**：这不是这套脚本的设计目的，也不在项目的预期使用范围内。

---

## 文件一览

| 文件                  | 作用                                                                       |
| --------------------- | -------------------------------------------------------------------------- |
| `sync_local.py`       | 主管道：`discover`（构建监视列表） + `sync`（驱动 v2dl 下载）              |
| `sync_actors_profile.py` | 把 `data/sync/actors.xlsx` 里的演员**逐个采集 profile + 头像**入库；已完成的自动跳过。用来"先看脸再挑订阅"。 |
| `v2dl-sync.ps1`       | 给 Windows 任务计划程序用的 PowerShell 薄封装，套在 `sync_local.py` 之上   |
| `smoke_profiles.py`   | 单元级 smoke：合成 HTML（**镜像真实 v2ph DOM**）→ 解析 → 写入 / 读出 SQLite |
| `smoke_manager.py`    | 端到端 smoke：用 FakeBot 跑通 `ScrapeManager`，覆盖正常路径 + backfill 路径 |
| `smoke_real_html.py`  | 拿 `album截图/*.html`（用户在浏览器"另存为"出来的真实页面）做硬断言       |
| `smoke_listing_name.py` | actor / 机构列表页 display name 抽取的回归用例                             |
| `smoke_collision.py`  | 同名 album 落盘冲突时的目录消歧（`Album / Album (2)`）回归用例             |

`sync_local.py` 把监视列表写到仓库根目录下的 `data/sync/`（已 gitignore）。

---

## 快速上手

```powershell
# 1) 激活项目 venv（之前跑过 v2dl 的话应该已经做过）
.\.venv\Scripts\Activate.ps1

# 2) 一次性：把 v2ph 上所有机构枚举到一个 xlsx 工作簿
#    列：url | name | total | 是否采集(0/1)
#    所有行的 是否采集 默认是 0（暂时什么都不同步）
python scripts/sync_local.py discover companies
#   -> data/sync/companies.xlsx

# 3) 用 Excel 打开 data/sync/companies.xlsx，把你想下载的机构那一行
#    的「是否采集」从 0 改成 1，保存。

# 4) 日常增量同步：读取 xlsx，只挑「是否采集 == 1」的行交给 v2dl 跑。
#    已经下载过的相册会通过 download_log_path 自动跳过。
python scripts/sync_local.py sync --destination "D:\v2ph_archive"

# 5)（可选）每周一次的完整重扫，会翻页跑完每家被选中机构的所有列表页。
#    用得很少——请求量比较大。
python scripts/sync_local.py sync --destination "D:\v2ph_archive" --mode full

# 6)（可选）做一份「按演员」的监视列表。`discover actors` 会把
#    companies.xlsx 里每家机构的每一页都翻完，写出 data/sync/actors.xlsx
#   （同样是 4 列；其中 total 是该演员在所有遍历过的页里被链到的页数，
#    可以拿来当「在这家机构很活跃」的排序依据）。
python scripts/sync_local.py discover actors
#   -> data/sync/actors.xlsx
#
#    翻页默认是「全量」的。常用的限速开关：
#      --only-selected             只走 是否采集 = 1 的机构
#                                  （快得多；缺点是漏掉没勾选的机构里的演员）
#      --max-pages-per-company N   每家机构最多翻 N 页就停
python scripts/sync_local.py discover actors --only-selected --max-pages-per-company 5

#    在 actors.xlsx 里把你想下载的演员标 是否采集 = 1，保存，
#    然后让 sync 跑这份 xlsx 而不是 companies.xlsx：
#    --mode incremental  只翻每个演员列表的第 1 页（补最新专辑，日常够用）
#    --mode full         翻完该演员的所有列表页（首次全量 / 周级重扫）
python scripts/sync_local.py sync --input data/sync/actors.xlsx --destination "D:\v2ph_archive" --mode full

# 7)（可选）CDN 中断或部分相册没下完时，智能补漏（见下文「--force-download」）
python scripts/sync_local.py sync --input data/sync/actors.xlsx --destination "D:\v2ph_archive" --mode full --force-download
```

**重跑 `discover companies` 不会清掉你的勾选**：脚本按 `url` 列匹配，把已有的「是否采集」原样填回去。所以日 / 周级别地重新跑 discover 来发现新机构是安全的（新加进来的机构默认 0），你之前的订阅集不会被重置。

下载在磁盘上**按机构分目录**存放：

```
D:\v2ph_archive\
├── Beautyleg\
│   ├── [LE] LERB-146 - Min.E\
│   │   ├── 001.jpg
│   │   └── ...
│   └── ...
├── 网络美女\
│   └── ...
└── RQ-STAR\
    └── ...
```

目录名优先取自机构列表页的 breadcrumb / `<title>` 标签（所以**中文品牌名会落到中文目录**），实在解析不到时回落到 URL slug（英文）。

默认的限速参数：

* `--max-worker 2`：同一相册并发下载数（硬上限 3）
* `--rate-limit 1000` kbps：单文件限速（硬上限 2000）
* `discover actors` 在不同机构之间 sleep 5 秒

`discover actors` **整个爬取过程只起一次 Chrome**（每次 fetch 重启的话每次要付 5-10 秒启动成本，跑下来很恐怖），翻页直接复用了 v2dl 自己的 `UrlHandler.add_page_num` / `get_max_page`——也就是说 v2dl 正常 scrape 时用的同一套翻页判定逻辑也是这里的停步条件，行为一致。

这些上限其实就是 `v2dl` 自己的默认值；这层 wrapper 只是当成硬天花板强制住，避免你某天手抖把 `--max-worker` 拉到 16 去批量跑。

### `sync` 命令行参数（要点）

| 参数 | 说明 |
| --- | --- |
| `--input PATH` | 监视列表。默认 `data/sync/companies.xlsx`；按演员维度时用 `data/sync/actors.xlsx` |
| `--destination PATH` | 归档根目录（传给 `v2dl -d`） |
| `--mode incremental \| full` | `incremental` = 每个 listing 只翻第 1 页；`full` = 翻完所有列表页 |
| `--max-worker N` | 单相册并发下载数（硬上限 3，默认 2） |
| `--rate-limit N` | 单文件限速 kbps（硬上限 2000，默认 1000） |
| `--force-download` | **智能补漏**：只重试空相册 / 部分下载的相册；已完整的相册仍跳过（详见下文） |
| `--dry-run` | 只打印将要执行的 `v2dl` 命令，不真正跑 |

> **注意**：`sync_local.py --force-download` **不等于** `v2dl -f` / `--force`。后者会无脑重下所有相册的所有图片；`sync` 里的 `--force-download` 只会补缺失部分，已有图片文件不会重抓。

---

## 按演员同步：相册数量与 skip 策略

用 `sync --input data/sync/actors.xlsx` 时，相册会落在 `<destination>\<演员名>\<相册名>\` 下。目标是**该演员目录里的相册文件夹数与网站列表一致**。

### 两层含义不要混

| 维度 | 作用 | 存储 |
| --- | --- | --- |
| **全局 CDN 去重** | 这个 album URL 的图片是否已经从网站拉过 | `%APPDATA%\v2dl\downloaded_albums.txt` |
| **演员目录完整性** | 这个演员文件夹下是否**有一份**该相册的副本 | 磁盘 sidecar + `actor_album_placements` 表 |

多人合辑（杂志 / DOLCE / FRIDAY 等）会出现在多个演员的列表里。旧逻辑只要 URL 在 `downloaded_albums.txt` 里就全局 skip，导致演员 B 的目录比网站少几本。现在的 actor listing 模式：

| 情况 | 行为 |
| --- | --- |
| 该演员目录下已有相册且**有图片** | 跳过 |
| URL 在 txt 里，但图在**别的演员**目录下 | **复制一份**到当前演员目录（物理 `copy`，互不影响） |
| 源目录为空（只有 `.v2dl_album.json`）或从未下载 | **重新 scrape** 并下载 |
| 目录里只有 sidecar、图片数 = 0 | 视为未完成，**重新 scrape** |

判断是否「有图」用 `count_files()`，**自动排除** `.v2dl_album.json`。

### `--force-download`（智能补漏）

传给 v2dl 的 `--retry-incomplete`。适用场景：CDN 403 导致**部分图片**没下完、或历史代码留下空目录。

| 相册状态 | 普通 `sync` | `sync --force-download` |
| --- | --- | --- |
| 已完整（磁盘图片数 ≥ `listed_photo_count`） | 跳过 | **仍跳过** |
| 部分下载（例如 listed=90，实际 60 张） | 跳过（有图即视为完成） | **重新 scrape，只补缺的编号** |
| 空目录 / 缺失整本相册 | 会补（见上表） | 会补 |

查可疑的部分下载相册：

```sql
SELECT album_url, title, scraped_photo_count, listed_photo_count, download_dest
FROM albums
WHERE listed_photo_count IS NOT NULL
  AND scraped_photo_count < listed_photo_count;
```

### CDN 报错时怎么办

图片域 `cdn.v2ph.com` 走 Cloudflare。日志里常见 `HTTP 403`、`CDN warmup failed`、`Failed to download N images`。

1. **保持默认限速**（`--max-worker 2`、`--rate-limit 1000`），不要拉高。
2. **长时间跑批时分段**：CF 错误连续刷屏时 **Ctrl-C**，冷静几小时再跑。
3. **一次不要勾太多演员**；用 `actors.xlsx` 的 `是否采集` 控制批量。
4. **CDN 错误变多后重启一次 sync**（重新起 Chrome / CDN 标签页）；必要时手动打开 v2ph 刷新 cookie。
5. 网络不稳时换 VPN 节点或试直连；确保已 `pip install curl-cffi`（浏览器 CDN 通道挂掉时的后备）。
6. 补漏用 `--force-download`，不要用 `v2dl -f`（后者会重下已完整的相册）。

---

## Profile DB & 头像归档

`v2dl` 在抓相册图片之外，还会**把 actor / album 的卡片信息写进一份 SQLite**，方便之后做关联查询、报表、再加工。

### 落盘位置

| 文件 | 默认路径 | 说明 |
| --- | --- | --- |
| Profile DB | `<download_dir>/v2ph_profiles.sqlite3` | 5 张表：`actors` / `albums` / `actor_album_placements` / `album_models` / `album_tags` |
| 头像目录   | `<download_dir>/_avatars/<actor_slug>.<ext>` | actor 主页上方那张自我介绍照片 |

两条路径都可以在 `config.yaml` 单独覆盖；留空就按上面默认派生：

```yaml
static_config:
  # 留空 = 自动派生为 <download_dir>/v2ph_profiles.sqlite3 / <download_dir>/_avatars
  profile_db_path: ""
  avatar_dir: ""
```

把 `profile_db_path` 显式设成空字符串可以**关闭** profile 收集（图片下载不受影响）。

### 表结构

```text
actors       (id, actor_url[unique], actor_slug, name, birthday, height,
              from_location, zodiac, blood_type, profession, hobbies, bio,
              listed_album_count, scraped_album_count,
              avatar_url, avatar_local_path,
              first_seen_at, last_updated_at)

albums       (id, album_url[unique], album_slug, title, release_date,
              listed_photo_count, scraped_photo_count,
              actor_id  --> actors.id  (ON DELETE SET NULL),
              download_dest,            -- ★ 相册的本地目录路径
              first_seen_at, last_updated_at)

album_models (album_id --> albums.id  (ON DELETE CASCADE),
              model_name, model_url,   UNIQUE(album_id, model_name))

album_tags   (album_id --> albums.id  (ON DELETE CASCADE),
              tag_name,  tag_url,      UNIQUE(album_id, tag_name))

actor_album_placements
              (actor_id --> actors.id  (ON DELETE CASCADE),
               album_url, download_dest, scraped_photo_count,
               UNIQUE(actor_id, album_url))
              -- 同一 album URL 在每个演员目录下各有一份落盘记录
```

要找某个相册存哪儿，直接 join 就行：

```sql
SELECT a.name AS actor_name, ab.title, ab.release_date,
       ab.scraped_photo_count, ab.download_dest, a.avatar_local_path
FROM   actors a
JOIN   albums ab ON ab.actor_id = a.id
WHERE  a.actor_slug = 'Miku-Tanaka';
```

### 老相册的自动补录（backfill）

`v2dl` 用 `%APPDATA%\v2dl\downloaded_albums.txt` 记录「这个 album URL 已从 CDN 拉过图」。如果你是在装上 profile 收集功能**之前**就已经跑过 v2dl 的老用户，那些老相册的 URL 都在 txt 里、文件也在磁盘上、但 SQLite 里啥都没有。

**直接输入单个 album URL**（没有演员 listing 的 `parent_slug`）时，`scrape_album` 走全局 skip + backfill：

| 状态 | 行为 |
| --- | --- |
| 不在 `downloaded_albums.txt` | 正常完整抓取（所有页 + 下图） |
| 在 txt 且 profile DB 已有记录 | 完全跳过 |
| **在 txt 但 profile DB 没有记录** | **只抓 page 1** 拿到 album 卡片，`scraped_photo_count` 直接用 `count_files()` 数磁盘上的实际文件 |

**通过 `sync --input actors.xlsx` 跑演员列表**时，走上一节的 per-actor 策略（按演员目录判断 skip / 复制 / 重下），不会被「别的演员已经下过这本合辑」误跳过。详见 [按演员同步：相册数量与 skip 策略](#按演员同步相册数量与-skip-策略)。

这意味着**老用户什么都不用做，把之前抓过的 actor URL 重跑一遍就行**：

```powershell
# 例：之前已经下载过田中美久的所有相册，现在想把 profile 补到 DB 里
python -m v2dl --bot drissionpage `
  "https://www.v2ph.com/actor/Miku-Tanaka?hl=zh-Hans"
```

actor 主页扫一次（拿 actor profile + 头像），列表里每个 album 也只翻 page 1（拿 album 卡片），跑一遍下来 actor 的全部历史相册都会进库。

如果有些 album 不挂在任何 actor 列表下面（散养 URL），把它们写到一个文本文件里走 url-file 模式即可，同样会触发 backfill：

```powershell
python -m v2dl --bot drissionpage --url-file my_old_albums.txt
```

### 用 smoke 脚本快速验证一下

不想动真账号也不想等 Cloudflare，可以直接跑这几个 smoke：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\smoke_profiles.py     # 解析 + DB 单测
.\.venv\Scripts\python.exe -X utf8 scripts\smoke_manager.py      # 端到端，含 backfill
.\.venv\Scripts\python.exe -X utf8 scripts\smoke_listing_name.py # 列表页 display name
.\.venv\Scripts\python.exe -X utf8 scripts\smoke_collision.py    # 同名 album 目录消歧
.\.venv\Scripts\python.exe -X utf8 scripts\smoke_real_html.py    # 用真实 HTML 做硬断言
```

所有 smoke 都把数据写到临时目录、跑完自动清理，不会污染你的 archive。改了 `profiles.py` 或 `manager.py` 之后**至少过一遍** `smoke_manager.py` 与 `smoke_real_html.py`。

---

## 用头像挑人：`sync_actors_profile.py`

> **用例**：`discover actors` 给你列出了 v2ph 上**所有**演员（动辄两三千），但你根本不知道哪些是想要的。这个脚本帮你**预先把每个演员的基本信息 + 头像采集进库**，跑完之后在文件管理器里翻 `<destination>\_avatars\` 看脸，把感兴趣的人在 `actors.xlsx` 里勾 `是否采集 = 1`，再走 `sync_local.py sync` 下专辑。

它读 `data/sync/actors.xlsx`，逐行决定该做什么（**不需要 `是否采集` 也能跑——默认采集**所有**行**）：

| DB 中状态 | 行为 |
| --- | --- |
| 没有此 actor | **full**：抓 actor 主页 HTML → 解析（13 个字段 + `avatar_url` + 简介）→ upsert → 下头像 → 写 `avatar_local_path` |
| 有 actor 行，但头像没落盘且 `avatar_url` 已知 | **avatar-only**：跳过 HTML 抓取，**只去 CDN GET 一次头像** —— 用来续传中断的 run |
| 有 actor 行 + 头像文件存在 | **skip**：完全跳过，不消耗 Cloudflare 配额 |
| 有 actor 行但 `avatar_url` 和文件都没有 | **full**：上次解析很可能挂了，重抓一次 |

实现上**复用了主下载器的全套基础设施**——不是另起炉灶：

- HTML 抓取走 `V2DLApp.bot.auto_page_scroll`（同一个 DrissionPage / Cloudflare clearance）
- 头像下载走 `ImageScraper.download_file`，先尝试**已 warmed 的 `cdn.v2ph.com` 标签页**（同源 fetch，绕 CF 最稳），失败回落 curl-cffi / httpx
- 解析走 `ProfileExtractor.extract_actor`（跟 `smoke_real_html.py` 用同一份）
- 入库走 `ProfileDB.upsert_actor` / `update_actor_avatar_path`，**与主下载器共享同一份 `v2ph_profiles.sqlite3`**——你之前 v2dl 跑过相册下载顺便存的 actor 行，这里自动 skip 不会重抓

### 快速上手

```powershell
.\.venv\Scripts\Activate.ps1

# 1) 先看一下 dry-run：会跑多少 full / avatar-only / skip，不动 Chrome / 不写库
python scripts\sync_actors_profile.py -d "D:\v2ph_archive" --dry-run

# 2) 小批起步——比如先把已有 actor 的"丢头像"全部续传回来（cheap）
#    + 再带 20 个新人 full fetch
python scripts\sync_actors_profile.py -d "D:\v2ph_archive" --limit 100

# 3) 翻 D:\v2ph_archive\_avatars\ 看脸，把感兴趣的演员在 actors.xlsx 里
#    把 是否采集 改成 1，保存。

# 4) 只跑勾过的子集（采集进度会很快——大概率它们已经在 DB 里了）
python scripts\sync_actors_profile.py -d "D:\v2ph_archive" --only-selected

# 5) 后续就走主管道按演员维度同步专辑
python scripts\sync_local.py sync --input data\sync\actors.xlsx --destination "D:\v2ph_archive"
```

### 命令行参数（要点）

| 参数 | 说明 |
| --- | --- |
| `-d / --destination` **必填** | 归档根目录。DB 派生为 `<destination>\v2ph_profiles.sqlite3`，头像派生为 `<destination>\_avatars\` |
| `--db PATH` | 显式指定 DB 路径（覆盖 `--destination` 派生） |
| `--avatar-dir PATH` | 显式指定头像目录（覆盖 `--destination` 派生） |
| `--no-avatar` | 不下载头像（仍会把 `avatar_url` 字段存进库，方便日后再补） |
| `--only-selected` | 只处理 `是否采集 == 1` 的行；默认**处理全部行**（预采集"看脸库"这个用例下，行越全越好） |
| `--limit N` | 本轮**最多处理** N 个 actor（已 skip 的不算预算）。`--limit` 用满时**优先填 avatar-only 续传**，因为这些不抓 HTML，CF 配额成本几乎为 0 |
| `--force` | 即使 DB 里完整也重抓一次（`upsert_actor` 走 COALESCE 合并，安全） |
| `--sleep SEC` | actor 主页**之间**的 sleep（默认 5 秒，沿用 `sync_local.INTER_URL_SLEEP_SECONDS`）。avatar-only 续传**不触发**此 sleep |
| `--dry-run` | 只分类不执行；不开 Chrome、不写库 |

### 落盘位置

| 文件 | 路径 | 来源 |
| --- | --- | --- |
| Profile DB | `<destination>\v2ph_profiles.sqlite3` | `ProfileDB.upsert_actor` |
| 头像 | `<destination>\_avatars\<actor_slug>.<ext>` | `ImageScraper.download_file`；扩展名按响应 MIME 自动改写（jpg / png / webp …） |
| `actors` 表的 `avatar_local_path` 字段 | 上面那条头像文件的绝对路径 | `ProfileDB.update_actor_avatar_path` |

跑完之后想批量浏览，文件管理器开**大图标视图**直接看就行；要做更花式的"打勾保存"前端的话，`avatar_local_path` 也已经写库了，自己写小工具读 SQLite 就能拼。

### 注意事项

- **CF 配额是真天花板**。脚本默认 5 秒一次 page fetch，看起来慢——但你试图把 2000+ 个 actor 一次跑完肯定会被拦截。**分多天 / 多次跑**是正常用法，断点续传是设计目标。
- 如果你某次跑里发现"`Cloudflare interstitial or empty body; skipping`"开始连续刷屏，**立刻 Ctrl-C**，让账户冷静几小时再说，不要硬刚。
- 头像下载**失败不会丢 profile**——actor 主页那一坨 dt/dd 字段照样写库，只是 `avatar_local_path` 留空，下次跑会自动走 avatar-only 续传路径。
- 这个脚本和 `python -m v2dl --bot drissionpage <actor_url>` 在写库行为上是**等价的**（都调 `upsert_actor`），区别只是这个脚本**不抓相册列表**。所以你之前用主下载器扫过的 actor，这里会自动 skip 头像那一格补就行。

#### `smoke_real_html.py` 的特殊性

前 4 个 smoke 用合成的 HTML，写起来快但理论上可能跟真实 v2ph DOM 漂移。`smoke_real_html.py` 直接吃用户在浏览器"另存为"出来的 `album截图/*.html`：当文件存在时**对每个字段做硬断言**（actor 的 13 个字段 + album 的 7 个字段 + models / tags），缺文件时优雅 skip。

> 历史上发现的几个真实漂移：
> - `bio` 是裸文本节点（不是 `<p>`），最稳的来源是 `<meta name="description">`。
> - `listed_album_count` 在 `<div class="text-center my-2">已收录 <span>N</span> 套</div>`，不在 `.card` 容器里。
> - `avatar_url` 用 `<meta property="og:image">` 比依赖 `<img src=>` 抗 lazyload / "另存为"重写都强。
>
> 这些都已经在 `ProfileExtractor` 里覆盖到。下次想改 XPath 之前请先跑这个 smoke 看哪条假设是错的。

### 注意事项

- **头像下载会尝试一次但不阻塞**。失败就只在日志里 INFO 一句，profile 行照样写库。
- **Cloudflare 的关账户风险也适用于 profile 抓取**——backfill 模式虽然只翻 page 1，但仍然走浏览器，跟正常下载一样消耗每天的限额。如果要把上百个老 actor 一次性补录，建议**分批跑**或在两次之间手动间隔一下。
- `scraped_photo_count` 总是从磁盘文件数计算（不是"本次成功下载数"），所以**重跑会自动修正**，不用担心半路失败留下的脏数据。

---

## 用 Windows 任务计划程序定时跑

```powershell
# 以管理员身份在仓库根目录跑 PowerShell：
$repo = (Resolve-Path .).Path
$cmd  = "-NoProfile -ExecutionPolicy Bypass -File `"$repo\scripts\v2dl-sync.ps1`" -Destination `"D:\v2ph_archive`""

$action  = New-ScheduledTaskAction -Execute "pwsh.exe" -Argument $cmd -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Daily -At 3am
Register-ScheduledTask -TaskName "v2dl-sync-daily" -Action $action -Trigger $trigger -RunLevel Limited
```

如果你还想要**每周一次的完整重扫**，再注册一个任务，加上 `-Mode full` 并用周触发器即可。

`v2dl-sync.ps1` 会在 `$env:TEMP\v2dl-sync.lock` 抢一个 PID 锁；如果上一轮还没跑完它会直接退出，所以**两个计划重叠也不会真的同时跑两份**，是安全的。

---

## 注意事项 / 已知坑

1. **账号配额**：v2ph 每个账号有每日浏览图片的配额。一次性把所有机构都同步会**轻松打爆免费账号**。请有意识地规划（比如只标几家机构为 `是否采集 = 1`），或者接受看到很多 "VIP-only" 占位图。
2. **Chrome 实例**：`discover` 会短暂地拉起一个 DrissionPage 控制的 Chrome。**不要同时开着用相同 user-profile 的另一个 Chrome 窗口**，否则会因为 profile 被锁而启动失败。
3. **HTML 布局漂移**：提取逻辑依赖 `/company/<name>` / `/actor/<name>` 这种 href 前缀，加上 `N 套 / 套图 / 部 / 张 / sets` 的正则。如果 v2ph 之后大改了卡片结构，`sync_local.py` 里的 `_extract_listing_entries` / `_extract_paths` 可能要更新——不过此时是**温和退化**而不是炸：`total` 会变空、`name` 会回落到 URL slug，URL 仍然能正确抓到。**Profile DB 也是同样的容错风格**：`v2dl/scraper/profiles.py` 里的 label 字典对接不上时只会 INFO 一行 debug，相册照常下载，profile 字段留 NULL。
4. **遗留的 companies.txt**：如果 `data/sync/companies.xlsx` 不存在但 `data/sync/companies.txt` 还在（老安装留下来的），`sync` 会**透明地回落到那个 txt** 跑老格式。跑一次 `python scripts/sync_local.py discover companies` 就完成迁移到 xlsx 流程了。
5. **老相册没进 profile DB**：参见上面 [Profile DB & 头像归档 → 老相册的自动补录](#老相册的自动补录backfill) 一节，重跑 actor URL 即可自动补录，不需要新命令。
6. **演员目录相册数比网站少**：多半是合辑先被别的演员下过，或历史空目录。普通 `sync --mode full` 会自动复制 / 重下；部分图片 CDN 失败用 `--force-download` 补漏。参见 [按演员同步](#按演员同步相册数量与-skip-策略)。
7. **只有 `.v2dl_album.json` 没有图片**：视为空相册，重跑 `sync` 会重新 scrape；若仍失败可加 `--force-download`。

---

## 典型用法演进

如果你只是想把整套流程梳理一遍，建议按下面这个顺序：

1. `discover companies` —— 拉一份 companies.xlsx，看看 v2ph 上都有哪些机构。
2. 在 Excel 里凭兴趣勾选 `是否采集 = 1`（建议先勾 3-5 家试水，看下载量心里有数）。
3. `sync --destination D:\v2ph_archive --mode incremental` —— 跑一次增量。第一次会下不少，之后跑就只补新增的。
4. 想按演员订阅了再 `discover actors --only-selected` —— 只跑被你勾选过的机构，速度可接受；翻页是全量的，确保**每个演员都能落到表里**。
5. **`sync_actors_profile.py -d D:\v2ph_archive`**（分多天跑，受 CF 配额限制）—— 把每个演员的基本信息 + 头像逐个采集进 `<destination>\_avatars\`，跑完就有了一个"看脸库"。
6. **翻 `_avatars\` 看脸**，在 `actors.xlsx` 里把感兴趣的演员勾 `是否采集 = 1`，保存。
7. 用 `sync --input data/sync/actors.xlsx --mode full` 跑演员维度的同步，把刚才挑出来的人专辑都下下来。
8. CDN 中断或个别相册不完整时，加 `--force-download` 再跑一轮（已完整的不会重下）。
9. 把整套 `sync` 命令塞进 `v2dl-sync.ps1` 用任务计划程序定时跑。

---

## 路径速查

| 类型       | 默认路径                    | 是否会被 git 提交 |
| ---------- | --------------------------- | ----------------- |
| 机构列表   | `data/sync/companies.xlsx`  | 否（.gitignore）  |
| 演员列表   | `data/sync/actors.xlsx`     | 否（.gitignore）  |
| 老格式回落 | `data/sync/companies.txt`、`data/sync/actors.txt` | 否 |
| 已下载日志 | `%APPDATA%\v2dl\downloaded_albums.txt`（v2dl 配置目录） | 否 |
| Profile DB | `<download_dir>\v2ph_profiles.sqlite3`（可在 config.yaml 改） | 否 |
| 头像目录   | `<download_dir>\_avatars\<actor_slug>.<ext>` | 否 |
| 实际下载   | 你 `--destination` 指定的目录 | 否 |
