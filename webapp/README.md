# webapp/ — V2PH 本地归档浏览站

一个用 Flask 写的轻量网站，把 `D:\v2ph_archive` 下已离线归档的相册 / 模特 / 机构按
[v2ph.com](https://www.v2ph.com/) 的版式重新呈现出来。**纯本地、只读**：不联网、不写库、
不下载，只读取你已经采集好的 `v2ph_profiles.sqlite3` + 磁盘上的图片。

## 功能对照（参考原站版式）

| 页面 | 路由 | 说明 |
| --- | --- | --- |
| 首页 | `/` | 站点统计（相册 / 模特 / 图片 / 机构数）、精选相册、最新更新、热门模特、热门标签 |
| 地区 | `/region/<key>` | 中国 / 日本 / 韩国 / 台湾 / 泰国 / 欧美（按模特 `region` 字段，见下文） |
| 模特列表 | `/models` | 支持地区筛选、关键字、排序（相册数 / 名称 / 更新） |
| 模特主页 | `/model/<slug>` | 头像 + 资料卡（生日 / 身高 / 出身 / 星座 / 血型…） + 其相册 |
| 机构列表 | `/vendors` | 按本地实际收录量排序的厂牌 |
| 机构主页 | `/vendor/<slug>` | 该机构下本地相册 |
| 标签 | `/tags`、`/tag/<name>` | 标签云 + 单标签相册 |
| 相册 | `/album/<slug>` | 瀑布流图片 + 点击灯箱（键盘 ← → Esc）、分页、模特 / 标签 / 描述、**相关推荐**、**打包下载** |
| 随机 | `/random` | 随机跳转到一套有图的相册 |
| 打包下载 | `/album/<slug>/download` | 把该相册所有图片流式打包成 zip 下载 |
| 搜索 | `/search?q=` | 同时搜相册标题/描述与模特 |
| 图片 | `/media/<rel>` | 带路径穿越保护地从归档根目录读图 |
| 注册 / 登录 | `/register`、`/login`、`/logout` | 账号体系（密码用 werkzeug 加盐哈希存储） |
| 我的账户 | `/account` | 会员状态、到期时间、订阅记录 |
| 会员套餐 | `/pricing`、`/subscribe/<plan>` | 月度 / 季度 / 年度，**模拟支付**开通 VIP |
| 切换语言 | `/set-lang/<code>` | cookie 持久化；右上角下拉 + 页脚均可切 |
| 管理后台 | `/admin`、`/admin/users` | 仅管理员：统计总览 + 用户管理（赠送/撤销 VIP、设/取消管理员） |
| 我的收藏 | `/favorites?tab=album\|actor` | 收藏的相册 / 模特（需登录） |
| 浏览历史 | `/history` | 最近看过的相册（登录后自动记录） |
| 收藏开关 | `POST /fav/<album\|actor>/<slug>` | 相册页 / 模特页的爱心按钮，AJAX 即时切换 |

### 收藏与浏览历史

- 相册页、模特页右上角有**爱心按钮**，登录后点一下即收藏 / 取消（不刷新页面）；未登录会跳到登录页。
- 登录用户每打开一个相册会**自动记一条浏览历史**（同一相册只保留最近一次）。
- 收藏 / 历史数据存在独立的 `app_data.sqlite3`（`favorites` / `history` 两张表），与归档库隔离。

### 无限滚动 / 加载更多

模特、地区、机构、标签、机构详情、模特详情等列表页改用「**加载更多**」：滚到底部自动
追加下一页（`IntersectionObserver`），也可手动点按钮。实现上每个列表路由都支持
`?partial=albums|models|vendors`，只返回卡片片段供前端追加，无 JS 时按钮仍可点。

### 找回密码（纯本地，无需邮箱服务器）

| 入口 | 说明 |
| --- | --- |
| `/forgot` | 登录页的「忘记密码？」链接。输入用户名/邮箱后生成一次性重置令牌（30 分钟有效）。 |
| 重置链接 | 本地没有邮件服务器，链接会**打印到运行服务的控制台 / `_server.log`**，形如 `[password-reset] user=xxx link=http://127.0.0.1:8000/reset/<token>`。 |
| `/reset/<token>` | 打开链接设置新密码；令牌用过即失效。 |

**命令行找回（被锁在外面时用）** —— 直接读写 `app_data.sqlite3`，无需启动服务：

```bash
python -m webapp.reset_password --list                       # 列出所有用户
python -m webapp.reset_password <用户名或邮箱>                 # 重置为随机密码并打印
python -m webapp.reset_password <用户名或邮箱> -p 新密码        # 重置为指定密码
```

### 管理员

- **第一个注册的账号自动成为管理员**；也可用环境变量 `V2PH_ADMIN=用户名1,用户名2`
  强制指定（这些账号注册即为管理员）。
- 管理后台可：查看用户/VIP/订单/收入统计；搜索用户；给任意用户**赠送**或**撤销** VIP
  （赠送会记一条 `granted` 订单，金额 0）；**设/取消管理员**（不能取消自己，避免锁死）。
- 登录支持「**记住我**」：勾选后会话保持 30 天，否则关浏览器即失效。

### 多语言（i18n）

界面支持 **简体中文 / 繁体中文 / 日本語 / English / 한국어** 手动切换（导航栏地球图标
或页脚）。相册标题、模特名等**数据本身**保持原样，只翻译界面文案。翻译表在
`i18n.py`，缺失的键回退到英文、再回退到键名。

### 账号与订阅（VIP）

仿原站「VIP-only」模式：

| 能力 | 免费 / 未登录 | VIP 会员 |
| --- | --- | --- |
| 相册浏览 | 仅前 `FREE_PREVIEW_PHOTOS`（默认 6）张 + 付费墙 | 全部高清大图 + 翻页 |
| 打包下载 zip | 跳转到 `/pricing` | 可用 |

- **支付是本地模拟**（`/subscribe/<plan>` 直接开通并写一条订单），适合演示；不接任何真实支付网关。
- 用户 / 订单数据存在**独立**的读写库 `webapp/app_data.sqlite3`，与只读的归档库
  `v2ph_profiles.sqlite3` **完全隔离**（已 gitignore）。
- 会话密钥：设置环境变量 `V2PH_SECRET` 即可；不设时首跑会自动生成一个随机密钥
  持久化在 `app_data.sqlite3` 里。

「相关推荐」优先取同一模特的其它相册，不足再按共享标签数补齐。zip 打包用
`ZIP_STORED`（图片本身已压缩，不再二次压缩）并边生成边发送，不占额外磁盘。

封面优先用 `albums.cover_local_path`，没有就回退到相册目录里的第一张图；都没有时用占位图。

## 运行

```powershell
# 在仓库根目录、已激活项目 venv 的前提下：
.\.venv\Scripts\python.exe -m pip install -r webapp\requirements.txt

# 首次运行前，给 actors 表补一个 region 字段（幂等，可重复跑）
.\.venv\Scripts\python.exe -X utf8 webapp\migrate.py

# 启动
.\.venv\Scripts\python.exe -m webapp
# -> http://127.0.0.1:8000

```
## 测试账号
```angular2html
# 免费账号：随便注册一个
用户名：wulitaotao033
密码：wulitaotao033
```

## 管理员说明
```angular2html
# 管理后台地址
控制台总览：http://127.0.0.1:8000/admin
用户管理（赠送/撤销 VIP、设/取消管理员）：http://127.0.0.1:8000/admin/users

# 账号密码
没有预设的账号密码 —— 这是有意的设计。规则是：第一个注册的账号自动成为管理员。

目前 app_data.sqlite3 里还没有任何用户，所以请按下面的步骤创建你的管理员账号：

打开注册页 http://127.0.0.1:8000/register
填写用户名 / 邮箱 / 密码（密码至少 6 位），由你自己设定
提交后这个账号就是管理员，导航栏右上角头像菜单里会出现「管理后台」入口，直接进 /admin
如果你更想用一个固定的管理员用户名，也可以用环境变量指定（这样即使不是第一个注册的，只要用这个名字注册就是管理员）：

# 管理员账号：注册第一个账号即为管理员；也可设置环境变量强制指定 V2PH_ADMIN=用户名1,用户名2
当前管理员账号列表：$env:V2PH_ADMIN -split ','
管理员账号密码： admin/huaren2060
```

## 配置（环境变量，均有默认值）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `V2PH_ARCHIVE` | `D:\v2ph_archive` | 归档根目录（含相册目录 / `_avatars` / `_covers`） |
| `V2PH_DB` | `<archive>\v2ph_profiles.sqlite3` | SQLite 资料库 |
| `V2PH_HOST` / `V2PH_PORT` | `127.0.0.1` / `8000` | 监听地址 |
| `V2PH_DEBUG` | 空 | 设为非空开启 Flask 调试模式 |
| `V2PH_USER_DB` | `webapp\app_data.sqlite3` | 账号 / 订阅库（读写，独立于归档库） |
| `V2PH_SECRET` | 空 | 会话密钥；不设则自动生成并持久化 |
| `V2PH_FREE_PREVIEW` | `6` | 免费用户每套相册可预览的图片数 |

## 对数据库的补充

原 `v2ph_profiles.sqlite3` 的 `actors` 表没有地区字段，原站却按国家/地区导航。
`migrate.py` 给 `actors` 增加了一个 `region` 列（`japan` / `china` / `korea` / `taiwan` /
`thailand` / `western`），用 `from_location`、模特名、所属机构名做启发式推断。这是
**唯一**对归档库的结构改动，且只新增列、不改任何已有数据。

> 提示：本站只用于浏览你**本机已离线收藏**的内容，不对外提供服务、不触发任何抓取。
