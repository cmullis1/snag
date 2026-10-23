<#
.SYNOPSIS
    Sets Snag up on this PC: its Python environment, its shortcuts, and starts it.

.DESCRIPTION
    Run once from the Snag folder (running it again is safe: every step checks or repeats cleanly):
        powershell -ExecutionPolicy Bypass -File setup.ps1

    1. Finds Python 3.12 or newer (python on PATH, else the py launcher; or the one given with -Python).
       The Microsoft Store's Python is refused: it keeps what it writes to %LOCALAPPDATA% in a
       private folder of its own, so Snag's data wouldn't be where Snag looks for it.
    2. Stops if Snag is running (it answers on its port, or holds its single-instance mutex): pip
       can't replace files a running Snag has open. Quit it from the tray icon first.
    3. Creates the virtual environment .venv in this folder, unless it is already there.
    4. Installs Snag and its dependencies into it (pip install -e .).
    5. Makes the shortcuts, through Snag's own helper (python -m snag.platform.startup), which
       first says whether Snag's Start menu shortcut is there. Only this script makes it, so:
       - the first install (no Start menu shortcut yet) makes the Start menu shortcut (opens
         Snag's tab) and the Startup shortcut (Snag starts with Windows, quietly in the tray),
         even if Snag has already run here and made its database;
       - run again, the Start menu shortcut again, and the Startup one only if it is there, so
         Start with Windows stays as it was set in Snag's Settings.
    6. Starts Snag and opens its tab.
    7. Prints how to add the browser extension.

    On first start Snag downloads yt-dlp, ffmpeg and Deno into %LOCALAPPDATA%\Snag\bin.

    Started from Explorer (Run with PowerShell), its window would close as soon as it ends, so it
    waits for Enter first.

    Snag's tests set SNAG_TEST_HOOK=1, and then SNAG_PORT is the port to look for a running Snag
    on, SNAG_SETUP_PARENT stands in for the name of the process that started this script, and
    SNAG_START_MENU (read by the helper) for the user's Start menu folder.

.PARAMETER DryRun
    Only check Python and print what would be done; change nothing.

.PARAMETER Python
    The Python to create the virtual environment with, instead of looking for one.
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [string]$Python
)

$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$venv = Join-Path $repo '.venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'
$venvPythonw = Join-Path $venv 'Scripts\pythonw.exe'
$minimum = [version]'3.12'
$testHook = $env:SNAG_TEST_HOOK -eq '1'
$port = 47823
if ($testHook -and $env:SNAG_PORT) { $port = [int]$env:SNAG_PORT }

# The name of the process that started this script ('explorer' for Run with PowerShell), or $null.
function Get-ParentName {
    if ($testHook -and $env:SNAG_SETUP_PARENT) { return $env:SNAG_SETUP_PARENT }
    try {
        $parentId = (Get-CimInstance Win32_Process -Filter "ProcessId=$PID").ParentProcessId
        return (Get-Process -Id $parentId).ProcessName
    } catch {
        return $null
    }
}

# Started from Explorer, this window closes when the script ends: wait for Enter, so what happened
# can be read. Not with -NonInteractive, which promises nobody is there to press it.
function Wait-BeforeClosing {
    $nonInteractive = @([Environment]::GetCommandLineArgs() | Where-Object { $_ -match '^-noni' }).Count -gt 0
    if ($nonInteractive -or (Get-ParentName) -ne 'explorer') { return }
    Write-Host ''
    Write-Host 'Press Enter to close this window.'
    [void][Console]::ReadLine()
}

function Exit-Setup([int]$code) {
    Wait-BeforeClosing
    exit $code
}

function Fail([string]$message) {
    Write-Host ''
    Write-Host $message -ForegroundColor Red
    Exit-Setup 1
}

$storePython = ("This Python is the Microsoft Store's, which keeps what it writes to %LOCALAPPDATA% in a " +
                "private folder of its own, so Snag's data and tools wouldn't be where Snag looks for them. " +
                "Install Python from https://www.python.org/downloads/ (tick 'Add python.exe to PATH')")

# What a Python command reports: @{ Version; Base } (Base: the Python it really is, sys._base_executable),
# or $null when it doesn't run. A missing Python's Store stub prints to stderr and exits 9009: $null.
function Get-PythonInfo([string[]]$command) {
    $exe = $command[0]
    $rest = @($command | Select-Object -Skip 1)
    try {
        $found = @(& $exe @rest -c "import sys; print('{0}.{1}.{2}'.format(*sys.version_info)); print(getattr(sys, '_base_executable', sys.executable))" 2>$null)
    } catch {
        return $null
    }
    if ($LASTEXITCODE -ne 0 -or $found.Count -eq 0) { return $null }
    try { $version = [version]"$($found[0])".Trim() } catch { return $null }
    $base = ''
    if ($found.Count -gt 1) { $base = "$($found[1])".Trim() }
    return @{ Version = $version; Base = $base }
}

function Test-StorePath([string]$path) {
    return $path -match '\\WindowsApps\\'
}

function Find-Python {
    if ($Python) {
        $candidates = @(, @($Python))
    } else {
        $candidates = @()
        if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += , @('python') }
        if (Get-Command py -ErrorAction SilentlyContinue) { $candidates += , @('py', '-3') }
    }
    $seen = @()
    $store = $false
    foreach ($command in $candidates) {
        $info = Get-PythonInfo $command
        if ($null -eq $info) { continue }
        if (Test-StorePath $info.Base) { $store = $true; continue }
        if ($info.Version -ge $minimum) {
            return @{ Command = $command; Version = $info.Version }
        }
        $seen += "$($info.Version)"
    }
    if ($store) { Fail "$storePython, then run setup.ps1 again." }
    $also = ''
    if ($seen.Count) { $also = " (found Python $($seen -join ', '), which is too old)" }
    Fail ("Snag needs Python 3.12 or newer$also. Install it from https://www.python.org/downloads/ " +
          "(tick 'Add python.exe to PATH'), then run setup.ps1 again.")
}

# True when Snag runs: it holds its single-instance mutex (from the moment it starts), or answers
# Snag's /api/status on its port. Another program on the port doesn't count.
function Test-SnagRunning {
    $mutex = $null
    try {
        if ([System.Threading.Mutex]::TryOpenExisting("Local\Snag-$port", [ref]$mutex)) {
            $mutex.Dispose()
            return $true
        }
    } catch [System.UnauthorizedAccessException] {
        return $true
    }
    try {
        $request = [System.Net.WebRequest]::Create("http://127.0.0.1:$port/api/status")
        $request.Proxy = $null
        $request.Timeout = 3000
        $response = $request.GetResponse()
        try {
            $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
            $body = $reader.ReadToEnd()
        } finally {
            $response.Close()
        }
        $status = $body | ConvertFrom-Json
        return ($null -ne $status.version)
    } catch {
        return $false
    }
}

# Runs a step, or under -DryRun only says what it would run. $shown is the command as printed.
function Step([string]$what, [string]$shown, [scriptblock]$action) {
    if ($DryRun) {
        Write-Host "[dry run] ${what}: $shown"
        return
    }
    Write-Host "${what}..."
    & $action
    if ($LASTEXITCODE) { Fail "That didn't work (exit code $LASTEXITCODE), so setup stopped. The messages above say why." }
}

function Quote([string]$path) { return '"' + $path + '"' }

if ($DryRun) { Write-Host 'Snag setup (dry run: nothing will be changed)' } else { Write-Host 'Snag setup' }

$found = Find-Python
$pythonCommand = $found.Command
$pythonExe = $pythonCommand[0]
$pythonArgs = @($pythonCommand | Select-Object -Skip 1)
Write-Host "Python $($found.Version): $($pythonCommand -join ' ')"

if (Test-SnagRunning) { Fail 'Snag is running: quit it from the tray icon, then run setup.ps1 again.' }

if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
    # The Python it was made with, from its pyvenv.cfg ("home = C:\Python312", "version = 3.12.10").
    $made = $null
    $madeFrom = ''
    $cfg = Join-Path $venv 'pyvenv.cfg'
    if (Test-Path -LiteralPath $cfg -PathType Leaf) {
        $lines = @(Get-Content -LiteralPath $cfg)
        $line = $lines | Where-Object { $_ -match '^\s*version(_info)?\s*=\s*([0-9.]+)' } | Select-Object -First 1
        if ($line -and $line -match '=\s*([0-9.]+)') { try { $made = [version]$Matches[1] } catch { $made = $null } }
        $line = $lines | Where-Object { $_ -match '^\s*home\s*=' } | Select-Object -First 1
        if ($line -and $line -match '=\s*(.*)$') { $madeFrom = $Matches[1].Trim() }
    }
    if (Test-StorePath $madeFrom) {
        Fail ("The virtual environment in $venv was made with the Microsoft Store's Python, which keeps what " +
              "it writes to %LOCALAPPDATA% in a private folder of its own. Install Python from " +
              "https://www.python.org/downloads/. Delete the .venv folder and run setup.ps1 again.")
    }
    if ($null -eq $made -or $made -lt $minimum) {
        $which = 'an unknown Python'
        if ($null -ne $made) { $which = "Python $made" }
        Fail ("The virtual environment in $venv was made with $which, and Snag needs 3.12 or newer. " +
              'Delete the .venv folder and run setup.ps1 again.')
    }
    Write-Host "Using the existing virtual environment $venv"
} else {
    $shown = (@('&', (Quote $pythonExe)) + $pythonArgs + @('-m', 'venv', (Quote $venv))) -join ' '
    Step 'Create the virtual environment' $shown { & $pythonExe @pythonArgs -m venv $venv }
}

Step 'Install Snag and its dependencies' "& $(Quote $venvPython) -m pip install --disable-pip-version-check -e $(Quote $repo)" {
    & $venvPython -m pip install --disable-pip-version-check -e $repo
}

# Whether Snag's Start menu shortcut is there, from Snag's helper (it knows where the shortcuts live).
# Run with the Python found above, from this folder, so it works before .venv exists (a dry run);
# -B writes no bytecode, so a dry run changes nothing. The helper prints one line of JSON.
function Test-StartMenuShortcut {
    Push-Location -LiteralPath $repo
    try {
        $said = @(& $pythonExe @pythonArgs -B -m snag.platform.startup status)
    } finally {
        Pop-Location
    }
    if ($LASTEXITCODE -ne 0) {
        Fail "Couldn't check for Snag's Start menu shortcut (exit code $LASTEXITCODE), so setup stopped. The messages above say why."
    }
    try {
        $status = ($said -join "`n") | ConvertFrom-Json
    } catch {
        Fail "Couldn't check for Snag's Start menu shortcut: its helper said '$($said -join ' ')', so setup stopped."
    }
    return [bool]$status.start_menu.exists
}

# The first install (only this script makes the Start menu shortcut) gets both shortcuts, even if
# Snag has already run here. Run again, Start with Windows stays as Snag's Settings left it.
if (Test-StartMenuShortcut) {
    $shortcuts = 'refresh'
    $what = 'Make the Start menu shortcut again (and the Startup one, if Start with Windows is on)'
} else {
    $shortcuts = 'create'
    $what = 'Make the Start menu and Startup shortcuts'
}
Step $what "& $(Quote $venvPython) -m snag.platform.startup $shortcuts" {
    Push-Location -LiteralPath $repo
    try { & $venvPython -m snag.platform.startup $shortcuts } finally { Pop-Location }
}

Step 'Start Snag' "Start-Process $(Quote $venvPythonw) -m snag --open" {
    Start-Process -FilePath $venvPythonw -ArgumentList '-m', 'snag', '--open' -WorkingDirectory $repo
    $global:LASTEXITCODE = 0
}

$extension = Join-Path $repo 'extension'
Write-Host ''
Write-Host 'Last step: add the Snag extension to your browser (once per browser).'
Write-Host '  1. Open chrome://extensions in Chrome, or edge://extensions in Edge.'
Write-Host '  2. Turn on Developer mode.'
Write-Host '  3. Click Load unpacked and choose this folder:'
Write-Host "       $extension"
Write-Host ''
if (-not $DryRun) {
    Write-Host 'Snag is running: look for its icon in the notification area, next to the clock.'
    Write-Host 'On first start it downloads yt-dlp, ffmpeg and Deno; its tab shows the progress.'
}
Exit-Setup 0
