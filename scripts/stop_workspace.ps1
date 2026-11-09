param([ValidateRange(1024,65535)][int]$Port = 5175)
$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectPath
& 'C:\ProgramData\anaconda3\python.exe' -B (Join-Path $PSScriptRoot 'launch_workspace.py') --stop --port $Port
exit $LASTEXITCODE
