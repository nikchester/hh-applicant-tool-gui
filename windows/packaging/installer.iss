; Compile with Inno Setup 6.4.3. No elevation, runtime downloads or profile removal.
#if VER != EncodeVer(6, 4, 3)
  #error Use exactly Inno Setup 6.4.3
#endif
#ifndef AppSource
  #error AppSource must point to the checked PyInstaller onedir output
#endif
#ifndef OutputPath
  #error OutputPath is required
#endif
#ifndef AppVersion
  #define AppVersion "0.3.2"
#endif
[Setup]
AppId={{CA2072D5-592B-4A8D-98AB-596909B98D40}
AppName=Автоотклики HH
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\Автоотклики HH
DefaultGroupName=Автоотклики HH
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0
OutputDir={#OutputPath}
OutputBaseFilename=hh-applicant-tool-gui-{#AppVersion}-windows-x64-setup
; Bounded developer builds: deflate avoids very slow solid LZMA on this host.
; Trades a larger installer for fast, low-memory compression/decompression.
Compression=zip/1
SolidCompression=no
WizardStyle=modern
UninstallDisplayIcon={app}\hh-applicant-tool-gui.exe
CloseApplications=no
RestartApplications=no
SetupLogging=no

[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "{#AppSource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\Автоотклики HH"; Filename: "{app}\hh-applicant-tool-gui.exe"; WorkingDir: "{app}"
Name: "{userdesktop}\Автоотклики HH"; Filename: "{app}\hh-applicant-tool-gui.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[InstallDelete]
; Shortcuts of the former name "PocketNode HH"; never profile data.
Type: files; Name: "{userprograms}\PocketNode HH.lnk"
Type: files; Name: "{userdesktop}\PocketNode HH.lnk"
; Executable of the former name; the shortcuts above now start hh-applicant-tool-gui.exe.
Type: files; Name: "{app}\PocketNodeHH.exe"

; Deliberately no [Run], [UninstallDelete], or profile-directory [Dirs] entries.
