# Clipper setup: installs what Clipper needs, downloads it, and puts a shortcut on the desktop.
# Run it by double-clicking "Install Clipper.bat". Safe to run again: it skips what's already there
# and updates an existing copy.
$ErrorActionPreference = 'Stop'
$Repo = 'https://github.com/anasisanas123-ux/clipper.git'

function Say($text) { Write-Host "`n== $text" -ForegroundColor Green }
function Has($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}
function Need($id, $name, $test) {
    if (& $test) { Write-Host "$name is already installed."; return }
    if (-not (Has 'winget')) { throw "winget is missing. Install 'App Installer' from the Microsoft Store, then run this again." }
    Say "Installing $name"
    winget install -e --id $id --accept-package-agreements --accept-source-agreements --silent
    Refresh-Path
    if (-not (& $test)) { throw "$name didn't install. Install it yourself, then run this again." }
}

Need 'Git.Git' 'Git' { Has 'git' }
Need 'Python.Python.3.11' 'Python 3.11' { (Has 'py') -and ((& py -3.11 --version 2>$null) -match '3\.11') }
Need 'OpenJS.NodeJS.LTS' 'Node.js' { Has 'npm' }

# Use the folder this script is in when it's already a Clipper copy, otherwise %USERPROFILE%\Clipper.
$Dir = $PSScriptRoot
if (-not (Test-Path (Join-Path $Dir 'backend\server.py'))) {
    $Dir = Join-Path $env:USERPROFILE 'Clipper'
    if (Test-Path (Join-Path $Dir '.git')) {
        Say 'Updating the existing copy'
        git -C $Dir pull --ff-only
    } else {
        Say "Downloading Clipper to $Dir"
        git clone $Repo $Dir
    }
}
Set-Location $Dir

if (-not (Test-Path '.venv\Scripts\python.exe')) {
    Say 'Creating the Python environment'
    py -3.11 -m venv .venv
}
Say 'Installing Python packages (a few minutes the first time)'
& .venv\Scripts\python.exe -m pip install --upgrade pip -q
& .venv\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Installing the Python packages failed (see above).' }

Say 'Installing the app window (Electron)'
npm install --no-audit --no-fund
if ($LASTEXITCODE -ne 0) { throw 'npm install failed (see above).' }

Say 'Adding a desktop shortcut'
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'Clipper.lnk'))
$lnk.TargetPath = "$env:WINDIR\System32\wscript.exe"
$lnk.Arguments = '"' + (Join-Path $Dir 'Start Clipper.vbs') + '"'
$lnk.WorkingDirectory = $Dir
$lnk.IconLocation = (Join-Path $Dir 'node_modules\electron\dist\electron.exe') + ',0'
$lnk.Save()

Say 'Done. Open Clipper from the desktop shortcut. Updates: Settings > Check for updates.'
