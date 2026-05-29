# scripts/

V2PH-Downloader 的本地归档辅助脚本。

**用途范围：仅供本机离线归档。** 这些脚本刻意限制了并发和下载速率，并在每个 URL 之间加了 sleep。**请勿调高**——v2ph 按账号 / IP 限流，激进同步只有一个结局：你的账号被封、整个项目被针对、cookie 登录绕过被打补丁，大家一起完。

如果你打算把这些内容**二次架站对外提供服务**——**请停下**：这不是这套脚本的设计目的，也不在项目的预期使用范围内。

---

## 文件一览

| 文件              | 作用                                                                       |
| ----------------- | -------------------------------------------------------------------------- |
| `sync_local.py`   | 主管道：`discover`（构建监视列表） + `sync`（驱动 v2dl 下载）              |
| `v2dl-sync.ps1`   | 给 Windows 任务计划程序用的 PowerShell 薄封装，套在 `sync_local.py` 之上   |

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
python scripts/sync_local.py sync --input data/sync/actors.xlsx --destination "D:\v2ph_archive"
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
3. **HTML 布局漂移**：提取逻辑依赖 `/company/<name>` / `/actor/<name>` 这种 href 前缀，加上 `N 套 / 套图 / 部 / 张 / sets` 的正则。如果 v2ph 之后大改了卡片结构，`sync_local.py` 里的 `_extract_listing_entries` / `_extract_paths` 可能要更新——不过此时是**温和退化**而不是炸：`total` 会变空、`name` 会回落到 URL slug，URL 仍然能正确抓到。
4. **遗留的 companies.txt**：如果 `data/sync/companies.xlsx` 不存在但 `data/sync/companies.txt` 还在（老安装留下来的），`sync` 会**透明地回落到那个 txt** 跑老格式。跑一次 `python scripts/sync_local.py discover companies` 就完成迁移到 xlsx 流程了。

---

## 典型用法演进

如果你只是想把整套流程梳理一遍，建议按下面这个顺序：

1. `discover companies` —— 拉一份 companies.xlsx，看看 v2ph 上都有哪些机构。
2. 在 Excel 里凭兴趣勾选 `是否采集 = 1`（建议先勾 3-5 家试水，看下载量心里有数）。
3. `sync --destination D:\v2ph_archive --mode incremental` —— 跑一次增量。第一次会下不少，之后跑就只补新增的。
4. 想按演员订阅了再 `discover actors --only-selected` —— 只跑被你勾选过的机构，速度可接受；翻页是全量的，确保**每个演员都能落到表里**。
5. 在 actors.xlsx 勾选具体演员，用 `sync --input data/sync/actors.xlsx` 跑演员维度的同步。
6. 把整套 `sync` 命令塞进 `v2dl-sync.ps1` 用任务计划程序定时跑。

---

## 路径速查

| 类型       | 默认路径                    | 是否会被 git 提交 |
| ---------- | --------------------------- | ----------------- |
| 机构列表   | `data/sync/companies.xlsx`  | 否（.gitignore）  |
| 演员列表   | `data/sync/actors.xlsx`     | 否（.gitignore）  |
| 老格式回落 | `data/sync/companies.txt`、`data/sync/actors.txt` | 否 |
| 已下载日志 | `%APPDATA%\v2dl\downloaded_albums.txt`（v2dl 配置目录） | 否 |
| 实际下载   | 你 `--destination` 指定的目录 | 否 |
