# Builds the Windows executables into pc\dist\LANTooth\, plus
#   dist\LANTooth-<version>-win64.zip       portable folder
#   dist\LANTooth-<version>-setup.exe       installer (with -Installer; needs Inno Setup 6)
# The version comes from the repo-root VERSION file.
#
#   powershell -ExecutionPolicy Bypass -File pc\build_exe.ps1 [-Installer]
#
# Needs: 64-bit Python 3.10+ ("python3.13"/"python3"/"python"/"py" on PATH) and a
# 64-bit opus.dll or libopus-0.dll in pc\ (the GitHub release workflow builds
# opus.dll from source; locally e.g. copy libopus-0.dll from VLC) - it is
# bundled next to the exe.
param([switch]$Installer)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$version = (Get-Content (Join-Path $PSScriptRoot "..\VERSION") -Raw).Trim()
Write-Host "Building LANTooth $version"

$py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    $base = @("python3.13", "python3", "python", "py") |
        Where-Object { Get-Command $_ -ErrorAction SilentlyContinue } | Select-Object -First 1
    if (-not $base) { throw "No Python interpreter found on PATH." }
    & $base -m venv .venv
}

& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -r requirements-lock.txt -r requirements-build.txt

& $py selftest.py
if ($LASTEXITCODE -ne 0) { throw "selftest.py failed - not building." }

& $py -m PyInstaller --noconfirm --clean lantooth.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

Get-ChildItem dist -Filter "LANTooth-*" -File -ErrorAction SilentlyContinue | Remove-Item
$zip = Join-Path $PSScriptRoot "dist\LANTooth-$version-win64.zip"
# .NET ZipFile rather than Compress-Archive, which fails with "access denied" when
# antivirus is still scanning the freshly written files.
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    (Join-Path $PSScriptRoot "dist\LANTooth"), $zip, [System.IO.Compression.CompressionLevel]::Optimal, $true)
Write-Host "Built dist\LANTooth\ and $zip"

if ($Installer) {
    $iscc = @(
        (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source,
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $iscc) { throw "Inno Setup 6 (ISCC.exe) not found - install it or build without -Installer." }
    & $iscc /Qp "/DAppVersion=$version" installer.iss
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed." }
    Write-Host "Built dist\LANTooth-$version-setup.exe"
}
