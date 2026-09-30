@echo off
setlocal
rem Remove every installed EEVEE Bridge version, its Houdini package registration, logs
rem and caches. Uses Houdini's bundled Python, or the Python launcher without Houdini.
set "PYTHON="
set "LAUNCHER="
if defined HFS if exist "%HFS%\python313\python.exe" set "PYTHON=%HFS%\python313\python.exe"
set "PROGRAMS=%ProgramW6432%"
if not defined PROGRAMS set "PROGRAMS=%ProgramFiles%"
if not defined PYTHON for /d %%H in ("%PROGRAMS%\Side Effects Software\Houdini 22.0.*") do if exist "%%H\python313\python.exe" set "PYTHON=%%H\python313\python.exe"
rem Houdini installed outside Program Files is registered with its folder.
if not defined PYTHON for /f "tokens=2,*" %%A in ('reg query "HKLM\SOFTWARE\Side Effects Software" /s /v InstallPath 2^>nul ^| findstr /I /C:"Houdini 22.0."') do if exist "%%B\python313\python.exe" set "PYTHON=%%B\python313\python.exe"
if not defined PYTHON if exist "%WINDIR%\py.exe" set "PYTHON=%WINDIR%\py.exe" & set "LAUNCHER=-3"
if not defined PYTHON (
  echo Neither Houdini 22's Python nor the Python launcher was found. Run install.py --uninstall-all with Python 3.10 or later.
  pause
  exit /b 1
)
"%PYTHON%" %LAUNCHER% -E "%~dp0install.py" --uninstall-all %*
if errorlevel 1 (
  echo EEVEE Bridge was not uninstalled. See the message above.
  pause
  exit /b 1
)
pause
