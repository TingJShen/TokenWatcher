[CmdletBinding()]
param(
    [string]$PythonExe = 'python',
    [switch]$StopRunning
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$ExePath = Join-Path $Root 'TokenWatcher.exe'
$RuntimePath = Join-Path $Root 'TokenWatcher.runtime'
$DistPath = Join-Path $Root 'dist\TokenWatcher'
$LauncherSource = Join-Path $Root 'launcher\TokenWatcherLauncher.cs'

function Assert-WorkspaceChildPath([string]$Path) {
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $pathFull = [IO.Path]::GetFullPath($Path)
    $prefix = $rootFull + [IO.Path]::DirectorySeparatorChar
    if (-not $pathFull.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify path outside the repository: $pathFull"
    }
}

if ($StopRunning) {
    Get-Process TokenWatcher -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Milliseconds 300
}

Push-Location $Root
try {
    & $PythonExe -m PyInstaller `
        --noconfirm `
        --clean `
        --onedir `
        --windowed `
        --exclude-module numpy `
        --name TokenWatcher `
        --distpath (Join-Path $Root 'dist') `
        --workpath (Join-Path $Root 'build\TokenWatcher') `
        --specpath $Root `
        (Join-Path $Root 'src\token_watcher.py')

    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }

    Assert-WorkspaceChildPath $RuntimePath
    if (Test-Path -LiteralPath $RuntimePath) {
        Remove-Item -LiteralPath $RuntimePath -Recurse -Force
    }
    Copy-Item -LiteralPath $DistPath -Destination $RuntimePath -Recurse

    $cscCandidates = @(
        (Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'),
        (Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe')
    )
    $CscExe = $cscCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $CscExe) {
        throw 'The Windows C# compiler was not found.'
    }

    & $CscExe `
        '/nologo' `
        '/target:winexe' `
        '/optimize+' `
        '/reference:System.Windows.Forms.dll' `
        "/out:$ExePath" `
        $LauncherSource
    if ($LASTEXITCODE -ne 0) {
        throw "Launcher compilation failed with exit code $LASTEXITCODE."
    }

    Write-Output "Built launcher: $ExePath"
    Write-Output "Built runtime:  $RuntimePath"
}
finally {
    Pop-Location
}
