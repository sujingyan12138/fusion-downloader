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

$argsList += @("--collect-all", "yt_dlp")

$ffmpeg = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
if (-not $ffmpeg) {
    throw "ffmpeg.exe was not found. Packaging stopped because Bilibili highest-quality downloads require bundled FFmpeg."
}
$argsList += @("--add-binary", "$($ffmpeg.Source);.")

$ffprobe = Get-Command ffprobe.exe -ErrorAction SilentlyContinue
if (-not $ffprobe) {
    throw "ffprobe.exe was not found. Packaging stopped because packaged downloads must verify both video and audio streams."
}
$argsList += @("--add-binary", "$($ffprobe.Source);.")

$argsList += "app.py"

& $python @argsList
Write-Host "Build complete: dist\$exeName.exe"
