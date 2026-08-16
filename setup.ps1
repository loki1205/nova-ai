<#
.SYNOPSIS
  Install Nova on this machine.

.DESCRIPTION
  Builds the virtual environment, installs dependencies, fetches the speech
  models, and checks that the Claude CLI is reachable. Everything is local to
  this folder -- copy it anywhere and run this.

  The models are the slow part: ~500 MB for Whisper small.en and ~60 MB for the
  Piper voice, downloaded once into models\ and piper\ (both gitignored).

.PARAMETER Force
  Rebuild the virtual environment even if one exists.

.PARAMETER Voice
  Piper voice to install. Default en_GB-alba-medium.
  Browse them at https://rhasspy.github.io/piper-samples/
#>
[CmdletBinding()]
param(
    [switch]$Force,
    [string]$Voice = 'en_GB-alba-medium'
)

$ErrorActionPreference = 'Stop'
$root   = $PSScriptRoot
$venv   = Join-Path $root '.venv'
$python = Join-Path $venv 'Scripts\python.exe'

function Step($t) { Write-Host "`n== $t" -ForegroundColor Cyan }
function Good($t) { Write-Host "   $t" -ForegroundColor Green }
function Warn2($t){ Write-Host "   $t" -ForegroundColor Yellow }
function Bad($t)  { Write-Host "   $t" -ForegroundColor Red }

# --- python ------------------------------------------------------------------
Step 'Finding a Python interpreter'

# From `python -V`, not `python -c`: Windows PowerShell mangles double quotes
# inside arguments to native executables, so any probe containing a Python
# string literal arrives as a syntax error and looks like a missing interpreter.
function Get-PyVersion($exe) {
    try {
        $raw = & $exe -V
        if ($LASTEXITCODE -eq 0 -and $raw -match 'Python\s+(\d+)\.(\d+)') {
            return [version]("{0}.{1}" -f $Matches[1], $Matches[2])
        }
    } catch { }
    return $null
}

$best = $null; $bestV = $null
foreach ($name in @('python', 'python3', 'py')) {
    $found = Get-Command $name -ErrorAction SilentlyContinue
    if (-not $found) { continue }
    # Zero-byte Microsoft Store alias stubs advertise the Store on stderr and
    # report no version, which reads exactly like a broken interpreter.
    if ($found.Source -like '*\WindowsApps\*') {
        $item = Get-Item $found.Source -ErrorAction SilentlyContinue
        if (-not $item -or $item.Length -lt 100KB) { continue }
    }
    $v = Get-PyVersion $found.Source
    if ($v -and $v -ge [version]'3.10' -and (-not $bestV -or $v -gt $bestV)) {
        $best = $found.Source; $bestV = $v
    }
}
if (-not $best) { Bad 'Python 3.10+ required. Install from https://python.org'; exit 1 }
Good "Python $bestV"

# --- venv --------------------------------------------------------------------
Step 'Virtual environment'
if ((Test-Path $python) -and -not $Force) {
    Good '.venv already exists (use -Force to rebuild)'
} else {
    if (Test-Path $venv) { Remove-Item -Recurse -Force $venv }
    & $best -m venv $venv
    Good "created"
}

Step 'Dependencies'
& $python -m pip install --quiet --upgrade pip
& $python -m pip install --quiet -r (Join-Path $root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { Bad 'pip install failed'; exit 1 }
Good 'installed'

# --- config ------------------------------------------------------------------
# Never overwritten. This is where your microphone, voice and working directory
# live, and re-running setup should not throw them away.
Step 'Settings'
$config = Join-Path $root 'config.json'
if (Test-Path $config) {
    Good 'config.json already exists (left untouched)'
} else {
    Copy-Item (Join-Path $root 'config.example.json') $config
    Good 'created config.json from config.example.json'
}

# --- claude cli --------------------------------------------------------------
Step 'Claude CLI'
$claude = Get-Command claude -ErrorAction SilentlyContinue
if (-not $claude) {
    Warn2 'Not on PATH. Nova needs it to think.'
    Warn2 'Install Claude Code, or set claude_path in config.json.'
} else {
    Good $claude.Source
    $servers = & claude mcp list 2>$null | Select-String -Pattern '^desktop:'
    if ($servers -match 'Connected') {
        Good 'desktop-control is connected -- Nova will have hands'
    } else {
        Warn2 'The desktop MCP server is not registered.'
        Warn2 'Nova will still talk, but it will not be able to act.'
        Warn2 'Install desktop-control and run its setup.ps1 to enable that.'
    }
}

# --- models ------------------------------------------------------------------
Step 'Speech models'

$voiceFile = Join-Path $root "piper\voices\$Voice.onnx"
if (Test-Path $voiceFile) {
    Good "Piper voice $Voice already present"
} else {
    Warn2 "Downloading Piper voice $Voice (~60 MB)..."
    & (Join-Path $root 'install_piper.ps1') -Voice $Voice
}

# Whisper downloads on first use; doing it here means the first thing you say
# is not answered after a two-minute silence.
Write-Host '   fetching the speech-recognition model (once, ~500 MB)...'
& $python (Join-Path $root 'warmup.py')
if ($LASTEXITCODE -ne 0) { Bad 'model download failed -- see above'; exit 1 }

Step 'Done'
Write-Host @"
   Talk to it:
     $root\bin\nova.cmd

   Or type instead:
     $root\bin\nova.cmd --text

   Say the wake name -- "Nova, what is on my screen?" -- or turn that
   off with "wake": {"required": false} in config.json.
"@ -ForegroundColor Gray
