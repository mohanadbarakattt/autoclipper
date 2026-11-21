# AutoClipper one-line installer for Windows.
#   irm https://raw.githubusercontent.com/mohanadbarakattt/autoclipper/main/install.ps1 | iex
# Installs Python + FFmpeg if missing (winget), downloads AutoClipper to %USERPROFILE%\AutoClipper,
# adds a Desktop shortcut and starts it. Re-run any time to update - your keys, streamers and clips are kept.
$ErrorActionPreference = 'Stop'
$Repo = 'mohanadbarakattt/autoclipper'
$Dest = Join-Path $env:USERPROFILE 'AutoClipper'

function Say($m) { Write-Host "`n== $m" -ForegroundColor Cyan }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}
function Need($cmd, $id, $name) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) { return }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "$name is missing and winget isn't available. Install $name manually, then run this again."
    }
    Say "Installing $name (approve the Windows prompt if one appears)..."
    winget install --id $id -e --silent --accept-source-agreements --accept-package-agreements | Out-Null
    Refresh-Path
}

Say 'AutoClipper installer'
Need 'python' 'Python.Python.3.11' 'Python 3.11'
Need 'ffmpeg' 'Gyan.FFmpeg' 'FFmpeg'

Say "Downloading AutoClipper to $Dest"
$tmp = Join-Path $env:TEMP ('autoclipper_' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $tmp | Out-Null
$zip = Join-Path $tmp 'src.zip'
Invoke-WebRequest "https://github.com/$Repo/archive/refs/heads/main.zip" -OutFile $zip -UseBasicParsing
Expand-Archive $zip -DestinationPath $tmp
$src = Get-ChildItem $tmp -Directory | Select-Object -First 1
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
# copy code; never overwrite your own profiles (new presets are still added)
Get-ChildItem $src.FullName -Recurse -File | ForEach-Object {
    $rel = $_.FullName.Substring($src.FullName.Length + 1)
    $target = Join-Path $Dest $rel
    if ($rel -like 'config\profiles\*' -and (Test-Path $target)) { return }
    New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
    Copy-Item $_.FullName $target -Force
}
Remove-Item $tmp -Recurse -Force

Say 'Adding a Desktop shortcut'
$lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'AutoClipper.lnk'
$sh = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$sh.TargetPath = Join-Path $Dest 'start.bat'
$sh.WorkingDirectory = $Dest
$sh.Description = 'AutoClipper - stream clips on autopilot'
$sh.Save()

Say 'Starting AutoClipper (first run installs the AI models, ~10 minutes, once)'
Start-Process -FilePath (Join-Path $Dest 'start.bat') -WorkingDirectory $Dest
Write-Host "`nDone. Next time just double-click 'AutoClipper' on your Desktop." -ForegroundColor Green
