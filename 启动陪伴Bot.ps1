$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$guiExe = Join-Path $PSScriptRoot 'runtime\desktop\CompanionBot\CompanionBot.exe'
if (Test-Path -LiteralPath $guiExe) {
    Start-Process -FilePath $guiExe -WorkingDirectory $PSScriptRoot
} else {
    Start-Process -FilePath "$PSScriptRoot\.venv\Scripts\pythonw.exe" -ArgumentList @("`"$PSScriptRoot\desktop_entry.py`"") -WorkingDirectory $PSScriptRoot
}
