# 版本记录（RELEASE NOTES）

本文档记录「雪哨 v2 / xueqiu-watch-v2」的主要版本与关键修复。当前线上版本为 **v2.3.0**。

---

## v2.3.0 — 多字体 + 灰阶标题栏（2026-08-09）
- 渲染引擎支持多款字体：`song` 宋体（默认）、`fang` 仿宋、`kai` 楷体、`deng` 等线-细、`hira` Hiragino Sans GB W3。
  - `eink.render_digest(font_tag=...)` 切换；对应字库 `fonts/font{size}_{tag}.xwbf`。
  - 字库生成器 `tools/gen_songs_fonts.py` 重构为支持 `FONTS` 字典多字体。
- 标题栏支持灰阶：`title_gray`（0=纯黑底白字，0.95=近黑带白点纹理）用 Bayer 4×4 抖动自绘（因推送 `dither=false`，灰阶需自行点阵化）。
- 用户最终选定 **宋体 + 纯黑标题栏** 为定稿外观（Hiragino 等其他字体字库保留备用，未推到设备）。

## v2.2.0 — 像素级复刻「历史上的今天」排版（2026-08-09）
- 彻底改用**宋体**（`simsun.ttc`），1-bit 墨水屏下细笔画表现远优于黑体。
- 排版参数对齐参考图：标题栏 30px、内容 16px、名字/时间 12px、宽松间距、6 条。
- 修复三个渲染 bug：
  - 名字带字母被截断 → 去掉 `min(uw, avail_w-70)` 硬编码宽度限制，完整显示。
  - 时间格式/字号不统一 → 统一为「M月D日 H:MM」无前导零、三级同字号。
  - 字顶/字底缺行 → 字库格高改为真实自然高度 `ascent+descent`，`baseline=ascent`，零裁切。
- Zectrix 推送字段名修正为 `images`、端口改 8899、守护进程改用 `setsid`。

## v2.1.0 — 纯 Python 点阵渲染引擎（2026-08-09）
- 引入 `bitmapfont.py`：纯 Python 1-bit 点阵渲染 + 手写 PNG 编码，彻底摆脱 Pillow 对 ARM NAS 的依赖。
- 引入 `.xwbf` 点阵字库格式（GBK 全字符集），本地用 Pillow 一次性烤好，NAS 端只读。
- 重写 `eink.py` 用新引擎渲染，本机与 NAS 出图逐像素一致。

## v2.0.0 — 脱离本机架构（2026-08-09）
- 把原「雪哨」Chrome 插件的抓取/渲染/推送逻辑搬到 QNAP NAS 常驻运行，不再依赖电脑常开。
- 单进程 `main.py`：后台 Cookie 接收 HTTP 服务 + 主线程定时轮询。
- Cookie 多通道导入（HTTP 推送 + 文件同步），加密存储。
- 多 Sink 推送：Zectrix 墨水屏 + 手机（wecom/bark/http）。
- 配套 `server.py` 提供 `/api/set-cookies`、`/api/posts`、`/api/health`、`/api/refresh-group`。

---

## 已知限制
- 墨水屏内容更新节奏 = 推送端 `poll_interval_minutes`（10 分钟）；设备端刷新频率需在 Zectrix 小程序另行对齐，否则会反复刷同一张旧图。
- 雪球 cookie 每隔几周过期，需手动重导（后端自动续用，过期只需重导一次）。
- 字库为 GBK 字符集，极生僻汉字可能缺字（缺字时画空心方框占位）。
