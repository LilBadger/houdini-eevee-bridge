; Windows setup for the EEVEE Bridge, built by tools/package_release.py --setup with NSIS 3.
; It installs per user without administrator rights and runs install.ps1, which finds
; Houdini and Blender, uses or builds the plugin, renders a test image and then removes
; older versions. Settings > Apps uninstalls every version.
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
; The setup program is 32-bit; Sysnative reaches 64-bit PowerShell, which sees the
; real Program Files where Houdini is installed.
!define POWERSHELL "$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe"

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"

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

; Installing and uninstalling replace files that a running Houdini keeps open.
!macro CLOSE_HOUDINI UN
Function ${UN}CloseHoudini
  retry:
    nsExec::Exec '"${POWERSHELL}" -NoProfile -Command "if (Get-Process -Name houdini*,happrentice,hython,husk,hbatch -ErrorAction SilentlyContinue) { exit 3 }"'
    Pop $0
    ${If} $0 == 3
      MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "Please close Houdini (and any hython or husk renders), then click Retry." /SD IDCANCEL IDRETRY retry
      Abort
    ${EndIf}
FunctionEnd
!macroend
!insertmacro CLOSE_HOUDINI ""
!insertmacro CLOSE_HOUDINI "un."

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "The EEVEE Bridge needs 64-bit Windows." /SD IDOK
    Abort
  ${EndIf}
  Call CloseHoudini
FunctionEnd

Function un.onInit
  Call un.CloseHoudini
FunctionEnd

Section "Install"
  ; Unpack beside the current payload, so a failed install leaves the previous one intact.
  RMDir /r "$INSTDIR\incoming"
  SetOutPath "$INSTDIR\incoming"
  File /r "${PAYLOAD}\*.*"
  SetOutPath "$INSTDIR"
  DetailPrint "Installing for your Houdini. If the plugin has to be compiled for your Houdini build, this takes a few minutes."
  nsExec::ExecToLog '"${POWERSHELL}" -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\incoming\install.ps1" --reinstall --remove-old'
  Pop $0
  ${If} $0 != 0
    RMDir /r "$INSTDIR\incoming"
    RMDir "$INSTDIR"
    DetailPrint "EEVEE Bridge was not installed, and your Houdini setup was not changed. The messages above say why."
    MessageBox MB_ICONSTOP "EEVEE Bridge could not be installed. The messages in the setup window say why." /SD IDOK
    Abort
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
  nsExec::ExecToLog '"${POWERSHELL}" -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\payload\uninstall.ps1" --yes'
  Pop $0
  ${If} $0 != 0
    MessageBox MB_ICONEXCLAMATION "Some EEVEE Bridge files could not be removed. The messages in the setup window say which." /SD IDOK
  ${EndIf}
  SetOutPath "$TEMP"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  RMDir "$LOCALAPPDATA\HoudiniEEVEE"
SectionEnd
