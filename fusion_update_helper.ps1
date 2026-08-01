param(
    [Parameter(Mandatory = $true)][string]$CurrentPath,
    [Parameter(Mandatory = $true)][string]$StagedPath,
    [Parameter(Mandatory = $true)][int]$ParentPid,
    [Parameter(Mandatory = $true)][string]$CurrentVersion,
    [Parameter(Mandatory = $true)][string]$LatestVersion
)

$ErrorActionPreference = "Stop"
$current = [IO.Path]::GetFullPath($CurrentPath)
$staged = [IO.Path]::GetFullPath($StagedPath)
$appDir = [IO.Path]::GetDirectoryName($current)
$updateDir = [IO.Path]::GetFullPath((Join-Path $appDir "更新临时文件"))
$backupDir = Join-Path $appDir "更新备份"
$logDir = Join-Path $appDir "更新日志"
$backup = Join-Path $backupDir "Fusion.Downloader.previous.exe"
$logPath = Join-Path $logDir "latest-update.log"

New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

function Write-UpdateLog([string]$Message) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    [IO.File]::AppendAllText($logPath, $line + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
}

try {
    if (-not $staged.StartsWith($updateDir + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Staged executable is outside the owned update directory."
    }
    if (-not (Test-Path -LiteralPath $current -PathType Leaf)) {
        throw "Current executable does not exist."
    }
    if (-not (Test-Path -LiteralPath $staged -PathType Leaf)) {
        throw "Staged executable does not exist."
    }

    Write-UpdateLog "Waiting for version $CurrentVersion to exit before installing $LatestVersion."
    try {
        Wait-Process -Id $ParentPid -Timeout 120 -ErrorAction Stop
    }
    catch {
        if (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue) {
            throw "The application did not exit within 120 seconds."
        }
    }

    if (Test-Path -LiteralPath $backup) {
        Remove-Item -LiteralPath $backup -Force
    }
    Move-Item -LiteralPath $current -Destination $backup
    try {
        Move-Item -LiteralPath $staged -Destination $current
        $newProcess = Start-Process -FilePath $current -WorkingDirectory $appDir -PassThru -WindowStyle Normal
        Start-Sleep -Seconds 5
        if ($newProcess.HasExited) {
            throw "The updated application exited during startup verification."
        }
        Write-UpdateLog "Update to $LatestVersion installed and startup verification passed. Backup: $backup"
    }
    catch {
        if (Test-Path -LiteralPath $current) {
            Remove-Item -LiteralPath $current -Force
        }
        Move-Item -LiteralPath $backup -Destination $current
        Start-Process -FilePath $current -WorkingDirectory $appDir -WindowStyle Normal | Out-Null
        throw
    }
}
catch {
    Write-UpdateLog ("Update failed: " + $_.Exception.Message)
    exit 1
}

exit 0
