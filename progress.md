# 进度日志：融合下载器平台扩展

## 会话：2026-07-23

### 阶段 11：项目经历归档、提交与推送
- **状态：** complete
- 执行的操作：
  - 完整读取 `project-retrospective-writer`，并复核 Obsidian 的 `SOUL.md`、`CLAUDE.md`、经历模板和经历索引。
  - 按项目名、仓库名和核心关键词检索 `学习/经历/`，确认唯一主经历为 `融合下载器从抖音收藏夹工具到多平台高质量下载工作台 2026-04-12.md`。
  - 更新主经历顶部定位、平台标签、演化主线和阶段结果；追加 YouTube 公开最高质量、Deno/EJS/FFmpeg 独立打包、移除脆弱登录、并行 Range 速度优化和真实验收结果。
  - 复核 `学习/经历/index.md` 已有正确入口，因此不重复修改索引；未创建或修改 `生活/复盘/`。
  - 最终运行 31 项自动化测试，全部通过；`pip check`、语法检查和 Git 空白检查均通过。
  - 审计 11 个待提交文件，未发现真实 Cookie、token、Authorization、密码或测试链接中的私有分享参数；构建产物、下载结果、虚拟环境和平台登录态均未进入提交范围。
  - 将 YouTube 功能与项目文档提交到 `main` 并推送 `origin/main`；推送后对比本地 HEAD 与远端分支引用，确认完全一致。
- 创建/修改的文件：
  - `D:/Homework/Obsidian/学习/经历/融合下载器从抖音收藏夹工具到多平台高质量下载工作台 2026-04-12.md`
  - `task_plan.md`
  - `progress.md`

## 会话：2026-07-22

### 阶段 10：YouTube 下载速度诊断与优化
- **状态：** complete
- 执行的操作：
  - 完整读取文件规划技能，恢复三个状态文件并运行 session catchup；确认工作区仍是前序 YouTube 功能的未提交改动，没有覆盖用户文件。
  - 按关键词检索跨项目经验，仅将“YouTube 4 MiB 分段可稳定完成 4K50”和“CDN Range 需现场验证”作为待验证假设。
  - 初步代码审计确认：YouTube 固定 4 MiB HTTP 顺序分段；`max_workers` 仅进入 `concurrent_fragment_downloads`，可能无法加速测试链接的普通分离媒体流。
  - 第一次 32 MiB 单 Range 基准在当前 CDN 节点 45 秒无响应并超时；已停止该大块策略，下一轮改为重新解析后的小 Range 顺序/并行对照，并让每种策略独立报告失败。
  - 第二轮基准成功：解析 4.551 秒；8 MiB 视频数据用 1 MiB 顺序 Range 仅 0.34 MiB/s，4 路并行为 1.16 MiB/s，8 路并行为 2.38 MiB/s，均为正确 206 Range，当前瓶颈已定位为单连接/顺序传输。
  - 第三轮块大小基准成功：8 路并行下 2 MiB 块为 4.64 MiB/s，4 MiB 块为 7.08 MiB/s；决定保留 4 MiB 稳定块大小并实现 2/4/8 路有界并行，而不是放大单连接。
  - 实现 YouTube 普通 HTTP 流有界并行 Range 下载：逐块校验 206、`Content-Range` 和长度，精确预分配文件，限制 8 路与 3 次重试，失败时清理不完整流并重新解析后回退 yt-dlp 稳定分段。
  - 新增分块覆盖、Cookie 剥离、Range 写入、重试、分轨合并和稳定回退测试；31 项全项目测试、`pip check`、语法检查和 Git 空白检查通过。
  - 使用测试链接完成 8 路真实 4K50 端到端下载：总耗时 44.132 秒；视频 231.5 MiB、平均 7.55 MiB/s，音频 1.9 MiB、0.51 MiB/s，合并 0.3 秒；最终 244,696,376 字节 MKV，FFprobe 确认 3840×2160/50fps VP9 + AAC。
  - GUI 速度档位回归通过：stable/balanced/fast 分别返回 2/4/8 路，YouTube 仍仅显示“视频媒体”和公开内容边界说明。
  - 默认均衡档补充测速通过：4 路下载 16 MiB 用时 4.115 秒，平均 3.89 MiB/s；用户无需手动切快速档也能获得明显提速。
  - 首次最终打包在覆盖现有 `dist/融合下载器.exe` 时遇到 `WinError 5`；构建分析和归档阶段已完成，当前先检查该精确 EXE 的占用进程，不原样重复构建。
  - 确认旧版 EXE 由用户窗口占用后未强制终止；先生成并验证并排优化版。用户关闭旧窗口后，以相同 SHA-256 的已验证优化版安全替换标准 `dist/融合下载器.exe`，并清理本轮创建的临时并排副本。
  - 最终标准 EXE 为 244,045,920 字节，SHA-256 `665A2B6BF07A5BA118F852011C1FF061D8D114DFB899F9476B37A5DCD3BE5FB8`；归档含 YouTube 模块、requests、Deno、EJS、FFmpeg/FFprobe，隐藏启动 12 秒正常。
  - 按 `experience-summary-writer` 合并更新旧条目“能列出格式不代表最高流可稳定且快速下载”，补充单连接限速、有界并行 Range、逐块验证、稳定回退和真实速度证据，没有新增重复条目。

### 阶段 9：移除 YouTube 登录
- **状态：** complete
- 执行的操作：
  - 按文件规划技能恢复三个状态文件，运行 session catchup，并检查当前 Git 未提交改动。
  - 查看用户截图，确认 Google 拒绝自动化/调试浏览器登录；公开视频下载能力本身未受影响。
  - 明确本阶段边界：YouTube 仅支持公开可访问内容，保留公开最高质量、Deno/EJS、4 MiB 分段、FFmpeg 合并和 FFprobe 验证。
  - 删除 YouTube 浏览器 profile、登录窗口、Cookie 捕获/注入与登录态读取代码；下载函数和统一调度不再接收账号 Cookie。
  - GUI 移除“登录 YouTube”按钮；切换到 YouTube 时隐藏“检查登录状态”，并显示“仅支持公开内容”的明确说明。
  - 更新 README、`.gitignore` 和 YouTube 测试，新增无登录 API、公开模式日志及受限内容错误边界检查。
  - `pip check` 通过；25 项自动化测试、全项目语法检查和 Git 空白检查通过，登录代码关键词扫描仅命中“API 不存在”的反向测试。
  - 四平台 GUI 回归通过：YouTube 隐藏全部登录/检查按钮，其他三个平台仍只显示各自登录入口；YouTube 状态文案明确公开内容边界。
  - 对测试链接完成公开模式实时解析：仍选择 `315+140`，即 3840×2160/50fps VP9 + AAC，Deno/EJS 警告为 0；本次不重复下载 244 MB 媒体。
  - 重新打包成功；首次归档关键词检查因假定 `downloaders.youtube` 会以明文条目出现而提前失败，已记录并改为检查归档实际命名与 PyInstaller TOC，不重复原假设。
  - 改用 `pyi-archive_viewer -r -b` 递归检查后确认归档包含 `downloaders.youtube`、YouTube extractor、`yt_dlp_ejs`、Deno、FFmpeg 和 FFprobe；新 EXE 为 244,038,713 字节，隐藏启动 12 秒正常。
  - 按 `experience-summary-writer` 回顾相关旧条目：“YouTube 下载｜能列出格式不代表最高流可稳定下载”和“yt-dlp 登录态｜Cookie 不要放进全局请求头”仍在各自适用边界内有效，无需合并、更新或删除；本轮产品取舍属于项目决策，没有另写跨项目经验。
  - 最终运行规划完成检查器仍因中文阶段格式报告 `0/0`；人工核对阶段 1-9 均为 complete，无 pending/in_progress，并以测试、实时解析、GUI 和打包证据完成审计。

### 阶段 8：YouTube 最高质量视频下载
- **状态：** complete（登录部分已由阶段 9 移除）
- 执行的操作：
  - 完整读取文件规划技能并恢复 `task_plan.md`、`findings.md`、`progress.md`。
  - 检查项目规则、Git 状态与现有 Bilibili、调度、GUI、README 和测试集成点；工作区初始为干净状态。
  - 将 YouTube 新目标作为阶段 8 合并到现有规划体系，没有创建第二套 memory bank。
  - 对测试链接完成未登录元数据探测：最高格式为 4K/50fps VP9 + AAC，但收到“缺少受支持 JavaScript 运行时、部分格式可能缺失”的弃用警告。
  - 核对 yt-dlp 官方 EJS 指南与本机环境：当前只有 Node 24.13.0，目标机不能依赖系统 Node；需要把运行时与 `yt-dlp-ejs` 纳入打包方案。
  - 安装并验证 `yt-dlp[default,deno]`：Deno 2.9.3 与 `yt-dlp-ejs 0.8.0` 正常工作；测试链接解析为 4K50 VP9 + AAC，且不再产生 JavaScript 运行时警告。
  - 用最低质量完成一次系统临时目录下载实验，确认合并后路径可由 `requested_downloads[].filepath` 获取，临时成品随目录自动清理。
  - 完成实现方案：独立 `downloaders/youtube.py`、域名受限 CookieJar、Deno/EJS 打包、统一 MKV 无重编码合并、FFprobe 验证、调度与 GUI 媒体平台分支。
  - 首次真实最高质量下载在视频流阶段返回 `HTTP 403`；元数据、Deno/EJS 和格式选择均成功，已停止原样重试并进入媒体访问上下文诊断。
  - 检索经验总结未发现可直接套用的 YouTube 403 条目；仅将“先核对签名 URL 请求上下文”作为假设。
  - 重新解析后验证 4K 视频与最佳音频均可普通 GET/Range 访问；采用已现场验证的 4 MiB HTTP 分段后再做完整下载，不原样重复失败路径。
  - 第二次真实下载成功：格式 `315+140`，3840×2160/50fps、VP9 + AAC、244,696,376 字节 MKV；FFprobe 内部验证通过，系统临时目录结束后媒体自动清理。
  - GUI 回归通过：YouTube 仅显示“视频媒体”，仅 YouTube 登录与检查按钮可见，IDM/评论/收藏参数禁用，链接输入保持可用。
  - 四平台切换回归通过：抖音、小红书、Bilibili 与 YouTube 各自只显示对应登录入口，原平台功能和输入状态未被 YouTube 分支覆盖。
  - 依赖检查、25 项自动化测试、全项目语法检查和 Git 空白检查全部通过。
  - 重新生成 244,045,149 字节单文件 EXE；归档确认包含 `downloaders.youtube`、完整 YouTube extractor、`yt_dlp_ejs` 脚本、Deno、FFmpeg 和 FFprobe，隐藏启动 12 秒保持正常。
  - 规划完成检查器仍因中文阶段格式报告 `0/0`；人工复核阶段 1-8 均为 complete，无 pending/in_progress，以真实下载、测试与打包证据完成审计。
- 创建/修改的文件：
  - `downloaders/youtube.py`（创建）
  - `tests/test_youtube.py`（创建）
  - `services/task_runner.py`
  - `app.py`
  - `requirements.txt`
  - `build_exe.ps1`
  - `.gitignore`
  - `README.md`
  - `AGENTS.md`
  - `task_plan.md`
  - `findings.md`
  - `progress.md`

## 会话：2026-07-17

### 阶段 7：Cookie 传递与日志降噪修复
- **状态：** complete
- 执行的操作：
  - 复核用户日志，确认 42 行红字由同一弃用提示以“提示/错误”各重复 21 次造成，下载流程最终完成。
  - 从 `yt-dlp` 全局 `http_headers["Cookie"]` 改为域名受限的内存 CookieJar。
  - 日志器对相同提示去重，并将进入 `error()` 的弃用提示降为普通提示；真实错误仍保留“Bilibili 错误”。
  - 增加 Cookie 域作用范围、提示去重和真实错误保留测试。
  - 运行 15 项自动化测试与全项目语法检查，全部通过；Git 空白检查通过。
  - 对用户最初提供的 BV 链接完成真实不下载解析：当前源码环境未读取到登录态，仍选择 `30080` 1080p/25fps AVC + `30280` AAC；自定义 logger 输出 0 条，弃用提示与错误均为 0 条。
  - 重新生成 200,289,103 字节单文件 EXE；归档确认包含 Bilibili 模块、yt-dlp Bilibili 解析器、FFmpeg 与 FFprobe，隐藏启动 12 秒保持正常。
  - 按 `experience-summary-writer` 复核相关旧条目：CDN 节点轮换与 B站 IDM 请求头条目仍有效，无需合并或删除；新增一条跨项目 CookieJar/日志分级经验，未记录任何真实 Cookie。
  - 首次暂存后的空白检查发现 `tests/__init__.py` 末尾多余空行；已删除并安排重新检查，源码逻辑不受影响。
  - 重新暂存后 Git 空白检查通过；提交文件清单、忽略目录和敏感信息扫描通过，未包含构建产物、登录态、下载媒体或真实 Cookie。
  - 规划技能完成检查器仍因中文阶段格式报告 `0/0`；人工复核 7 个阶段均为 complete，无 pending/in_progress，以实际测试和打包证据完成审计。
- 创建/修改的文件：
  - `downloaders/bilibili.py`
  - `tests/test_bilibili.py`
  - `task_plan.md`
  - `findings.md`
  - `progress.md`

### 阶段 1：恢复上下文与项目盘点
- **状态：** complete
- **开始时间：** 2026-07-17
- 执行的操作：
  - 完整读取 `planning-with-files-zh` 技能及三个模板。
  - 检查根目录项目规则、README、依赖、目录结构和 Git 状态。
  - 检索 Codex 项目记忆中与当前下载器有关的旧审阅索引；将其仅作为待复核假设。
  - 创建本次任务的三个项目级规划文件。
  - 初步定位 GUI、统一调度、平台下载器以及 `ffmpeg/ffprobe` 打包逻辑。
  - 手动执行会话恢复脚本；未发现需要补同步的旧会话内容。
  - 创建项目长期规则 `AGENTS.md`，将稳定约定与本次任务状态分离。
- 创建/修改的文件：
  - `task_plan.md`（创建）
  - `findings.md`（创建）
  - `progress.md`（创建）
  - `AGENTS.md`（创建）

### 阶段 2：Bilibili 技术方案与验收设计
- **状态：** complete
- 执行的操作：
  - 明确独立平台模块、统一批量调度与 GUI 最小功能入口的集成方式。
  - 核实本机 FFmpeg/FFprobe 可用，虚拟环境尚未安装 Bilibili 解析依赖。
  - 安装并验证 `yt-dlp` 2026.07.04。
  - 对给定 BV 链接完成不下载媒体的格式探测，确认 1080p DASH 视频与最佳 AAC 音频需合并。
- 创建/修改的文件：
  - 暂无。

### 阶段 3：功能实现
- **状态：** complete
- 执行的操作：
  - 新增 Bilibili 平台模块：链接识别、最高质量选择、临时下载、FFmpeg 合并、FFprobe 音视频验证和结构化报告。
  - 接入统一批量调度，保持单条失败聚合。
  - GUI 新增 Bilibili 平台且只暴露“视频媒体”，隐藏无关的登录和收藏控制。
  - 更新依赖、README 和 PyInstaller 收集配置。
  - 新增 Bilibili URL、媒体验证、命名和任务调度测试。
- 创建/修改的文件：
  - `downloaders/bilibili.py`（创建）
  - `services/task_runner.py`（修改）
  - `app.py`（修改）
  - `requirements.txt`（修改）
  - `build_exe.ps1`（修改）
  - `README.md`（修改）
  - `tests/__init__.py`（创建）
  - `tests/test_bilibili.py`（创建）

### 阶段 4：测试与真实链接验证
- **状态：** complete
- 执行的操作：
  - 运行 6 项自动化测试，首次发现并修复中文分享文案 URL 截断问题后全部通过。
  - 运行全项目语法检查和 Git 空白错误检查，均通过。
  - 将打包前置条件收紧：构建机缺少 FFmpeg 或 FFprobe 时直接停止，避免生成残缺 EXE。
  - 诊断三条 DASH 流的 Range 响应，确认均可访问；将质量排序调整为 1080p 高码率 AVC + 最佳 AAC，并启用分段下载超时/重试。
  - 验证 512 KiB、1 MiB、4 MiB 分段均成功，最终采用 4 MiB；已清理两次中断留下的精确临时目录。
  - 确认播放信息含 2 个备用 CDN 但 `yt-dlp` 公开格式未保留；实现节点补取、64 KiB 健康探测和下载中自动轮换。
  - 使用给定 BV 链接完成端到端真实下载；音频首节点中断后自动恢复，最终 FFmpeg 合并与 FFprobe 内部验证通过。
  - 独立 FFprobe 复核成品含 1920×1080 H.264 视频流和 AAC 音频流，时长 1973.269 秒，且无 `.bilibili-*` 临时目录残留。
  - 实际构建 200,281,429 字节单文件 EXE；归档确认含 Bilibili 模块、yt-dlp Bilibili 解析器、FFmpeg/FFprobe，隐藏启动 12 秒保持正常。
  - 手动回归 GUI 的 Bilibili 状态：仅显示“视频媒体”，隐藏两个登录按钮并禁用无关高级参数。
- 创建/修改的文件：
  - `downloaders/bilibili.py`（修复中文标点边界）
  - `build_exe.ps1`（强制检查并捆绑媒体工具）

## 测试结果
| 测试 | 输入 | 预期结果 | 实际结果 | 状态 |
|------|------|---------|---------|------|
| 初始工作区检查 | `git status --short` | 无用户未提交修改 | 输出为空 | 通过 |
| Bilibili 元数据提取 | 用户给定 BV 链接 | 可识别视频并列出最高质量流 | 识别为 1080p AVC 视频 + 128.6 kbps AAC 音频 | 通过 |
| 自动化测试 | `unittest discover` | 全部通过 | 12 项通过 | 通过 |
| 语法检查 | `compileall` | 无语法错误 | 无输出，退出码 0 | 通过 |
| Git 空白检查 | `git diff --check` | 无空白错误 | 退出码 0，仅提示现有换行转换策略 | 通过 |
| 真实视频下载 | 用户给定 BV 链接 | 最高可用画质、含音频、无临时残留 | 283,189,702 字节 MP4；1920×1080/25fps/H.264/AAC；格式 30080+30280 | 通过 |
| 独立媒体复核 | 下载成品 | 视频+音频、时长与大小正常 | 2 条流；1973.269 秒；283,189,702 字节 | 通过 |
| GUI 平台状态 | 切换到 Bilibili | 仅视频媒体、隐藏无关登录/参数 | 状态符合预期 | 通过 |
| 单文件打包 | `build_exe.ps1` | 生成可独立启动 EXE并内置全部依赖 | 200,281,429 字节；归档/启动检查通过 | 通过 |
| 未登录质量重验 | 最终质量常量 + 用户 BV 链接 | 不写死 1080p，选当前权限最高档 | 30080 1080p/25fps + 30280 AAC | 通过 |
| 登录 GUI 状态 | 切换到 Bilibili | 显示 Bilibili 登录/检查按钮，隐藏其他平台按钮 | 符合预期 | 通过 |
| Cookie 域边界 | 自动化测试 | 账号 Cookie 不进入媒体 CDN 请求 | 测试通过 | 通过 |
| 最终 EXE 打包 | 最终源码 | 内置依赖并可独立启动 | 200,286,867 字节；归档与启动检查通过 | 通过 |
| CookieJar 与日志回归 | 15 项自动化测试 | Cookie 仅作用于 Bilibili、提示去重、真实错误保留 | 15 项全部通过 | 通过 |
| 修复后真实格式解析 | `BV1oHNv6kEzB` | 最高可用格式不变，弃用警告消失 | 30080+30280；弃用提示 0 条、错误 0 条 | 通过 |
| YouTube 元数据与 EJS | `EvjZ7ckgYTg` | Deno/EJS 可解析完整格式且无运行时警告 | 31 个格式；最高 315 4K50；警告 0 条 | 通过 |
| YouTube 自动化测试 | 全项目测试集 | URL、质量、Cookie 域、调度、成品路径均通过 | 25 项全部通过 | 通过 |
| YouTube 真实最高质量下载 | `EvjZ7ckgYTg` | 账号当前可用最高视频+最佳音频、无重编码合并 | 315+140；3840×2160/50fps/VP9/AAC；244,696,376 字节 MKV | 通过 |
| YouTube Cookie 域边界 | 合成登录态 | 账号 Cookie 不进入 googlevideo CDN | youtube.com/google.com 可用；googlevideo.com 无 Cookie | 通过 |
| YouTube GUI 状态 | 切换到 YouTube | 仅视频媒体、显示 YouTube 登录入口、隐藏无关参数 | 状态符合预期 | 通过 |
| YouTube 最终 EXE 打包 | 最终源码 | 内置 Deno/EJS/yt-dlp/FFmpeg/FFprobe并可启动 | 244,045,149 字节；归档确认、隐藏启动 12 秒正常 | 通过 |
| YouTube 公开模式实时解析 | `EvjZ7ckgYTg` | 不读取登录态，仍选择公开最高格式 | `315+140`；3840×2160/50fps/VP9/AAC；警告 0 条 | 通过 |
| YouTube 登录移除回归 | 四平台 GUI + 25 项测试 | YouTube 无登录入口，其他平台登录功能不变 | GUI 断言、测试、语法、依赖与空白检查全部通过 | 通过 |
| YouTube 公开模式 EXE | 最终源码 | 内置全部运行依赖并可启动 | 244,038,713 字节；递归归档确认、隐藏启动 12 秒正常 | 通过 |
| YouTube 并行 Range 速度基准 | `EvjZ7ckgYTg` 格式 315 | 找出顺序传输瓶颈并验证有界并行 | 顺序 0.34 MiB/s；均衡 4 路 3.89 MiB/s；快速 8 路完整视频平均 7.55 MiB/s | 通过 |
| YouTube 优化后完整下载 | `EvjZ7ckgYTg` | 最高质量、音视频完整、失败可回退 | 44.132 秒；315+140；3840×2160/50fps/VP9/AAC；244,696,376 字节 | 通过 |
| YouTube 速度优化 EXE | 最终源码 | 内置依赖并可独立启动 | 244,045,920 字节；归档确认、隐藏启动 12 秒正常 | 通过 |

### 阶段 5：Bilibili 登录态与会员最高画质策略
- **状态：** complete
- 执行的操作：
  - 用户根据清晰度截图明确：4K/1080p60 为大会员档，希望登录后自动选择账号权限内最高档，而不是固定 1080p。
  - 核对当前实现：质量排序已满足“分辨率→帧率→码率”，但未提供 Bilibili 登录态，需补充独立登录 profile 与 Cookie 传递。
  - 经验总结技能已读取并完成相关旧条目检索；写回延后到新增登录能力完成后统一评估。
  - 新增 Bilibili 独立登录窗口、登录态读取、普通账号/大会员状态判断与任务日志。
  - Cookie 仅传给 `bilibili.com` 页面/API；媒体 CDN 请求显式删除 Cookie，并增加安全回归测试。
  - 重新验证未登录候选为 1080p/25fps + 最佳 AAC；质量策略不限制分辨率，按 `res → fps → br` 选择。
  - GUI 回归确认 Bilibili 登录与状态检查按钮显示，其他平台登录按钮隐藏，无关高级参数禁用。
- 创建/修改的文件：
  - `task_plan.md`
  - `findings.md`
  - `progress.md`
  - `downloaders/bilibili.py`
  - `services/task_runner.py`
  - `app.py`
  - `.gitignore`
  - `README.md`
  - `AGENTS.md`
  - `tests/test_bilibili.py`

### 阶段 6：文档、经验回顾与交付
- **状态：** complete
- 执行的操作：
  - 重打最终单文件 EXE，归档确认内置 yt-dlp/Bilibili 解析器/FFmpeg/FFprobe，隐藏启动 12 秒保持正常。
  - 按 `experience-summary-writer` 规则更新跨项目经验：补强 FFmpeg 打包旧条目，新增 CDN 轮换和 Windows 超时进程树条目；相关 B站 IDM 旧条目仍有效，无需删除。
  - 完成项目文档、规划文件、测试结果和交付物复核。
- 创建/修改的文件：
  - `dist/融合下载器.exe`（重新生成，Git 忽略）
  - `D:/Homework/Obsidian/经验总结.md`（跨项目经验更新）

## 错误日志
| 时间戳 | 错误 | 尝试次数 | 解决方案 |
|--------|------|---------|---------|
| 2026-07-17 | 规划 hooks 未自动生效 | 1 | 按技能文档手动执行流程 |
| 2026-07-17 | `rg` 直接接收 `*.spec` 在 Windows 返回路径语法错误 | 1 | 改用 glob 过滤或显式文件路径 |
| 2026-07-17 | URL 提取测试把中文逗号后的正文并入查询参数 | 1 | 增加中文标点截断并保留回归用例 |
| 2026-07-17 | 真实媒体下载被 10 秒外层命令时限终止 | 1 | 将外层时限改为 10 分钟并分段读取输出；临时目录负责清理中断文件 |
| 2026-07-17 | 外层终止后第一组 Python 子进程仍运行，形成重复任务 | 1 | 根据创建时间和父子 PID 精确停止遗留进程，不影响当前任务 |
| 2026-07-17 | 第二次下载在 AV1 流上超过 3 分钟仍为 0 字节 | 1 | 主动停止，改做 AV1/AVC 小范围连接诊断 |
| 2026-07-17 | 组合式强制停止/删除命令被安全策略拒绝 | 1 | 拆分为非强制停止与后续显式路径清理 |
| 2026-07-17 | 10 MiB 分段下载被 CDN 重置，3 次重试均失败 | 1 | 改为探测 512 KiB、1 MiB、4 MiB 的稳定性，不重复 10 MiB |
| 2026-07-17 | 4 MiB 配置下视频完成，但 yt-dlp 网络层的音频仍长期为 0 字节 | 1 | 保留解析/选格式能力，改用项目自有 requests Range 下载器获取视频和音频 |
| 2026-07-17 | 规划阶段状态补丁因上下文变化而两次校验失败 | 2 | 重读三个规划文件后按文件小范围合并，源码未受影响 |
| 2026-07-17 | 规划技能完成检查器对中文模板报告 0/0 阶段 | 1 | 人工核对 6 个阶段状态和全部验收证据，无未完成阶段 |
| 2026-07-22 | YouTube 最高视频流下载返回 HTTP 403 | 1 | 验证同格式 GET/Range 后改用 4 MiB HTTP 分段，第二次完整 4K50 下载成功 |

## 五问重启检查
| 问题 | 答案 |
|------|------|
| 我在哪里？ | 全部阶段已完成 |
| 我要去哪里？ | 交付给用户；会员 4K 待未来有大会员账号时实测 |
| 目标是什么？ | 集成 Bilibili 最高质量视频媒体下载并完成真实链接验证 |
| 我学到了什么？ | 见 `findings.md` |
| 我做了什么？ | 见上方会话记录 |
