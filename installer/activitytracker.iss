; Windows installer for Activity Tracker (Inno Setup 6). Built in CI:
;   iscc /DAppVersion=<version> /DSourceDir=<pyinstaller dist\ActivityTracker> installer\activitytracker.iss
; Per-user install: no admin prompt. Settings go to %LOCALAPPDATA%\ActivityTracker
; (the installed copy has no "data" folder next to it, so it isn't portable).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\ActivityTracker"
#endif

[Setup]
AppId={{6B7C3E38-4586-460F-8EED-74FDABE84C94}
AppName=Activity Tracker
AppVersion={#AppVersion}
AppPublisher=Activity Tracker
AppPublisherURL=https://github.com/npezarro/activity-tracker-app
AppSupportURL=https://github.com/npezarro/activity-tracker-app/issues
DefaultDirName={localappdata}\Programs\ActivityTracker
DefaultGroupName=Activity Tracker
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=ActivityTracker-Setup-x64
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\ActivityTracker.exe
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
CloseApplications=force
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "autostart"; Description: "Start Activity Tracker when I sign in to Windows"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "data\*"

[Icons]
Name: "{group}\Activity Tracker"; Filename: "{app}\ActivityTracker.exe"
Name: "{group}\Uninstall Activity Tracker"; Filename: "{uninstallexe}"
Name: "{userdesktop}\Activity Tracker"; Filename: "{app}\ActivityTracker.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "ActivityTracker"; ValueData: """{app}\ActivityTracker.exe"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\ActivityTracker.exe"; Description: "Launch Activity Tracker"; Flags: nowait postinstall skipifsilent
; In-app updates run this installer with /SILENT /RELAUNCH=1: start the new version afterwards.
Filename: "{app}\ActivityTracker.exe"; Flags: nowait; Check: ShouldRelaunch

[UninstallRun]
Filename: "{cmd}"; Parameters: "/c taskkill /IM ActivityTracker.exe /F"; Flags: runhidden; RunOnceId: "StopActivityTracker"

[Code]
function ShouldRelaunch: Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;
