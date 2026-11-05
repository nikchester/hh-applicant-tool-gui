; Preflight only: /O- disables installer output; nothing is installed or run.
#if VER != EncodeVer(6, 4, 3)
  #error Use exactly Inno Setup 6.4.3
#endif
[Setup]
AppName=PocketNode compiler version probe
AppVersion=0.0.0
DefaultDirName={tmp}\PocketNode-compiler-version-probe
CreateAppDir=no
Uninstallable=no
PrivilegesRequired=lowest
Output=no
