; Windows setup for the EEVEE Bridge, built by tools/package_release.py --setup with NSIS 3.
; It installs per user without administrator rights and runs install.py with Houdini's
; bundled Python, which finds Houdini and Blender, uses or builds the plugin, renders a
; test image and then removes older versions. Settings > Apps uninstalls every version.
; It starts no PowerShell: endpoint security tools block installers that run scripts.
; makensis /DVERSION=x.y.z /DPAYLOAD=<extracted Windows package> /DOUTFILE=<setup.exe> windows_setup.nsi

!ifndef VERSION
  !error "Pass /DVERSION=x.y.z"
!endif
!ifndef PAYLOAD
  !error "Pass /DPAYLOAD=<extracted houdini-eevee-x.y.z-windows-x86_64 folder>"
!endif
!ifndef OUTFILE
  !define OUTFILE "houdini-eevee-${VERSION}-windows-x86_64-setup.exe"
!endif

Unicode true
ManifestDPIAware true
RequestExecutionLevel user
SetCompressor /SOLID lzma
Name "EEVEE Bridge for Houdini ${VERSION}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\HoudiniEEVEE\setup"
ShowInstDetails show
ShowUninstDetails show
BrandingText "EEVEE Bridge for Houdini ${VERSION}"

!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\HoudiniEEVEEBridge"

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"

Var PYTHON

; Why a setup stopped, also for silent (/S) installs, in %TEMP%\houdini-eevee-setup.log.
!macro STOP MESSAGE
  FileOpen $9 "$TEMP\houdini-eevee-setup.log" a
  FileSeek $9 0 END
  FileWrite $9 "${MESSAGE}$\r$\n"
  FileClose $9
  MessageBox MB_ICONSTOP "${MESSAGE}" /SD IDOK
  Abort
!macroend

!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_TITLE "EEVEE Bridge is installed"
!define MUI_FINISHPAGE_TEXT "Start Houdini, open a Solaris network, and choose EEVEE Bridge from the viewport's renderer menu.$\r$\n$\r$\nOlder EEVEE Bridge versions were removed. Settings > Apps uninstalls it."
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${PAYLOAD}\LICENSE"
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

!macro SHARED UN
; Installing and uninstalling replace files that a running Houdini keeps open.
Function ${UN}CloseHoudini
  retry:
    ; Exact program names: this setup's own name also starts with "houdini".
    nsExec::Exec 'cmd.exe /c tasklist /NH /FO CSV | findstr /I /R /C:"^.houdini[a-z]*\.exe" /C:"^.hython\.exe" /C:"^.husk\.exe" /C:"^.hbatch\.exe" /C:"^.happrentice\.exe" /C:"^.hindie\.exe"'
    Pop $0
    ${If} $0 == 0
      MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "Please close Houdini (and any hython or husk renders), then click Retry." /SD IDCANCEL IDRETRY retry
      !insertmacro STOP "Houdini is running (tasklist check exit $0). Close it and run the setup again."
    ${EndIf}
FunctionEnd

; Houdini's bundled Python runs install.py; any Houdini 22.0 build's will do, since the
; installer picks the Houdini build itself. The Python launcher lets the uninstaller
; work after Houdini was removed.
Function ${UN}FindPython
  StrCpy $PYTHON ""
  ; Every Houdini build is registered with its folder, also outside Program Files.
  SetRegView 64
  StrCpy $3 0
  registry:
    EnumRegKey $2 HKLM "SOFTWARE\Side Effects Software" $3
    StrCmp $2 "" registry_done
    IntOp $3 $3 + 1
    StrCpy $4 $2 13
    StrCmp $4 "Houdini 22.0." 0 registry
    ReadRegStr $5 HKLM "SOFTWARE\Side Effects Software\$2" "InstallPath"
    IfFileExists "$5\python313\python.exe" 0 registry
      StrCpy $PYTHON "$5\python313\python.exe"
    Goto registry
  registry_done:
  SetRegView default
  ReadEnvStr $5 HFS
  ${If} $PYTHON == ""
  ${AndIf} ${FileExists} "$5\python313\python.exe"
    StrCpy $PYTHON "$5\python313\python.exe"
  ${EndIf}
  ${If} $PYTHON != ""
    Goto found
  ${EndIf}
  FindFirst $1 $2 "$PROGRAMFILES64\Side Effects Software\Houdini 22.0.*"
  loop:
    StrCmp $2 "" done
    IfFileExists "$PROGRAMFILES64\Side Effects Software\$2\python313\python.exe" 0 +2
      StrCpy $PYTHON "$PROGRAMFILES64\Side Effects Software\$2\python313\python.exe"
    FindNext $1 $2
    Goto loop
  done:
  FindClose $1
  found:
  ${If} $PYTHON == ""
  ${AndIf} ${FileExists} "$WINDIR\py.exe"
    StrCpy $PYTHON "$WINDIR\py.exe"
  ${EndIf}
  ; This setup is 32-bit; the installer must see the real Program Files, where Houdini is.
  System::Call 'kernel32::SetEnvironmentVariable(t "ProgramFiles", t "$PROGRAMFILES64")'
FunctionEnd
!macroend
!insertmacro SHARED ""
!insertmacro SHARED "un."

Function .onInit
  Delete "$TEMP\houdini-eevee-setup.log"
  ${IfNot} ${RunningX64}
    !insertmacro STOP "The EEVEE Bridge needs 64-bit Windows."
  ${EndIf}
  Call CloseHoudini
  Call FindPython
  ${If} $PYTHON == ""
    !insertmacro STOP "Houdini 22.0 was not found. Install Houdini 22.0 first, or set HFS to its folder."
  ${EndIf}
FunctionEnd

Function un.onInit
  Call un.CloseHoudini
  Call un.FindPython
FunctionEnd

Section "Install"
  ; Unpack beside the current payload, so a failed install leaves the previous one intact.
  RMDir /r "$INSTDIR\incoming"
  SetOutPath "$INSTDIR\incoming"
  File /r "${PAYLOAD}\*.*"
  SetOutPath "$INSTDIR"
  DetailPrint "Installing for your Houdini. If the plugin has to be compiled for your Houdini build, this takes a few minutes."
  ; The installer also writes everything it prints to this file.
  System::Call 'kernel32::SetEnvironmentVariable(t "HDEEVEE_INSTALL_LOG", t "$TEMP\houdini-eevee-install.log")'
  nsExec::ExecToLog '"$PYTHON" -E "$INSTDIR\incoming\install.py" --reinstall --remove-old'
  Pop $0
  ${If} $0 != 0
    RMDir /r "$INSTDIR\incoming"
    RMDir "$INSTDIR"
    DetailPrint "EEVEE Bridge was not installed, and your Houdini setup was not changed. The messages above say why."
    !insertmacro STOP "EEVEE Bridge could not be installed (install.py exited with $0). Its messages are in the setup window and in $TEMP\houdini-eevee-install.log."
  ${EndIf}
  RMDir /r "$INSTDIR\payload"
  Rename "$INSTDIR\incoming" "$INSTDIR\payload"
  WriteUninstaller "$INSTDIR\uninstall.exe"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "EEVEE Bridge for Houdini"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "badgerz42"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "URLInfoAbout" "https://github.com/badgerz42/houdini-eevee-bridge"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  DetailPrint "Removing every EEVEE Bridge version, its Houdini registration, logs and caches."
  ${If} $PYTHON == ""
    MessageBox MB_ICONEXCLAMATION "Neither Houdini 22.0 nor Python was found, so the installed EEVEE Bridge versions could not be removed. Only the setup's own files are removed." /SD IDOK
  ${Else}
    nsExec::ExecToLog '"$PYTHON" -E "$INSTDIR\payload\install.py" --uninstall-all --yes'
    Pop $0
    ${If} $0 != 0
      MessageBox MB_ICONEXCLAMATION "Some EEVEE Bridge files could not be removed. The messages in the setup window say which." /SD IDOK
    ${EndIf}
  ${EndIf}
  SetOutPath "$TEMP"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  RMDir "$LOCALAPPDATA\HoudiniEEVEE"
SectionEnd
