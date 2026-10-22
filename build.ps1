param([string]$DistPath = 'dist')
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    py -3.13 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.13 is required.' }
}
& .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& .\.venv\Scripts\python.exe -m pytest -q
if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }
& .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --windowed --onedir --distpath $DistPath --name SSH-Jumper --collect-submodules paramiko main.py
if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
$bundlePath = Join-Path $DistPath 'SSH-Jumper'
& .\.venv\Scripts\python.exe collect_licenses.py $bundlePath
if ($LASTEXITCODE -ne 0) { throw 'License collection failed.' }
& .\.venv\Scripts\python.exe finalize_build.py $bundlePath
if ($LASTEXITCODE -ne 0) { throw 'Packaged application verification failed.' }
