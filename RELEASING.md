# 融合下载器发布与软件内更新

## 用户侧更新流程

软件不会后台自动更新。用户点击顶部 `检查更新` 后：

1. 只读取 `sujingyan12138/fusion-downloader` 最新的正式 GitHub Release；草稿和预发布版本不会自动安装。
2. 新版本号高于当前版本时，展示 Release 更新说明、EXE 名称和大小。
3. 用户确认后下载到软件同级的 `更新临时文件/`。
4. 文件大小和 GitHub Release 返回的 SHA-256 必须同时一致；缺少或不一致时拒绝安装。
5. 用户再次确认后，独立更新助手等待当前进程退出，把旧 EXE 保存到 `更新备份/Fusion.Downloader.previous.exe`，替换并启动新版。
6. 新版在 5 秒内退出时自动恢复旧 EXE；设置、登录态和下载结果始终留在原目录。

## 维护者发布步骤

1. 在 `app_version.py` 增加版本号，使用数字版本，例如 `2.3.0`。
2. 运行完整单元测试、语法检查和受影响功能的真实验收。
3. 运行 `build_exe.bat`，确认 `dist/融合下载器.exe` 实际启动，并确认归档包含 `fusion_update_helper.ps1`。
4. 打包脚本会同时生成固定发布资产 `dist/Fusion.Downloader.exe` 并输出 SHA-256；上传这个文件，不要手工改名其他旧包。
5. 在 GitHub 创建正式 Release，标签使用同一版本号，例如 `v2.3.0`；不要标记为 draft 或 prerelease。
6. 上传 `Fusion.Downloader.exe`，填写面向用户的更新说明。发布后通过 GitHub API 核对资产大小和 `digest` 中的 SHA-256。
7. 使用上一正式版本的 EXE 点击 `检查更新`，完成一次真实下载、校验、替换、重启和备份验证后再宣布发布完成。

不要把源码 ZIP 当作更新资产，也不要复用旧标签覆盖文件。若 Release 缺少 Windows EXE 或 SHA-256，软件只允许用户打开 Release 页面，不会自动安装。
