@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" %*
if errorlevel 1 (
  echo EEVEE Bridge was not uninstalled. See the message above.
  pause
  exit /b 1
)
pause
