$ErrorActionPreference = 'Stop'
$config = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'local_config.json') | ConvertFrom-Json
Start-Process "http://127.0.0.1:$($config.port)/companion/qq"
