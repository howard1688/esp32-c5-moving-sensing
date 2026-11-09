param(
    [ValidateSet(1,2)][int]$Node = 1,
    [string]$Port = '',
    [switch]$Flash,
    [switch]$Monitor,
    [switch]$Menuconfig,
    [switch]$ListPorts,
    [string]$IdfActivation = 'C:\Espressif\tools\Microsoft.v6.0.2.PowerShell_profile.ps1'
)
$ErrorActionPreference = 'Stop'
if (-not (Get-Command idf.py -ErrorAction SilentlyContinue)) {
    if (-not (Test-Path -LiteralPath $IdfActivation)) {
        throw 'Open an ESP-IDF 6.0 PowerShell terminal or supply -IdfActivation.'
    }
    . $IdfActivation
}
# Use the IDF Python (which includes pyserial), not the viewer virtualenv.
$serialPython = if ($env:IDF_PYTHON_ENV_PATH) {
    Join-Path $env:IDF_PYTHON_ENV_PATH 'Scripts/python.exe'
} else { (Get-Command python -ErrorAction Stop).Source }
function Get-CsiUsbPorts {
    $portJson = & $serialPython (Join-Path $PSScriptRoot 'list_serial_ports.py')
    if ($LASTEXITCODE -ne 0) { throw 'Could not enumerate USB ports with the ESP-IDF Python.' }
    return @($portJson | ConvertFrom-Json | Where-Object { $_.port })
}
if ($ListPorts) {
    $available = @(Get-CsiUsbPorts)
    if ($available.Count -eq 0) { Write-Host 'No USB serial devices found.' }
    else { $available | Format-Table port,kind,description,serial -AutoSize }
    exit 0
}
if (($Flash -or $Monitor) -and -not $Port) {
    while (-not $Port) {
        $available = @(Get-CsiUsbPorts)
        if ($available.Count -eq 1) {
            $Port = $available[0].port
            Write-Host "Auto-selected $Port [$($available[0].kind)] serial=$($available[0].serial) for esp32c5-$Node"
        } elseif ($available.Count -eq 0) {
            Write-Host 'No USB serial device found. Connect the intended ESP32 using a data USB cable.'
            $choice = Read-Host 'Press Enter to scan again, or Q to cancel'
            if ($choice -match '^[qQ]$') { throw 'Cancelled; no board was flashed.' }
        } else {
            Write-Host "Multiple USB serial devices found. Select the board to become esp32c5-${Node}:"
            for ($i = 0; $i -lt $available.Count; $i++) {
                $p = $available[$i]
                Write-Host "[$($i + 1)] $($p.port) | $($p.kind) | $($p.description) | serial=$($p.serial)"
            }
            $choice = Read-Host 'Enter number, R to rescan after unplugging other boards, or Q to cancel'
            if ($choice -match '^[qQ]$') { throw 'Cancelled; no board was flashed.' }
            $index = 0
            if ([int]::TryParse($choice, [ref]$index) -and $index -ge 1 -and $index -le $available.Count) {
                $Port = $available[$index - 1].port
            }
        }
    }
}
$projectPath = Join-Path $PSScriptRoot '../firmware'
$env:IDF_CCACHE_ENABLE = '0'
Push-Location $projectPath
try {
    $idfArgs = @('-B', "build-esp32c5-$Node", '-D', "SDKCONFIG=sdkconfig.esp32c5-$Node",
        '-D', "SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.node$Node", '-D', 'IDF_TARGET=esp32c5')
    if ($Port) { $idfArgs += @('-p', $Port) }
    if ($Menuconfig) { $idfArgs += 'menuconfig' } else { $idfArgs += @('reconfigure', 'build') }
    if ($Flash) {
        $idfArgs += 'flash'
    }
    if ($Monitor) { $idfArgs += 'monitor' }
    idf.py @idfArgs
    if ($LASTEXITCODE -ne 0) { throw "idf.py failed: $LASTEXITCODE" }
} finally { Pop-Location }
