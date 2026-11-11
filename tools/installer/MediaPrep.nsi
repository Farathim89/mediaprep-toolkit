; MediaPrep Toolkit - Windows installer (NSIS 3, Modern UI 2).
; Built by tools/build_exe.py --installer, which passes:
;   /DVERSION=2.0.1  /DVERSION4=2.0.1.0  /DSRCDIR=<folder build>
;   /DOUTFILE=<Setup exe>  /DROOT=<project folder>
;
; Installs for ALL users into "C:\Program Files\MediaPrep Toolkit" (folder
; changeable). The app keeps working like the folder build: Media\ and Data\
; sit next to the exe. Program Files isn't writable for normal users, so the
; installer creates those two folders and grants BUILTIN\Users (S-1-5-32-545)
; Modify on them only - the program files stay protected.
;
; Upgrade in place: an existing install (registry InstallLocation) is used as
; the folder; a running MediaPrep from that folder is closed first (asked in
; the UI; /S waits up to ~20 s, then asks it to close); _internal\ is removed
; before the new files are copied. Media\ and Data\ are never touched.
;
; Command line:
;   /S              silent install (the in-app updater uses /S /RUN /D=<dir>)
;   /D=<dir>        install folder (must be last, no quotes)
;   /RUN            start the app when a silent install has finished
;   /NODESKTOP      no desktop shortcut      /NOSTARTMENU  no Start menu shortcut
;   (a silent upgrade keeps the shortcut choices of the previous install)
; Uninstaller: Uninstall.exe [/S] - asks whether to delete Media\ and Data\
; too (default No; /S keeps them).
;
; Run without admin rights (only possible by forcing it, e.g. for tests with
; __COMPAT_LAYER=RunAsInvoker) it installs for the current user: default
; folder %LOCALAPPDATA%\Programs\MediaPrep Toolkit, HKCU uninstall entry, the
; user's own shortcuts.

Unicode true
SetCompressor /SOLID lzma
SetCompressorDictSize 32

!ifndef VERSION
  !error "VERSION not defined (build with tools/build_exe.py --installer)"
!endif

!define APPNAME "MediaPrep Toolkit"
!define APPEXE "MediaPrep Toolkit.exe"
!define COMPANY "Farathim"
!define URL "https://github.com/Farathim89/mediaprep-toolkit"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}"
!define UNINSTALLER "Uninstall.exe"
!define USERS_SID "*S-1-5-32-545"

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "x64.nsh"
!include "Sections.nsh"

Name "${APPNAME}"
OutFile "${OUTFILE}"
InstallDir "$PROGRAMFILES64\${APPNAME}"
RequestExecutionLevel admin
BrandingText "${APPNAME} ${VERSION}"
ShowInstDetails show
ShowUninstDetails show
ManifestDPIAware true

VIProductVersion "${VERSION4}"
VIAddVersionKey "ProductName" "${APPNAME}"
VIAddVersionKey "CompanyName" "${COMPANY}"
VIAddVersionKey "LegalCopyright" "© 2026 Farathim, GPL-3.0-or-later"
VIAddVersionKey "FileDescription" "${APPNAME} ${VERSION} Setup"
VIAddVersionKey "FileVersion" "${VERSION4}"
VIAddVersionKey "ProductVersion" "${VERSION}"

Var IsAdmin
Var Upgrade
Var RunAfter
Var UnAllUsers
Var GotD

; ---------------------------------------------------------------- pages
!define MUI_ICON "${ROOT}\app\assets\icon.ico"
!define MUI_UNICON "${ROOT}\app\assets\icon.ico"
!define MUI_ABORTWARNING
!define MUI_COMPONENTSPAGE_NODESC
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Run ${APPNAME}"
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchApp

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${ROOT}\LICENSE"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; ---------------------------------------------------------------- helpers
; Is "<dir>\MediaPrep Toolkit.exe" running? (a running exe can't be opened
; for writing). Pushes 1 / 0.
!macro _IsRunning UN
Function ${UN}IsRunning
  Exch $0
  Push $1
  StrCpy $1 0
  ${If} ${FileExists} "$0\${APPEXE}"
    ClearErrors
    FileOpen $0 "$0\${APPEXE}" a
    ${If} ${Errors}
      StrCpy $1 1
    ${Else}
      FileClose $0
    ${EndIf}
  ${EndIf}
  StrCpy $0 $1
  Pop $1
  Exch $0
FunctionEnd

; ask the app(s) running from $INSTDIR to close: taskkill WITHOUT /F posts
; WM_CLOSE to their windows -> the app saves its settings like with its X
; button (only processes started from this folder)
Function ${UN}AskAppToClose
  ; the 64-bit PowerShell (Sysnative from this 32-bit installer): a 32-bit
  ; one can't read the exe path of the 64-bit app
  StrCpy $1 "$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
  ${IfNot} ${FileExists} $1
    StrCpy $1 "$SYSDIR\WindowsPowerShell\v1.0\powershell.exe"
  ${EndIf}
  nsExec::Exec `"$1" -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "Get-Process -Name 'MediaPrep Toolkit' -ErrorAction SilentlyContinue | Where-Object { $$_.Path -eq '$INSTDIR\${APPEXE}' } | ForEach-Object { & '$SYSDIR\taskkill.exe' /PID $$_.Id }"`
  Pop $0
FunctionEnd

; wait up to $R9 x 0.5 s for the app to exit. Pushes 1 (still running) / 0.
Function ${UN}WaitForExit
  Push $R8
  StrCpy $R8 0
  ${Do}
    Push $INSTDIR
    Call ${UN}IsRunning
    Pop $0
    ${If} $0 == 0
      ${Break}
    ${EndIf}
    IntOp $R8 $R8 + 1
    ${If} $R8 >= $R9
      ${Break}
    ${EndIf}
    Sleep 500
  ${Loop}
  Pop $R8
  Push $0
FunctionEnd

; make sure MediaPrep (from $INSTDIR) is not running
Function ${UN}CloseRunningApp
  Push $INSTDIR
  Call ${UN}IsRunning
  Pop $0
  ${If} $0 == 0
    Return
  ${EndIf}
  ${If} ${Silent}
    ; the in-app updater closes the app right after starting Setup
    DetailPrint "Waiting for ${APPNAME} to close..."
    StrCpy $R9 40
    Call ${UN}WaitForExit
    Pop $0
    ${If} $0 == 1
      Call ${UN}AskAppToClose
      StrCpy $R9 30
      Call ${UN}WaitForExit
      Pop $0
    ${EndIf}
    ${If} $0 == 1
      ; never delete files under a running app
      MessageBox MB_OK|MB_ICONSTOP "${APPNAME} is still running - close it and try again." /SD IDOK
      Abort
    ${EndIf}
    Return
  ${EndIf}
  ${Do}
    MessageBox MB_OKCANCEL|MB_ICONEXCLAMATION "${APPNAME} is running.$\r$\n$\r$\nClick OK to close it (your settings are saved; a running job is stopped), or Cancel to stop." IDOK +2
    Abort
    Call ${UN}AskAppToClose
    StrCpy $R9 30
    Call ${UN}WaitForExit
    Pop $0
    ${If} $0 == 0
      ${Break}
    ${EndIf}
    MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "${APPNAME} is still running. Close it yourself, then click Retry." IDRETRY +2
    Abort
    Push $INSTDIR
    Call ${UN}IsRunning
    Pop $0
    ${If} $0 == 0
      ${Break}
    ${EndIf}
  ${Loop}
FunctionEnd
!macroend
!insertmacro _IsRunning ""
!insertmacro _IsRunning "un."

; start the app as the normal user: an elevated installer hands the start to
; Explorer (which runs un-elevated); otherwise start it directly
Function LaunchApp
  ${If} $IsAdmin == 1
    Exec `"$WINDIR\explorer.exe" "$INSTDIR\${APPEXE}"`
  ${Else}
    Exec `"$INSTDIR\${APPEXE}"`
  ${EndIf}
FunctionEnd

Function CheckAdmin
  StrCpy $IsAdmin 0
  UserInfo::GetAccountType
  Pop $0
  ${If} $0 == "Admin"
    StrCpy $IsAdmin 1
  ${EndIf}
FunctionEnd

; ---------------------------------------------------------------- sections
Section "${APPNAME} (required)" SecMain
  SectionIn RO
  Call CloseRunningApp
  SetOutPath "$INSTDIR"
  ${If} ${FileExists} "$INSTDIR\${APPEXE}"
    ; upgrade: drop the old program files first so stale ones don't linger
    DetailPrint "Removing the old program files..."
    RMDir /r "$INSTDIR\_internal"
    ${If} ${FileExists} "$INSTDIR\_internal\*.*"
      MessageBox MB_OK|MB_ICONSTOP "Could not remove the old program files in$\r$\n$INSTDIR\_internal$\r$\n$\r$\nClose ${APPNAME} and run Setup again." /SD IDOK
      Abort
    ${EndIf}
  ${EndIf}
  ; the program files (folder build: exe, README, LICENSE, _internal\)
  File /r "${SRCDIR}\*.*"
  WriteUninstaller "$INSTDIR\${UNINSTALLER}"

  ; user data next to the app, writable for every user (these two only)
  CreateDirectory "$INSTDIR\Media"
  CreateDirectory "$INSTDIR\Data"
  DetailPrint "Letting all users write to Media\ and Data\..."
  nsExec::ExecToLog `"$SYSDIR\icacls.exe" "$INSTDIR\Media" /grant ${USERS_SID}:(OI)(CI)M /T /C /Q`
  Pop $0
  nsExec::ExecToLog `"$SYSDIR\icacls.exe" "$INSTDIR\Data" /grant ${USERS_SID}:(OI)(CI)M /T /C /Q`
  Pop $1
  ${If} $0 != 0
  ${OrIf} $1 != 0
    DetailPrint "WARNING: icacls returned $0 / $1"
  ${EndIf}

  ; Apps & features entry
  WriteRegStr SHCTX "${UNINST_KEY}" "DisplayName" "${APPNAME}"
  WriteRegStr SHCTX "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr SHCTX "${UNINST_KEY}" "Publisher" "${COMPANY}"
  WriteRegStr SHCTX "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\${APPEXE},0"
  WriteRegStr SHCTX "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr SHCTX "${UNINST_KEY}" "UninstallString" `"$INSTDIR\${UNINSTALLER}"`
  WriteRegStr SHCTX "${UNINST_KEY}" "QuietUninstallString" `"$INSTDIR\${UNINSTALLER}" /S`
  WriteRegStr SHCTX "${UNINST_KEY}" "URLInfoAbout" "${URL}"
  WriteRegStr SHCTX "${UNINST_KEY}" "HelpLink" "${URL}"
  WriteRegDWORD SHCTX "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD SHCTX "${UNINST_KEY}" "NoRepair" 1
  ; program files only (Media / Data grow with the user's videos)
  ${GetSize} "$INSTDIR\_internal" "/S=0K" $0 $1 $2
  IntOp $0 $0 + 1024
  WriteRegDWORD SHCTX "${UNINST_KEY}" "EstimatedSize" $0
  WriteRegDWORD SHCTX "${UNINST_KEY}" "ShortcutDesktop" 0
  WriteRegDWORD SHCTX "${UNINST_KEY}" "ShortcutStartMenu" 0
SectionEnd

Section "Desktop shortcut" SecDesktop
  CreateShortcut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\${APPEXE}" "" "$INSTDIR\${APPEXE}" 0
  WriteRegDWORD SHCTX "${UNINST_KEY}" "ShortcutDesktop" 1
SectionEnd

Section "Start menu shortcut" SecStartMenu
  CreateShortcut "$SMPROGRAMS\${APPNAME}.lnk" "$INSTDIR\${APPEXE}" "" "$INSTDIR\${APPEXE}" 0
  WriteRegDWORD SHCTX "${UNINST_KEY}" "ShortcutStartMenu" 1
SectionEnd

; an upgrade where a shortcut was switched off: remove the old one
Section "-shortcut cleanup"
  ${IfNot} ${SectionIsSelected} ${SecDesktop}
    Delete "$DESKTOP\${APPNAME}.lnk"
  ${EndIf}
  ${IfNot} ${SectionIsSelected} ${SecStartMenu}
    Delete "$SMPROGRAMS\${APPNAME}.lnk"
  ${EndIf}
SectionEnd

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_OK|MB_ICONSTOP "${APPNAME} needs 64-bit Windows." /SD IDOK
    Abort
  ${EndIf}
  SetRegView 64
  ; /D=<dir> given? (it replaces the InstallDir default before .onInit)
  StrCpy $GotD 1
  ${If} $INSTDIR == "$PROGRAMFILES64\${APPNAME}"
    StrCpy $GotD 0
  ${EndIf}
  Call CheckAdmin
  ${If} $IsAdmin == 1
    SetShellVarContext all
  ${Else}
    SetShellVarContext current
    ; no admin rights: Program Files would only be "virtualized" - use the
    ; user's own programs folder instead (unless /D= was given)
    ${If} $GotD == 0
      StrCpy $INSTDIR "$LOCALAPPDATA\Programs\${APPNAME}"
    ${EndIf}
  ${EndIf}

  ; existing install -> same folder (unless /D= was given)
  StrCpy $Upgrade 0
  ReadRegStr $0 SHCTX "${UNINST_KEY}" "InstallLocation"
  ${GetParameters} $R0
  ${If} $0 != ""
  ${AndIf} $GotD == 0
  ${AndIf} ${FileExists} "$0\${APPEXE}"
    StrCpy $INSTDIR $0
  ${EndIf}
  ${If} ${FileExists} "$INSTDIR\${APPEXE}"
    StrCpy $Upgrade 1
  ${EndIf}

  ; silent upgrade: keep the shortcut choices of the previous install
  ${If} ${Silent}
  ${AndIf} $Upgrade == 1
    ReadRegDWORD $0 SHCTX "${UNINST_KEY}" "ShortcutDesktop"
    ${If} $0 == "0"
      !insertmacro UnselectSection ${SecDesktop}
    ${EndIf}
    ReadRegDWORD $0 SHCTX "${UNINST_KEY}" "ShortcutStartMenu"
    ${If} $0 == "0"
      !insertmacro UnselectSection ${SecStartMenu}
    ${EndIf}
  ${EndIf}
  ClearErrors
  ${GetOptions} $R0 "/NODESKTOP" $1
  ${IfNot} ${Errors}
    !insertmacro UnselectSection ${SecDesktop}
  ${EndIf}
  ClearErrors
  ${GetOptions} $R0 "/NOSTARTMENU" $1
  ${IfNot} ${Errors}
    !insertmacro UnselectSection ${SecStartMenu}
  ${EndIf}
  StrCpy $RunAfter 0
  ClearErrors
  ${GetOptions} $R0 "/RUN" $1
  ${IfNot} ${Errors}
    StrCpy $RunAfter 1
  ${EndIf}
FunctionEnd

Function .onInstSuccess
  ${If} ${Silent}
  ${AndIf} $RunAfter == 1
    Call LaunchApp
  ${EndIf}
FunctionEnd

; ---------------------------------------------------------------- uninstaller
Function un.onInit
  SetRegView 64
  ; the context the install used: HKLM entry pointing here = all users
  StrCpy $UnAllUsers 0
  ReadRegStr $0 HKLM "${UNINST_KEY}" "InstallLocation"
  ${If} $0 == $INSTDIR
    StrCpy $UnAllUsers 1
    SetShellVarContext all
  ${Else}
    SetShellVarContext current
  ${EndIf}
FunctionEnd

Section "Uninstall"
  Call un.CloseRunningApp
  Delete "$DESKTOP\${APPNAME}.lnk"
  Delete "$SMPROGRAMS\${APPNAME}.lnk"

  RMDir /r "$INSTDIR\_internal"
  Delete "$INSTDIR\${APPEXE}"
  Delete "$INSTDIR\README.txt"
  Delete "$INSTDIR\LICENSE.txt"
  Delete "$INSTDIR\${UNINSTALLER}"

  ; the user's files: keep unless asked (silent: keep)
  ${If} ${FileExists} "$INSTDIR\Media\*.*"
  ${OrIf} ${FileExists} "$INSTDIR\Data\*.*"
    MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 "Also delete your Media and Data folders (videos, outputs, templates, settings)?$\r$\n$\r$\n$INSTDIR\Media$\r$\n$INSTDIR\Data$\r$\n$\r$\nChoose No to keep them." /SD IDNO IDYES deldata
    DetailPrint "Kept $INSTDIR\Media and $INSTDIR\Data"
    Goto datadone
    deldata:
      RMDir /r "$INSTDIR\Media"
      RMDir /r "$INSTDIR\Data"
    datadone:
  ${EndIf}
  RMDir "$INSTDIR"                      ; only if empty

  ${If} $UnAllUsers == 1
    DeleteRegKey HKLM "${UNINST_KEY}"
  ${Else}
    DeleteRegKey HKCU "${UNINST_KEY}"
  ${EndIf}
SectionEnd
