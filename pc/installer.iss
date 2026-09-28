; Inno Setup 6 script — packages pc\dist\LANTooth\ (built by lantooth.spec) into
; dist\LANTooth-<version>-setup.exe. Built by build_exe.ps1 -Installer, which
; passes the version from the repo-root VERSION file:
;   ISCC.exe /DAppVersion=0.1.0 installer.iss
;
; Installs per user by default (no admin prompt); the user can choose "all
; users" in the first dialog. Settings/identity in %APPDATA%\LANTooth are left
; in place on uninstall, so reinstalling keeps the phone's trust in this PC.

#ifndef AppVersion
  #error Pass /DAppVersion=x.y.z (see build_exe.ps1)
#endif

[Setup]
AppId={{6F1C2D0B-8E4A-4B7C-9A51-3D2E7F8B4C10}
AppName=LANTooth
AppVersion={#AppVersion}
AppVerName=LANTooth {#AppVersion}
AppPublisher=LANTooth
DefaultDirName={autopf}\LANTooth
DefaultGroupName=LANTooth
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=dist
OutputBaseFilename=LANTooth-{#AppVersion}-setup
SetupIconFile=build\lantooth.ico
UninstallDisplayIcon={app}\LANTooth.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Close a running LANTooth (it holds this mutex) before replacing its files.
AppMutex=Local\LANTooth.GUI
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "autostart"; Description: "Start LANTooth when I sign in to Windows"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "dist\LANTooth\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\LANTooth"; Filename: "{app}\LANTooth.exe"
Name: "{group}\Uninstall LANTooth"; Filename: "{uninstallexe}"
Name: "{autodesktop}\LANTooth"; Filename: "{app}\LANTooth.exe"; Tasks: desktopicon
Name: "{userstartup}\LANTooth"; Filename: "{app}\LANTooth.exe"; Tasks: autostart

[Run]
Filename: "{app}\LANTooth.exe"; Description: "{cm:LaunchProgram,LANTooth}"; Flags: nowait postinstall skipifsilent
