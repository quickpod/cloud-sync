; Inno Setup — Cloud Sync. Signed single-file installer, compiled in CI.
#define AppName "Cloud Sync"
#define AppVersion "1.1.0"

[Setup]
AppMutex=QuickOpen.CloudSync
AppId={{66F577E3-D43A-485E-BB9B-C981CEDB7430}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=QuickOpen (quickopen.ai)
AppPublisherURL=https://quickopen.ai/projects/cloud-sync
DefaultDirName={autopf}\CloudSync
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\CloudSync.exe
OutputDir=dist
OutputBaseFilename=CloudSync-Setup
SetupIconFile=..\cloud-sync.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
WizardImageFile=branding\wizard-large.bmp
WizardSmallImageFile=branding\wizard-small.bmp
AppCopyright=Apache-2.0. 100%% AI-built, published on QuickOpen (quickopen.ai).
VersionInfoCompany=QuickOpen
VersionInfoProductName=Cloud Sync
VersionInfoVersion=1.1.0.0
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel2=Cloud Sync is a 100%% AI-built, open-source offline tool, published on QuickOpen (quickopen.ai).%n%nThis will install it on your computer.
BeveledLabel=QuickOpen · quickopen.ai

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"
Name: "trustca"; Description: "Trust the QuickOpen Root CA (lets Windows verify QuickOpen signatures)"; GroupDescription: "Security:"; Flags: unchecked

[Files]
Source: "staging\CloudSync.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "staging\quickopen-root.crt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "staging\README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme skipifsourcedoesntexist
Source: "staging\LICENSE"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\Cloud Sync"; Filename: "{app}\CloudSync.exe"; IconFilename: "{app}\CloudSync.exe"
Name: "{group}\Uninstall Cloud Sync"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Cloud Sync"; Filename: "{app}\CloudSync.exe"; IconFilename: "{app}\CloudSync.exe"; Tasks: desktopicon

[Run]
Filename: "certutil.exe"; Parameters: "-addstore -user Root ""{app}\quickopen-root.crt"""; Tasks: trustca; Flags: runhidden; StatusMsg: "Trusting the QuickOpen Root CA..."
Filename: "{app}\CloudSync.exe"; Description: "Launch Cloud Sync now"; Flags: nowait postinstall skipifsilent

; Full uninstall: remove every app-owned trace. The QuickOpen Root CA is
; intentionally NOT touched — it is shared by all QuickOpen apps.
[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\CloudSync"
