# Remove every installed EEVEE Bridge version, its Houdini package registration, logs and caches.
& (Join-Path $PSScriptRoot 'install.ps1') --uninstall-all @args
exit $LASTEXITCODE
