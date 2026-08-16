# Install Piper, the local neural voice.
#
# Windows ships only the old SAPI voices (David / Hazel / Zira), which sound
# robotic enough to be unpleasant to listen to all day. Piper sounds far better
# and still runs entirely on-device. This downloads the package and one voice
# model, then verifies synthesis actually works before switching the config
# over -- so a failed download leaves you on a working voice rather than a
# silent one.
#
# setup.ps1 calls this for you. Run it directly only to add another voice:
#   .\install_piper.ps1 -Voice en_US-amy-medium
# Browse them at https://rhasspy.github.io/piper-samples/

param(
    [string]$Voice = ''
)

$ErrorActionPreference = 'Stop'
$Base = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Base

$Py       = Join-Path $Base '.venv\Scripts\python.exe'
$PiperDir = Join-Path $Base 'piper'
$VoiceDir = Join-Path $PiperDir 'voices'
$CfgPath  = Join-Path $Base 'config.json'

if (-not (Test-Path $Py)) { throw 'Run .\setup.ps1 first.' }

$cfg = Get-Content $CfgPath -Raw | ConvertFrom-Json
if (-not $Voice) { $Voice = $cfg.tts.piper_voice }

Write-Host ''
Write-Host "Installing Piper with voice '$Voice'..." -ForegroundColor Cyan

New-Item -ItemType Directory -Force -Path $VoiceDir | Out-Null

Write-Host '[1/4] Installing the piper package...' -ForegroundColor Yellow
uv pip install --python $Py piper-tts
if ($LASTEXITCODE -ne 0) { throw 'Could not install piper-tts.' }

Write-Host '[2/4] Downloading the voice model...' -ForegroundColor Yellow
# Voices live under: <lang>/<locale>/<name>/<quality>/<locale>-<name>-<quality>.onnx
$parts = $Voice -split '-'
if ($parts.Count -ne 3) { throw "Voice name must look like 'en_GB-alba-medium', got '$Voice'." }
$locale  = $parts[0]
$name    = $parts[1]
$quality = $parts[2]
$lang    = ($locale -split '_')[0]
$root    = "https://huggingface.co/rhasspy/piper-voices/resolve/main/$lang/$locale/$name/$quality"

# Use curl.exe, not Invoke-WebRequest: PowerShell 5.1 silently truncates large
# downloads (it cut the 63MB model off at 37MB), which fails as corrupt protobuf.
foreach ($ext in @('onnx', 'onnx.json')) {
    $dest = Join-Path $VoiceDir "$Voice.$ext"
    $url  = "$root/$Voice.$ext`?download=true"

    $expected = 0
    try {
        $head = Invoke-WebRequest -Uri $url -Method Head -UseBasicParsing
        $expected = [int64]$head.Headers['Content-Length']
    } catch { }

    if ((Test-Path $dest) -and $expected -gt 0 -and (Get-Item $dest).Length -eq $expected) {
        Write-Host "      have $Voice.$ext" -ForegroundColor DarkGray
        continue
    }

    Write-Host "      fetching $Voice.$ext ($([math]::Round($expected/1MB,1)) MB)"
    & curl.exe -sSL --fail -o $dest $url
    if ($LASTEXITCODE -ne 0) { throw "Download failed for $Voice.$ext" }

    $got = (Get-Item $dest).Length
    if ($expected -gt 0 -and $got -ne $expected) {
        Remove-Item $dest -Force
        throw "$Voice.$ext downloaded incomplete ($got of $expected bytes)."
    }
}

Write-Host '[3/4] Creating the launcher shim...' -ForegroundColor Yellow
$shim = "@echo off`r`n`"$Py`" -m piper %*`r`n"
Set-Content -Path (Join-Path $PiperDir 'piper.cmd') -Value $shim -Encoding ascii

Write-Host '[4/4] Verifying synthesis...' -ForegroundColor Yellow
$testWav = Join-Path $env:TEMP 'piper-check.wav'
$model   = Join-Path $VoiceDir "$Voice.onnx"
'Piper is working.' | & $Py -m piper --model $model --output_file $testWav

if ((Test-Path $testWav) -and ((Get-Item $testWav).Length -gt 1000)) {
    Remove-Item $testWav -Force
    $cfg.tts.engine = 'piper'
    $cfg.tts.piper_voice = $Voice
    $cfg | ConvertTo-Json -Depth 10 | Set-Content $CfgPath -Encoding utf8
    Write-Host ''
    Write-Host "Piper is live. config.json now uses engine='piper', voice='$Voice'." -ForegroundColor Green
} else {
    Write-Host ''
    Write-Host 'Piper produced no audio. Leaving config on the SAPI engine.' -ForegroundColor Red
    Write-Host 'Speech still works, just with the built-in Windows voice.' -ForegroundColor Red
    exit 1
}
