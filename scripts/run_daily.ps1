[CmdletBinding()]
param(
    [string]$ProjectPath = (Split-Path -Parent $PSScriptRoot),
    [string]$Date,
    [switch]$Force,
    [switch]$Sync,
    [string]$LogPath
)

$ErrorActionPreference = 'Stop'

try {
    $project = (Resolve-Path -LiteralPath $ProjectPath).Path
    $composeFile = Join-Path $project 'docker-compose.yml'
    $envFile = Join-Path $project '.env'
    if (-not (Test-Path -LiteralPath $composeFile -PathType Leaf)) {
        throw "docker-compose.yml not found: $composeFile"
    }
    if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
        throw ".env not found: $envFile"
    }
    if ($Date -and $Date -notmatch '^\d{4}-\d{2}-\d{2}$') {
        throw '-Date must use YYYY-MM-DD.'
    }

    $docker = (Get-Command docker.exe -ErrorAction Stop).Source
    $arguments = @(
        'compose', '--project-directory', $project,
        '--env-file', $envFile,
        '-f', $composeFile,
        'run', '--rm', 'analyzer'
    )
    if ($Date) { $arguments += @('--date', $Date) }
    if ($Force) { $arguments += '--force' }
    # Skip the batch queue when the report is needed today rather than cheaply.
    if ($Sync) { $arguments += '--sync' }

    # In Windows PowerShell 5.1 native stderr is not a PowerShell exception.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    if ($LogPath) {
        $logParent = Split-Path -Parent $LogPath
        if ($logParent -and -not (Test-Path -LiteralPath $logParent -PathType Container)) {
            throw "Log directory not found: $logParent"
        }
        & $docker @arguments 2>&1 | Tee-Object -FilePath $LogPath -Append
    }
    else {
        & $docker @arguments
    }
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousPreference
    exit $exitCode
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
