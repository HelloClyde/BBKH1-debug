param([ValidatePattern('^[A-Za-z]$')][string]$DriveLetter = 'F')
$ErrorActionPreference = 'Stop'
$rdWorkspace = Split-Path -Parent $PSScriptRoot
$rdSource = Join-Path $rdWorkspace 'dist\H1Debug.bda'
# ELF symbol metadata can contain an empty-name symbol, which Windows
# PowerShell's ConvertFrom-Json rejects. Use the build's exact checksum file.
$rdChecksum = Get-Content -LiteralPath (Join-Path $rdWorkspace 'dist\H1Debug.bda.sha256') -Raw
$rdMatch = [regex]::Match($rdChecksum, '\A([0-9a-fA-F]{64})  H1Debug\.bda\s*\z')
if (-not $rdMatch.Success) { throw 'Invalid H1Debug.bda.sha256 file' }
$rdExpected = $rdMatch.Groups[1].Value.ToUpperInvariant()
if ((Get-FileHash -LiteralPath $rdSource -Algorithm SHA256).Hash -ne $rdExpected) { throw 'Source BDA/build checksum mismatch' }
$rdPartition = Get-Partition -DriveLetter $DriveLetter -ErrorAction SilentlyContinue
$rdDisk = if ($rdPartition) { $rdPartition | Get-Disk } else { $null }
$rdUsb = Get-PnpDevice -PresentOnly | Where-Object { $_.Status -eq 'OK' -and $_.InstanceId -like 'USB\VID_07C4&PID_A4A5\*' }
$rdDirectory = $DriveLetter + ':\应用\程序'
if (-not $rdUsb -or -not $rdDisk -or $rdDisk.BusType -ne 'USB' -or $rdDisk.FriendlyName -notlike '*BBK*' -or -not (Test-Path -LiteralPath $rdDirectory -PathType Container)) {
    throw 'No matching BBK H1 USB disk/application directory. Connect the H1 in ordinary disk mode first.'
}
$rdDestination = Join-Path $rdDirectory 'H1Debug.bda'
$rdPending = Join-Path $rdDirectory 'H1Debug.bda.new'
if (Test-Path -LiteralPath $rdPending) { throw 'Pending H1Debug.bda.new already exists; preserve it for inspection.' }
$rdWork = Join-Path $rdWorkspace 'build\h1-debug'
New-Item -ItemType Directory -Path $rdWork -Force | Out-Null
$rdBackup = $null
if (Test-Path -LiteralPath $rdDestination) {
    $rdOldHash = (Get-FileHash -LiteralPath $rdDestination -Algorithm SHA256).Hash
    $rdBackup = Join-Path $rdWork ('device-before-' + (Get-Date -Format 'yyyyMMdd-HHmmss-ffff') + '.bda')
    Copy-Item -LiteralPath $rdDestination -Destination $rdBackup
    if ((Get-FileHash -LiteralPath $rdBackup -Algorithm SHA256).Hash -ne $rdOldHash) { throw 'Backup checksum mismatch' }
}
Copy-Item -LiteralPath $rdSource -Destination $rdPending
if ((Get-FileHash -LiteralPath $rdPending -Algorithm SHA256).Hash -ne $rdExpected) { throw 'Pending copy checksum mismatch' }
Move-Item -LiteralPath $rdPending -Destination $rdDestination -Force
if ((Get-FileHash -LiteralPath $rdDestination -Algorithm SHA256).Hash -ne $rdExpected) { throw 'Installed checksum mismatch' }
$rdReceipt = [pscustomobject]@{ path=$rdDestination; sha256=$rdExpected; previous_backup=$rdBackup; hardware_verified=$false }
$rdReceipt | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $rdWork 'installation.json') -Encoding utf8
$rdReceipt | ConvertTo-Json
Write-Output 'Safely eject and unplug USB. Open H1调试, return to the menu, then reconnect USB for PC control.'
