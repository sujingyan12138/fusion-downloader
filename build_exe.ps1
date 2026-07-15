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

$ffmpeg = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
if ($ffmpeg) {
    $argsList += @("--add-binary", "$($ffmpeg.Source);.")
}

$ffprobe = Get-Command ffprobe.exe -ErrorAction SilentlyContinue
if ($ffprobe) {
    $argsList += @("--add-binary", "$($ffprobe.Source);.")
}

$argsList += "app.py"

& $python @argsList
Write-Host "Build complete: dist\$exeName.exe"
