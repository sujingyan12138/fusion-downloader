$ErrorActionPreference = "Stop"
$fixtureRoot = Join-Path ([IO.Path]::GetTempPath()) ("fusion-updater-fixture-" + [guid]::NewGuid().ToString("N"))
$current = Join-Path $fixtureRoot "Fusion Downloader.exe"
$updateDir = Join-Path $fixtureRoot "更新临时文件"
$staged = Join-Path $updateDir "Fusion.Downloader-v9.9.9.exe"
$helper = Join-Path (Split-Path $PSScriptRoot -Parent) "fusion_update_helper.ps1"

try {
    New-Item -ItemType Directory -Path $updateDir -Force | Out-Null
    $source = @"
using System.Threading;
public static class Program {
    [System.STAThread]
    public static void Main() { Thread.Sleep(15000); }
}
"@
    Add-Type -TypeDefinition $source -Language CSharp -OutputAssembly $current -OutputType WindowsApplication
    $oldHash = (Get-FileHash -LiteralPath $current -Algorithm SHA256).Hash
    Copy-Item -LiteralPath $current -Destination $staged
    $stream = [IO.File]::Open($staged, [IO.FileMode]::Append, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $bytes = [Text.Encoding]::ASCII.GetBytes("NEW-VERSION")
        $stream.Write($bytes, 0, $bytes.Length)
    }
    finally {
        $stream.Dispose()
    }
    $newHash = (Get-FileHash -LiteralPath $staged -Algorithm SHA256).Hash

    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $helper `
        -CurrentPath $current `
        -StagedPath $staged `
        -ParentPid 999999 `
        -CurrentVersion 1.0.0 `
        -LatestVersion 9.9.9
    if ($LASTEXITCODE -ne 0) {
        throw "Update helper exited with $LASTEXITCODE."
    }

    $backup = Join-Path $fixtureRoot "更新备份\Fusion.Downloader.previous.exe"
    $currentHash = (Get-FileHash -LiteralPath $current -Algorithm SHA256).Hash
    $backupHash = (Get-FileHash -LiteralPath $backup -Algorithm SHA256).Hash
    $running = @(
        Get-Process -ErrorAction SilentlyContinue | Where-Object {
            try { $_.Path -eq $current } catch { $false }
        }
    )
    try {
        if ($currentHash -ne $newHash) { throw "Current executable was not replaced by the staged version." }
        if ($backupHash -ne $oldHash) { throw "Backup does not match the old executable." }
        if ($running.Count -lt 1) { throw "Updated executable did not remain running for startup verification." }
        if (-not (Test-Path -LiteralPath (Join-Path $fixtureRoot "更新日志\latest-update.log"))) {
            throw "Update log was not created."
        }
        Write-Output "Update helper integration passed."
    }
    finally {
        foreach ($process in $running) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }

    $rollbackRoot = Join-Path $fixtureRoot "rollback"
    $rollbackCurrent = Join-Path $rollbackRoot "Fusion Downloader.exe"
    $rollbackUpdateDir = Join-Path $rollbackRoot "更新临时文件"
    $brokenStaged = Join-Path $rollbackUpdateDir "Fusion.Downloader-v10.0.0.exe"
    New-Item -ItemType Directory -Path $rollbackUpdateDir -Force | Out-Null
    $oldSource = @"
using System.Threading;
public static class RollbackOldProgram {
    [System.STAThread]
    public static void Main() { Thread.Sleep(15000); }
}
"@
    $brokenSource = @"
public static class BrokenNewProgram {
    [System.STAThread]
    public static void Main() { }
}
"@
    Add-Type -TypeDefinition $oldSource -Language CSharp -OutputAssembly $rollbackCurrent -OutputType WindowsApplication
    Add-Type -TypeDefinition $brokenSource -Language CSharp -OutputAssembly $brokenStaged -OutputType WindowsApplication
    $rollbackOldHash = (Get-FileHash -LiteralPath $rollbackCurrent -Algorithm SHA256).Hash

    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $helper `
        -CurrentPath $rollbackCurrent `
        -StagedPath $brokenStaged `
        -ParentPid 999999 `
        -CurrentVersion 9.9.9 `
        -LatestVersion 10.0.0
    if ($LASTEXITCODE -eq 0) {
        throw "Broken update unexpectedly passed startup verification."
    }

    $restoredHash = (Get-FileHash -LiteralPath $rollbackCurrent -Algorithm SHA256).Hash
    $rollbackBackup = Join-Path $rollbackRoot "更新备份\Fusion.Downloader.previous.exe"
    $restoredProcesses = @(
        Get-Process -ErrorAction SilentlyContinue | Where-Object {
            try { $_.Path -eq $rollbackCurrent } catch { $false }
        }
    )
    try {
        if ($restoredHash -ne $rollbackOldHash) { throw "Failed update did not restore the old executable." }
        if (Test-Path -LiteralPath $rollbackBackup) { throw "Rollback left the only old copy stranded in backup." }
        if ($restoredProcesses.Count -lt 1) { throw "Restored old executable was not restarted." }
        Write-Output "Update helper rollback integration passed."
    }
    finally {
        foreach ($process in $restoredProcesses) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
}
finally {
    Start-Sleep -Milliseconds 300
    $resolvedFixture = [IO.Path]::GetFullPath($fixtureRoot)
    $resolvedTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    if (-not $resolvedFixture.StartsWith($resolvedTemp, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Fixture path escaped the system temp directory."
    }
    if (Test-Path -LiteralPath $resolvedFixture) {
        Remove-Item -LiteralPath $resolvedFixture -Recurse -Force
    }
}
