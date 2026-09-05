; Inno Setup script for UniGo Sync -- wraps the PyInstaller-built
; UniGoSync.exe (see UniGoSync.spec) into a single Setup.exe an end user
; can run with no admin rights and no Python installed.
;
; Build with:
;   iscc unigo_sync/packaging/installer.iss
; Expects dist\UniGoSync.exe to already exist (run PyInstaller first) and
; icon.ico to exist alongside this script (run make_icon.py first) -- see
; ../../.github/workflows/build-windows-installer.yml for the full,
; verified sequence.

#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
#endif
#define MyAppName "UniGo Sync"
#define MyAppExeName "UniGoSync.exe"
#define MyAppPublisher "UniGo Sync"

[Setup]
AppId={{6C6B9A2B-6E3B-4E1B-9C7A-6B6B6C6B9A2B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; Installs per-user under LocalAppData rather than Program Files, and
; PrivilegesRequired=lowest means no UAC prompt / admin account needed --
; the whole point of this installer is a non-technical end user just
; double-clicking it.
DefaultDirName={localappdata}\Programs\UniGoSync
DefaultGroupName=UniGo Sync
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=UniGoSyncSetup
Compression=lzma2
SolidCompression=yes
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked
Name: "startupicon"; Description: "Start UniGo Sync automatically when Windows starts"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "..\..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
; onlyifdoesntexist: don't clobber a user's edited config.yaml on
; reinstall/upgrade. uninsneveruninstall: leave it behind on uninstall
; too, in case they reinstall later or just want to keep it for reference.
Source: "..\config.yaml"; DestDir: "{app}"; Flags: onlyifdoesntexist uninsneveruninstall

[Icons]
Name: "{group}\UniGo Sync"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\Uninstall UniGo Sync"; Filename: "{uninstallexe}"
Name: "{userdesktop}\UniGo Sync"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{userstartup}\UniGo Sync"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: startupicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch UniGo Sync now"; Flags: nowait postinstall skipifsilent
