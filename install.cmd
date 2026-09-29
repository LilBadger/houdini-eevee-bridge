@echo off
setlocal
rem Run install.py with Houdini's bundled Python. No PowerShell and no separate Python
rem are needed; endpoint security tools often block installers that start PowerShell.
set "PYTHON="
set "LAUNCHER="
set "NEXT="
for %%A in (%*) do (
  if defined NEXT if exist "%%~A\python313\python.exe" set "PYTHON=%%~A\python313\python.exe"
  set "NEXT="
  if /I "%%~A"=="--houdini" set "NEXT=1"
)
if not defined PYTHON if defined HFS if exist "%HFS%\python313\python.exe" set "PYTHON=%HFS%\python313\python.exe"
set "PROGRAMS=%ProgramW6432%"
if not defined PROGRAMS set "PROGRAMS=%ProgramFiles%"
if not defined PYTHON for /d %%H in ("%PROGRAMS%\Side Effects Software\Houdini 22.0.*") do if exist "%%H\python313\python.exe" set "PYTHON=%%H\python313\python.exe"
if not defined PYTHON if exist "%WINDIR%\py.exe" set "PYTHON=%WINDIR%\py.exe" & set "LAUNCHER=-3"
if not defined PYTHON (
  echo Houdini 22 bundled Python was not found. Run install.cmd --houdini "C:\path\to\Houdini 22.0.xxx", or run install.py with Python 3.10 or later.
  pause
  exit /b 1
)
"%PYTHON%" %LAUNCHER% -E "%~dp0install.py" %*
if errorlevel 1 (
  echo EEVEE installation failed. See the message above.
  pause
  exit /b 1
)
echo EEVEE installation completed.
pause
