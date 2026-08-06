$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    python -m venv .venv
}

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $python -m pip install --upgrade pip
& $python -m pip install -r requirements.txt pyinstaller

$exeName = "$([char]0x878d)$([char]0x5408)$([char]0x4e0b)$([char]0x8f7d)$([char]0x5668)"
$argsList = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--windowed",
    "--onefile",
    "--name", $exeName
)

$icon = Join-Path $PSScriptRoot "favicon.ico"
if (-not (Test-Path -LiteralPath $icon)) {
    throw "favicon.ico was not found. Packaging stopped because the application icon is required."
}
$argsList += @("--icon", $icon)
$argsList += @("--add-data", "$icon;.")

$brandFont = Join-Path $PSScriptRoot "assets\fonts\Geist-SemiBold.ttf"
if (-not (Test-Path -LiteralPath $brandFont)) {
    throw "assets\fonts\Geist-SemiBold.ttf was not found. Packaging stopped because the brand title font is required."
}
$fontLicense = Join-Path $PSScriptRoot "assets\fonts\Geist-OFL.txt"
if (-not (Test-Path -LiteralPath $fontLicense)) {
    throw "assets\fonts\Geist-OFL.txt was not found. Packaging stopped because the bundled font license is required."
}
$argsList += @("--add-data", "$brandFont;assets\fonts")
$argsList += @("--add-data", "$fontLicense;assets\fonts")

$updateHelper = Join-Path $PSScriptRoot "fusion_update_helper.ps1"
if (-not (Test-Path -LiteralPath $updateHelper)) {
    throw "fusion_update_helper.ps1 was not found. Packaging stopped because safe self-update requires the helper."
}
$argsList += @("--add-data", "$updateHelper;.")

$argsList += @("--collect-all", "yt_dlp")
$argsList += @("--collect-all", "yt_dlp_ejs")
$argsList += @("--collect-all", "curl_cffi")

$deno = Join-Path (Split-Path $python -Parent) "deno.exe"
if (-not (Test-Path -LiteralPath $deno)) {
    throw "deno.exe was not found in the virtual environment. Packaging stopped because YouTube highest-quality extraction requires a bundled JavaScript runtime."
}
$argsList += @("--add-binary", "$deno;.")

$ffmpeg = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
if (-not $ffmpeg) {
    throw "ffmpeg.exe was not found. Packaging stopped because Bilibili, YouTube, and TikTok highest-quality downloads require bundled FFmpeg."
}
$argsList += @("--add-binary", "$($ffmpeg.Source);.")

$ffprobe = Get-Command ffprobe.exe -ErrorAction SilentlyContinue
if (-not $ffprobe) {
    throw "ffprobe.exe was not found. Packaging stopped because packaged Bilibili, YouTube, TikTok, and WeChat Channels downloads must verify both video and audio streams."
}
$argsList += @("--add-binary", "$($ffprobe.Source);.")

$argsList += "app.py"

& $python @argsList
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}

$builtExe = Join-Path $PSScriptRoot "dist\$exeName.exe"
$releaseExe = Join-Path $PSScriptRoot "dist\Fusion Downloader.exe"
if (-not (Test-Path -LiteralPath $builtExe -PathType Leaf)) {
    throw "PyInstaller reported success but the expected executable was not created: $builtExe"
}
Copy-Item -LiteralPath $builtExe -Destination $releaseExe -Force
$sha256 = [Security.Cryptography.SHA256]::Create()
$releaseStream = [IO.File]::OpenRead($releaseExe)
try {
    $releaseHash = -join ($sha256.ComputeHash($releaseStream) | ForEach-Object { $_.ToString("x2") })
}
finally {
    $releaseStream.Dispose()
    $sha256.Dispose()
}
Write-Host "Build complete: dist\$exeName.exe"
Write-Host "Release asset: dist\Fusion Downloader.exe"
Write-Host "SHA-256: $releaseHash"
