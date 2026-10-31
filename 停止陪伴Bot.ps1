$ErrorActionPreference = 'Stop'
$targetScript = Join-Path $PSScriptRoot 'start.py'
$owned = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($targetScript) }
foreach ($process in $owned) {
    Stop-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
}
Write-Output 'Companion Bot stopped. PostgreSQL service is unchanged.'
