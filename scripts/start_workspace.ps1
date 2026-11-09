param(
    [ValidateRange(1,8)][int]$Hours = 4,
    [ValidateRange(1024,65535)][int]$Port = 5175,
    [string]$CondaPython = 'C:\ProgramData\anaconda3\python.exe'
)
$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectPath
if (-not (Test-Path -LiteralPath $CondaPython)) { throw 'Existing Anaconda Python not found. Pass -CondaPython.' }
$recordPath = Join-Path $projectPath ("var\preview\writable\services-" + $Port + ".json")
if (Test-Path -LiteralPath $recordPath) {
    try {
        $record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
        if (-not $record.stopped -and $record.url -eq ("http://127.0.0.1:" + $Port + "/") -and (Get-Process -Id $record.controller_pid -ErrorAction SilentlyContinue)) {
            $response = Invoke-WebRequest -Uri $record.url -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -eq 200) {
                Write-Host ("Workspace is already running: " + $record.url)
                Start-Process $record.url
                exit 0
            }
        }
    } catch { }
}
$env:PYTHONDONTWRITEBYTECODE = '1'

& $CondaPython -B (Join-Path $PSScriptRoot 'launch_workspace.py') --hours $Hours --port $Port --open-browser
exit $LASTEXITCODE
