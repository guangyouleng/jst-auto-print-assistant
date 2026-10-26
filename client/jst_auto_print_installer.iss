; Requires Inno Setup 6.3 or newer and Languages\ChineseSimplified.isl.
#define AppVersion "0.5.25"
#define AppExeName "JSTAutoPrint_Win10_21H1.exe"
#define PayloadRoot SourcePath + "dist_win10_x64\JSTAutoPrint_Win10_21H1"

[Setup]
AppId={{26ACE70D-CC00-4A9C-87D6-6D1773732C1A}
AppName=聚水潭安全打单助手
AppVersion={#AppVersion}
AppVerName=聚水潭安全打单助手 {#AppVersion}
DefaultDirName={localappdata}\Programs\JSTAutoPrint
DefaultGroupName=聚水潭安全打单助手
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
; Keep the default installer architecture for Inno Setup 6.3+ compatibility.
; The application payload and installation mode below remain x64.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19041
AppMutex=Global\JSTAutoPrintAssistant
CloseApplications=yes
CloseApplicationsFilter={#AppExeName}
RestartApplications=no
UninstallDisplayIcon={app}\{#AppExeName}
OutputDir=dist_win10_installer
OutputBaseFilename=JSTAutoPrint_Windows_x64_V{#AppVersion}_Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
VersionInfoVersion=0.5.25.0
VersionInfoDescription=聚水潭安全打单助手安装程序
VersionInfoProductName=聚水潭安全打单助手
VersionInfoProductVersion={#AppVersion}

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："; Flags: checkedonce

[Files]
Source: "{#PayloadRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\聚水潭安全打单助手"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{autoprograms}\聚水潭安全打单助手（试运行不打印）"; Filename: "{app}\{#AppExeName}"; Parameters: "--no-print"; WorkingDir: "{app}"
Name: "{autoprograms}\聚水潭安全打单助手离线自检"; Filename: "{app}\Win10_离线自检.bat"; WorkingDir: "{app}"
Name: "{autoprograms}\聚水潭安全打单助手后台网络诊断"; Filename: "{app}\Win10_后台网络诊断.bat"; WorkingDir: "{app}"
Name: "{autodesktop}\聚水潭安全打单助手"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "安装完成后启动聚水潭安全打单助手"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent
