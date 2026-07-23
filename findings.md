# 发现与决策：融合下载器平台扩展

## TikTok 单作品新增需求（2026-07-23）
- 首阶段只接入公开单个作品链接，仅下载“视频媒体”；不做主页、收藏、评论、字幕或账号登录。
- 测试链接：`https://www.tiktok.com/@squidgamenetflix/video/7465383132565409070`。
- 沿用应用同级 `下载结果/`、Windows 安全文件名、稳定作品 ID、临时目录和 FFprobe 音视频验证。

## TikTok 当前验证与技术决策（2026-07-23）
- 当前虚拟环境为 `yt-dlp 2026.07.04`；测试链接未登录即可解析，标题 `Smile 🙂 #SquidGame2 #SquidGame`，作者 `squidgamenetflix`，时长 23 秒。
- 当前返回 11 个格式候选。最高项为 `bytevc1_1080p_542129-1`：1080×1920、H.265 + AAC、MP4、1,580,173 字节；默认解析也选择该格式。
- `download` 候选被明确标为 `watermarked` 且 preference 为 -2；其余转码候选 preference 为 -1。质量策略以 `bestvideo*+bestaudio/best` 为基础，先过滤 `format_note=watermarked`，再按 `res → fps → br` 排序；只有没有非水印候选时才允许最终 `/best` 回退。
- 当前链接无需 Cookie 或登录态即可解析；未安装 `curl-cffi` 时虽然能完成本次下载，但 yt-dlp 会提示缺少浏览器模拟目标，因此正式包加入该依赖。私密、区域限制或必须登录的作品暂不宣称支持。
- TikTok 当前最高候选已经同时含视频和音频，但实现仍保留 FFmpeg 路径以兼容未来分离流，并用 FFprobe 要求最终文件同时包含视频和音频。
- yt-dlp 当前文档建议嵌入 `YoutubeDL`、使用 `bv*+ba/b` 兼容合并流与分离流，并通过输出模板控制临时文件；`extract_info` 的内部对象不应被假定为普通 JSON。
- 第一次真实下载虽然拿到正确媒体，但 yt-dlp 明确提示“attempting impersonation, but no impersonate target is available”。因此将 `curl-cffi` 从“暂不需要”修正为正式依赖并纳入 PyInstaller 收集，避免其他网络环境只能依赖未模拟请求。
- 第一次真实下载的输出文件名包含 🙂；媒体已成功移动，但 PowerShell GBK 控制台在打印路径时抛出 `UnicodeEncodeError`，使任务被误报为失败。日志现在只对无法编码的字符做替换重试，不能让非关键日志破坏已完成下载。
- 修复后源码真实下载成功，报告为 `bytevc1_1080p_542129-1`、1080×1920、HEVC + AAC、23.317 秒、1,580,173 字节、`watermarked=false`；独立 FFprobe 得到相同音视频轨和尺寸。
- 最终单文件 EXE 为 250,112,193 字节。PyInstaller Analysis 与归档确认包含 `yt_dlp.extractor.tiktok`、`curl_cffi.requests.impersonate`、`curl_cffi/_wrapper.pyd`、Deno、FFmpeg 和 FFprobe。
- PyInstaller 的 `urllib3.contrib.emscripten/js` 与 `pycparser.lextab/yacctab` 警告来自非 Windows 条件分支或运行时生成路径；最终 EXE 已实际完成 TikTok 解析、模拟请求、下载和 FFprobe 验证，因此当前不属于阻塞性缺失。
- 将最终 EXE 复制到系统临时目录后进行 GUI 端到端回归：选择 TikTok 时只保留“视频媒体”和“单个”，批量按钮禁用，登录控件隐藏；粘贴测试链接后显示成功 1、失败 0，产物仍为 1080×1920 HEVC + AAC、1,580,173 字节。临时 EXE、媒体和目录均已清理。

## YouTube 下载速度优化（2026-07-22）
- 当前实现把 `max_workers` 只传给 `yt-dlp` 的 `concurrent_fragment_downloads`；测试链接格式 `315` 是普通分离 WebM 流，不一定是 HLS/DASH 分片清单，因此该选项可能无法为单个视频文件建立并行连接。
- 为规避之前的瞬时 403，当前固定启用 `http_chunk_size=4 MiB`。yt-dlp 的 HTTP 分段主要用于顺序 Range 请求；如果每段请求存在握手/首字节等待，它可能牺牲吞吐量换取稳定性。
- 旧经验只证明 4 MiB 分段能完成真实 4K50 下载，不证明它是当前网络下的最快策略；本轮必须分别测量不同 Range 大小和并行度后再决定。
- GUI 的 `stable/balanced/fast` 当前映射为不同 `max_workers`，但若 YouTube 普通 HTTP 流没有使用并行下载，这三个档位对单视频速度的影响会很小。
- 首次基准的 32 MiB 单 Range 在 `googlevideo.com` CDN 节点等待响应头 45 秒后超时，未进入有效传输；这说明简单放大分段甚至恢复单个长连接会降低稳定性，优化方向应是小块、有界并行和单块有限重试，而不是无限增大 chunk。
- 重新解析后格式 `315` 给出精确大小 242,730,822 字节；解析耗时 4.551 秒。相同签名 URL、相同 8 MiB 数据的对照结果：1 MiB 顺序 Range 为 0.34 MiB/s（23.41 秒），4 并行为 1.16 MiB/s（6.91 秒），8 并行为 2.38 MiB/s（3.36 秒），全部返回 206 且 Content-Range 正确。
- 当前网络下 8 路并行相对顺序 Range 提升约 7 倍，已证明瓶颈是单连接/顺序请求吞吐，而不是格式解析或 FFmpeg。下一步需选择兼顾重试成本的分块大小，并在实现中对不支持 Range、签名过期和单块失败保留 yt-dlp 稳定回退。
- 8 路并行的分块对照进一步显示：2 MiB 块达到 4.64 MiB/s，4 MiB 块达到 7.08 MiB/s，全部 206 且长度完整；因此保留已经验证过的 4 MiB 稳定块大小，只把“顺序”改为有界并行，避免重新引入 32 MiB 大连接超时。
- GUI 速度档位无需新增复杂选项：`stable=2`、`balanced=4`、`fast=8` 可直接作为单媒体流并行 Range 数；批量任务仍由调度器拆分总并发，避免每条视频都无上限占满连接。
- 默认 `balanced=4` 的独立实测为 16 MiB / 4.115 秒，即 3.89 MiB/s；即使用户不切到 `fast`，也明显高于原顺序基准 0.34 MiB/s。`fast=8` 的完整视频流实测平均 7.55 MiB/s。

## YouTube 新增需求（2026-07-22）
- 新增 YouTube 视频媒体下载，仅做视频下载能力。
- 仅下载公开可访问内容，并在公开格式中选择可获取的最高质量；不再提供 YouTube 登录功能。
- 使用测试链接：`https://www.youtube.com/watch?v=EvjZ7ckgYTg`。
- 必须继续内置 `yt-dlp` 与 FFmpeg/FFprobe，普通用户无需另行安装。

## YouTube 当前验证（2026-07-22）
- 测试链接可在未登录状态解析：标题 `Cristiano Ronaldo x Céline Dept: THE FULL MEETING`，时长 120 秒。
- 当前返回 31 个可下载格式，其中 23 个视频格式、4 个纯音频格式；最高视频为格式 `315`，3840×2160、50 fps、VP9、WebM，最高码率音频为格式 `140`，AAC/M4A 约 129.5 kbps。
- 当前 `yt-dlp 2026.07.04` 即使没有 JavaScript 运行时也能看到 4K50，但明确警告这种提取方式已弃用、部分格式可能缺失；因此不能把本次偶然成功当作长期“最高质量”保证。
- 官方 EJS 指南说明 YouTube 需要外部 JavaScript 运行时和 `yt-dlp-ejs` 挑战脚本；Deno 为推荐方案，Node 22+ 也受支持但需要显式启用。
- 当前开发机有 Node 24.13.0，没有 Deno、`yt-dlp-ejs` 或 `curl_cffi`；目标电脑不能假定已安装 Node，因此打包产物必须内置受支持的运行时和 EJS 脚本，或给出明确降级提示。
- 安装 `yt-dlp[default,deno]` 后获得 `yt-dlp-ejs 0.8.0` 与 Deno 2.9.3；Deno 可执行文件位于虚拟环境 `Scripts/deno.exe`，可由 PyInstaller 作为二进制捆绑。
- 显式传入 Deno 路径后，测试链接解析警告从 1 条降为 0 条，格式集合保持 31 个，最高仍为 `315` 4K50 VP9 + `140` AAC；证明运行时与 EJS 在当前版本组合中可用。
- 使用最低质量做临时下载实验时，`yt-dlp` 在 `requested_downloads[0].filepath` 返回合并后最终路径，临时目录只留下单个 `media.mkv`；可据此先验证成品再改名移动，不需要猜测合并后的扩展名。
- 现有调度和 GUI 对 Bilibili 的“仅视频媒体”分支是硬编码的；YouTube 接入时应把这一判断扩展为两个媒体平台，而不是让 YouTube 误用抖音/小红书的评论与收藏控件。
- YouTube 最高 4K VP9 与 AAC 的编码组合不能安全放入 MP4；统一让 FFmpeg 合并为 MKV 可避免转码或容器不兼容，满足最高质量优先，但系统播放器兼容性取决于目标电脑的 VP9/AV1 解码能力。
- 首次完整 4K 下载在视频流建立连接时返回 403；重新解析后对格式 `315/401/299/160/140/139` 的 64 KiB Range 均返回 206，且 4K 视频普通 GET 返回 200、4 MiB Range 返回 206，说明格式权限和签名解析本身可用，故将 4 MiB HTTP 分段作为针对临时 CDN/长连接波动的传输策略再验证。
- 采用 4 MiB HTTP 分段后，测试链接真实最高质量下载成功：`315` 3840×2160/50fps VP9 + `140` AAC，无重编码合并为 MKV；FFprobe 验证视频和音频均存在，成品 244,696,376 字节。
- 用户截图显示 Google 登录页明确拒绝当前自动化/调试浏览器，提示“此浏览器或应用可能不安全”。该问题属于登录入口兼容性，而非公开视频解析或媒体下载失败。
- 用户确认接受新的产品边界：删除 YouTube 登录入口和登录态依赖；年龄限制、私享、会员或其他必须登录的内容暂不支持。Deno/EJS、4 MiB 分段、最高质量排序、FFmpeg 无重编码合并与 FFprobe 验证继续保留。

## 需求
- 新增 Bilibili 视频媒体下载；本阶段不做评论、收藏、图文或其他扩展能力。
- 对每个视频选择当前条件下可获得的最高质量。
- 集成到现有项目，而不是另建独立工具。
- 使用测试链接：`https://www.bilibili.com/video/BV1oHNv6kEzB/`。
- 建立项目级 `AGENTS.md`；长期规则与当前任务状态分离保存。

## 研究发现
- 新版 `yt-dlp` 会把 `http_headers["Cookie"]` 视为潜在安全风险并重复发出弃用提示；当前日志中同一提示分别进入自定义 logger 的 `warning()` 与 `error()`，造成 21 组、42 行红字，但任务本身成功。
- `YoutubeDL.cookiejar` 可直接接收标准 `http.cookiejar.Cookie`；把浏览器登录态限定到 `.bilibili.com`、HTTPS 和根路径后，`yt-dlp` 可继续解析账号权限内清晰度，同时不会把 Cookie 发往 `bilivideo.com` 或其他 CDN。
- 修复后的真实不下载解析未出现任何 `yt-dlp` 弃用消息或错误，仍选择当前权限可见的 1080p AVC + AAC；源码运行环境与 `dist` EXE 使用不同的应用同级登录态目录，因此本次源码验证显示未登录，不代表 `dist/Bilibili浏览器登录态` 被删除或失效。
- 项目当前 README 描述抖音、小红书、统一 GUI、任务调度、IDM 自动检测和固定 `下载结果/` 输出目录。
- 根目录此前不存在 `AGENTS.md`、`CLAUDE.md`、`task_plan.md`、`findings.md` 或 `progress.md`。
- 初始 Git 工作区干净；依赖目前只有 `requests`、`pillow`、`websocket-client`。
- 先前只读性能审阅是历史假设来源，具体代码位置和现状仍需在当前 checkout 复核。
- 当前结构为 Tkinter `app.py`、统一 `services/task_runner.py` 与 `downloaders/` 下平台模块；GUI 平台选项目前只有抖音和小红书。
- `build_exe.ps1` 会在系统可找到时把 `ffmpeg.exe` 和 `ffprobe.exe` 加入打包产物，这为 Bilibili 分离音视频流合并提供了现有基础。
- 项目当前未发现测试目录或测试文件，需要在本次功能中补充可离线运行的核心测试。
- `TaskOptions` 是平台无关的任务参数；`extract_task_inputs()` 与 `run_task()` 目前通过平台中文名分支，Bilibili 可按相同契约加入独立 URL 批处理函数。
- `app.py` 的平台/功能选项、登录态逻辑与收藏控件带有两平台假设；新增 Bilibili 时必须让其仅提供“视频媒体”，并避免误入抖音登录/收藏分支。
- 现有批量调度 `run_url_batch()` 可复用，保持单条失败聚合而不中止整个批次。
- 当前打包配置中的 `ffmpeg/ffprobe` 路径来自构建时系统发现；运行时仍需明确检测，避免分离流只生成无声视频。
- 当前虚拟环境是 Python 3.14.4，尚未安装 `yt-dlp`；本机 `ffmpeg.exe` 与 `ffprobe.exe` 均可用，版本为 2026-01-29 构建。
- 根目录 `.gitignore` 已排除虚拟环境、构建产物、下载结果、spec 文件和两平台登录态；新增测试与 Bilibili 模块可正常纳入版本控制。
- GUI 登录按钮目前同时显示；Bilibili 无收藏/评论能力时应禁用或隐藏平台无关的登录、收藏及评论参数，避免用户误解。
- 已安装并验证 `yt-dlp` 2026.07.04 可在当前 Python 3.14.4 环境解析测试链接。
- 2026-07-17 对测试链接的未登录元数据验证：标题“寻找卢本伟”，作者“STN工作室”，时长约 1973 秒；最高可见视频流为 1920×1080、约 25 fps、AVC、估算约 250.8 MB，最佳音频为 AAC 128.6 kbps、估算约 31.7 MB。
- 测试链接的最高视频与音频是独立 DASH 流，必须用 FFmpeg 合并；当前环境无需登录即可看到 1080p，但更高权限/大会员专属清晰度仍受账号和源站权限限制。
- `yt-dlp` 默认质量排序在同为 1080p/25fps 时选择了格式 `100026`（AV1，约 507 kbps），而不是码率更高的 `30080`（AVC，约 1017 kbps）；若目标是画面质量和大部分电脑兼容性，应显式让码率优先于编码效率。
- 两次真实下载均在 AV1 媒体连接阶段长时间停留于 0 字节；第一次遗留任务稍后曾写入约 11 MB，说明并非解析失败，需要进一步区分 CDN 首包延迟、格式节点差异与下载器超时行为。
- 对格式 `100026`、`30080`、`30280` 分别请求前 64 KiB 均成功返回 HTTP 206，耗时约 4.0 秒、9.0 秒和 1.7 秒；问题集中在完整 GET/首包稳定性，不是链接或权限失效。
- 已用 `yt-dlp` 的 `format_sort=[res,fps,br]` 做不下载验证，实际选择 `30080`（1080p AVC）+ `30280`（最佳 AAC）。
- 实际使用 10 MiB HTTP 分段时，CDN 连续返回 `ConnectionResetError(10054)`；代码能正确转为中文任务失败且临时目录自动清理，但该分段大小在当前节点不可用。
- 对高码率 AVC 流实测 512 KiB、1 MiB、4 MiB Range 均返回完整 HTTP 206，耗时约 2.1、2.2、2.3 秒；选择 4 MiB 作为当前已验证的最大稳定分段。
- 4 MiB 下视频流可由 `yt-dlp` 网络层完整下载，但音频流再次长期停在 0 字节；同一音频 URL 经 `requests` Range 可快速响应，故最终将职责拆分为 `yt-dlp` 只解析/选格式、项目下载器负责分段字节、FFmpeg 负责合并。
- Bilibili 播放信息实际为每条测试流提供 3 个 CDN URL；`yt-dlp` 2026.07.04 的公开 format 只保留 `baseUrl`，未暴露 `backupUrl`。测试视频/音频均有 `mcdn`、`mountaintoys` 和 `mirrorcoso1` 节点。
- 最终真实下载成功：视频节点预检首段约 1.6 秒，1080p 视频流一次完成；音频节点预检约 1.7 秒，首节点中断一次后自动轮换并完成。成品 283,189,702 字节，1920×1080、25 fps、H.264 + AAC，格式为 `30080` + `30280`。

## 技术决策
| 决策 | 理由 |
|------|------|
| Bilibili 使用独立下载器模块并复用统一批量调度 | 符合现有平台边界，避免把解析逻辑写进 GUI |
| GUI 中 Bilibili 仅暴露“视频媒体” | 严格控制本次功能范围 |
| 最高质量允许分离视频和音频，但最终必须无重编码合并 | Bilibili 高画质通常采用 DASH，单一路径可能主动降质或无声 |
| 使用 `yt-dlp>=2026.7.4`，格式表达式采用最佳视频加最佳音频并保留单文件回退 | 站点解析变化频繁，需要成熟维护依赖；回退仍需经过音视频验证 |
| 成品落盘前运行 FFprobe 检查视频流和音频流 | 把“下载成功”与“可正常观看且有声音”区分开 |
| 同清晰度优先帧率与码率；HTTP Range 固定为已验证的 4 MiB | 兼顾最高画质、兼容性和当前 CDN 稳定性 |
| `yt-dlp` 只负责站点解析和格式选择，下载字节使用项目内可控 Range 下载器 | 避免目标机额外依赖，同时绕开当前音频流在 yt-dlp 网络层的停滞并保留可诊断重试 |
| 从同一 Bilibili 播放信息补取备用 URL，下载前健康探测，分段失败时轮换节点 | 默认 CDN 在当前网络不稳定，不能浪费源站已经提供的备用节点 |
| 真实下载中保留主/备节点轮换，即使预检成功也不假定后续大分段必然稳定 | 本次音频首段预检成功后正式下载仍中断一次，轮换机制实际生效并恢复 |
| Bilibili Cookie 仅用于官方页面/API 权限解析，签名媒体 URL 下载前显式删除 Cookie 头 | 登录能力不能以向第三方 CDN 泄露账号凭据为代价 |
| 登录 Cookie 通过 `YoutubeDL.cookiejar` 注入，并对弃用提示按内容去重 | 消除全局 Cookie Header 警告，保留登录权限和真正错误的可见性 |
| YouTube 使用 `yt-dlp[default,deno]` 并在 EXE 中捆绑 Deno | YouTube 挑战解析长期依赖 JS 运行时，不能假定目标电脑已安装 Node/Deno |
| YouTube 最高质量分轨统一无重编码合并为 MKV，成品经 FFprobe 验证后再移动 | 最高格式可能是 VP9/AV1 + AAC/Opus，MKV 能覆盖这些组合且不主动降质 |
| YouTube 普通 HTTP 媒体流使用 4 MiB 分段并保留 yt-dlp 有限重试 | 当前完整 GET 曾遇瞬时 403，而同一格式 Range 可用；分段后真实 4K50 下载通过 |

## 遇到的问题
| 问题 | 解决方案 |
|------|---------|
| 规划技能 hooks 未在当前 Codex 环境自动触发 | 手动执行恢复、阶段更新和完成检查 |
| Windows 下直接把 `*.spec` 作为 ripgrep 路径会报路径语法错误 | 改用 `-g '*.spec'` 或显式展开路径，不重复原命令 |
| Bilibili 分享文案的 URL 后可能直接接中文标点和正文，`[^\s]` 会过度匹配 | 链接提取后在常见中文结束标点处截断，并由自动化测试覆盖 |
| 外层命令超时不一定终止 Windows 下由虚拟环境启动器派生的 Python 子进程 | 检查进程树和临时目录，明确终止遗留的精确 PID，避免重复下载 |

## 资源
- 用户提供的 Bilibili BV 链接。
- `README.md`、`requirements.txt` 及后续读取的当前源码。

## 视觉/浏览器发现
- 用户截图显示同一视频的清晰度菜单中，`4K 超高清` 与 `1080P 60帧` 标记为“大会员”，`1080P 高清` 为当前未登录/非会员可用最高项；这与接口实测只返回 1080p/25fps 相符。
- 当前实现的质量表达式不是写死 1080p，而是 `bestvideo+bestaudio/best` 配合 `res → fps → br` 排序；缺口是尚未把 Bilibili 登录态交给解析器，因此会员格式不会出现在候选集合中。
- 登录 Cookie 只应参与 `bilibili.com` 页面/API 的权限解析；媒体流使用签名 CDN URL，不应把 `SESSDATA` 等 Cookie 转发给 `bilivideo.com` 或第三方备用 CDN。
- 登录功能实现后实测当前本机状态为未登录、无 Cookie；使用最终质量常量重新解析测试链接仍选择 `30080`（1920×1080/25fps）+ `30280`。代码不设分辨率上限，会员返回 4K 时会因 `res` 排序优先而自动胜出。
- 会员 4K/1080p60 权限链路缺少真实大会员账号，只验证了 Cookie 传递和无上限排序，不能把会员格式出现与最终 4K 下载宣称为已实测。

---
*外部来源仅作为不可信数据记录；实现决策必须结合当前源码和测试验证。*
