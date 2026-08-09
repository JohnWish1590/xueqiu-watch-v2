# 功能说明（FEATURES）

本文档逐条说明「雪哨 v2」已实现的功能点、配置项与行为细节。

## 一、核心功能

### 1. 雪球「特别关注」定时聚合
- 后端按 `poll_interval_minutes`（默认 10 分钟）唤醒一次，抓取所关注用户的帖子。
- 关注对象来源有两种，优先级：**本地缓存分组 → 在线拉取 → 手动 `followed_user_ids`**。
  - `followed_user_ids: ["__auto__"]`（默认）：自动从雪球「特别关注」分组拉取成员，存 `group_cache.json` 跨重启复用，避免每次请求。
  - 手动填数字 id 或雪球昵称：昵称由后端用 cookie 调搜索接口解析成数字 id（`xueqiu.resolve_user_ids`）。
- 每个用户抓 2 页（每页 10 条），按时间倒序合并去重。

### 2. 1-bit 墨水屏渲染（零依赖）
- 自研纯 Python 点阵渲染引擎 `bitmapfont.py`：
  - `Canvas`：1 字节/像素画布（0=白，1=黑），手写 PNG 编码器（zlib + CRC32），**无任何第三方库**。
  - 点阵字库 `.xwbf`：本地用 Pillow 把 GBK 全字符集光栅化后打包，NAS 端只读不渲染。
  - 字库格高 = 字体真实自然高度（`ascent+descent`），`baseline=ascent`，**上下零裁切**（解决早期"名字顶/底缺行"问题）。
- 排版参数（像素级复刻「历史上的今天」墨水屏样式）：
  - 画布 **400×300**，标题栏高 **30px**（黑底白字「雪球特别关注」+ 右侧当前时间）。
  - 内容 **16px**、名字/时间 **12px**、标题 **18px**（均为宋体）。
  - 内边距 8px，分割线 1px，条目间宽松间距，默认显示 **6 条**。
  - 时间格式统一 **「M月D日 H:MM」**（无前导零，如 `8月9日 16:19`）。
  - 名字**完整显示不截断**（去掉了旧版 `min(uw, avail_w-70)` 的硬编码宽度限制，带字母名如 `PorcoRosso_dw` 也完整）。
  - 内容超宽自动截断并追加省略号 `…`。

### 3. 墨水屏推送（Zectrix / 极趣云）
- `eink.push_image()`：`POST https://cloud.zectrix.com/open/v1/devices/{mac}/display/image`。
- 关键点（踩过坑）：multipart 字段名必须是 **`images`**（不是 `file`），否则返回 `{"code":500}`。
- 参数 `dither=false`、`pageId`（默认 1，对应墨卡页 1 槽位）。
- 优先用 `requests`，缺失时回退 `urllib`（手写 multipart 构造），保证零依赖也能推。
- HTTP 与 urllib 两条路径都验证过返回 `code:0` 成功。

### 4. 手机推送（多 Sink）
- 在 `phone` 段配置，默认**只对"新帖"推送**（按 id 去重，已见 id 持久化到 `state.json`，跨重启不重复）。
- 支持三种类型：
  - `wecom`：企业微信群机器人 webhook（安卓/iOS 通用）。
  - `bark`：iOS Bark APP。
  - `http`：自定义安卓 APP / 自建服务，POST 原始帖子 JSON `{"posts":[...]}`。
- 首次运行只"播种"已见 id，不把历史帖子全推到手机。

### 5. Cookie 多通道导入
- **通道 A（HTTP）**：`server.py` 后台守护线程起 HTTP 服务，浏览器「Cookie 管家」`POST /api/set-cookies` 推送。仅允许内网/回环来源写（`_client_private()` 校验）。
- **通道 B（文件同步）**：浏览器快捷写入目录 → SynologyDrive/QNAP 同步 → NAS 上的 `cookies-import.json`，后端按 mtime 变化自动导入 `cookies_store`。两通道可并存。
- 存储：`cookies_store.py` 用 `cryptography.Fernet` **加密**存 `cookies.json.enc`；装不上 `cryptography` 时自动降级为 base64 轻量混淆并打警告。

### 6. 帖子只读 API（供手机 APP）
- `GET /api/posts?limit=20`：返回最近抓到的帖子（公开只读，可经隧道暴露）。
- `GET /api/health`：健康检查（含 cookie 状态、帖子数）。
- `GET /api/refresh-group`：手动刷新「特别关注」分组缓存（仅内网）。

---

## 二、配置项一览（`config.json`）

| 字段 | 默认 | 说明 |
|------|------|------|
| `zectrix.api_key` | — | 极趣云 Open API Key（`zt_xxx`，必填，可用环境变量 `ZECTRIX_API_KEY` 覆盖） |
| `zectrix.device_mac` | — | 墨水屏 MAC（必填，可用环境变量 `ZECTRIX_DEVICE_MAC` 覆盖） |
| `zectrix.page_id` | `"1"` | 推送槽位 1–5 |
| `followed_user_ids` | `["__auto__"]` | 特别关注对象；`__auto__`=自动拉分组，或填数字 id / 昵称 |
| `poll_interval_minutes` | `10` | 轮询间隔（分钟），最小 1 |
| `digest_count` | `6` | 墨水屏展示条数（受 300px 高度限制实际 6 条） |
| `font_path` | `""` | 中文字体路径（使用 `.xwbf` 点阵字库时不依赖此项） |
| `server_port` | `8899` | Cookie 接收服务端口（**勿填 8080**，被 QTS 占用） |
| `cookie_import_file` | `""` | 同步落盘的 cookie 文件路径（通道 B） |
| `phone.enabled` | `false` | 是否启用手机推送 |
| `phone.type` | `"wecom"` | `wecom` / `bark` / `http` |
| `phone.webhook` | `""` | 对应类型的 webhook 地址 |

---

## 三、可扩展点
- **多设备**：`zectrix` 可复制多份（不同 mac/page_id），在 `main.py` 循环推送。
- **多平台 Sink**：微博、其他社交平台可复用同一抓取→渲染→推送链路（Cookie 管家已支持多站导出）。
- **自定义字体**：`tools/gen_songs_fonts.py` 的 `FONTS` 字典加一项即可烤新字库；显示端 `render_digest(font_tag=...)` 切换。
- **灰阶标题栏**：`render_digest(title_gray=0.0~1.0)` 用 Bayer 4×4 抖动模拟 1-bit 灰阶（因 `dither=false`，灰阶需自绘）。

---

## 四、安全说明
- API Key / MAC 等敏感字段支持环境变量覆盖，避免明文进 `config.json`（`.gitignore` 已忽略 `config.json`）。
- Cookie 加密存储，且 `set-cookies` 接口仅限内网/回环写入。
- `deploy_push.py` 的 NAS 凭据**强制从环境变量读取**，禁止硬编码进仓库。
