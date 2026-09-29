# Use Houdini's bundled Python; a separate system Python is not required.
$ErrorActionPreference = 'Stop'
$installerArgs = $args
$roots = @()
for ($i = 0; $i -lt $installerArgs.Count - 1; $i++) {
    if ($installerArgs[$i] -eq '--houdini') { $roots += $installerArgs[$i + 1] }
}
if ($env:HFS) { $roots += $env:HFS }
$roots += @(Get-ChildItem "$env:ProgramFiles\Side Effects Software\Houdini 22.0.*" -Directory -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
$python = $null
foreach ($root in $roots) {
    foreach ($pattern in @('python*\python.exe','python\bin\python.exe')) {
        $candidate = Get-ChildItem (Join-Path $root $pattern) -File -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($candidate) { $python = $candidate.FullName; break }
    }
    if ($python) { break }
}
$prefix = @()
if (-not $python) {
    # Uninstalling needs no Houdini; any Python 3.10+ runs install.py.
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) { $python = $launcher.Source; $prefix = @('-3') }
    else { $python = (Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source }
}
if (-not $python) {
    Write-Error 'Houdini 22 bundled Python was not found. Run install.cmd --houdini "C:\path\to\Houdini 22.0.xxx" or run install.py using Python 3.10+.'
    exit 1
}
& $python @prefix (Join-Path $PSScriptRoot 'install.py') @installerArgs
exit $LASTEXITCODE
