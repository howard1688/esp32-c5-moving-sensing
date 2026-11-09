param([switch]$Demo, [string]$Replay = '', [int]$Port = 5500)
$ErrorActionPreference = 'Stop'
Push-Location (Join-Path $PSScriptRoot '..')
try {
    if (-not (Test-Path .venv/Scripts/python.exe)) {
        throw 'Run python -m venv .venv, then .venv/Scripts/python.exe -m pip install -r host/requirements.txt'
    }
    $receiverArgs = @('host/receiver.py', '--port', "$Port")
    if ($Demo) { $receiverArgs += '--demo' }
    if ($Replay) { $receiverArgs += @('--replay', $Replay) }
    & .venv/Scripts/python.exe @receiverArgs
    if ($LASTEXITCODE -ne 0) { throw "Receiver failed: $LASTEXITCODE" }
} finally { Pop-Location }
