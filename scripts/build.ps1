[CmdletBinding()]
param(
    [string]$PythonExe = 'python',
    [switch]$StopRunning
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$ExePath = Join-Path $Root 'TokenWatcher.exe'
$DistPath = Join-Path $Root 'dist\TokenWatcher.exe'

if ($StopRunning) {
    Get-Process TokenWatcher -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Milliseconds 300
}

Push-Location $Root
try {
    & $PythonExe -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name TokenWatcher `
        --distpath (Join-Path $Root 'dist') `
        --workpath (Join-Path $Root 'build\TokenWatcher') `
        --specpath $Root `
        (Join-Path $Root 'src\token_watcher.py')

    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }
    Copy-Item -LiteralPath $DistPath -Destination $ExePath -Force
    Write-Output "Built: $ExePath"
}
finally {
    Pop-Location
}
