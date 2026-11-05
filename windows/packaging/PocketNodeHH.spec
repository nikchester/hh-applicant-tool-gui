# PyInstaller 6.16.0, CPython 3.12 x64. No repository-wide data collection.
import os
from pathlib import Path
import sys

stage = Path(os.environ["HH_PACKAGING_STAGE"]).resolve()
sys.path.insert(0, str(stage))
from manifest import APP_MODULES, APP_NAME

datas = [(str(stage / "config.example.toml"), ".")]
for path in sorted((stage / "metadata").rglob("*")):
    if path.is_file():
        datas.append((str(path), str(path.parent.relative_to(stage / "metadata"))))
datas.append((str(stage / "THIRD-PARTY-NOTICES.txt"), "."))
a = Analysis(
    [str(stage / "entrypoint.py")], pathex=[str(stage)],
    # Shared explicit list includes gui_chatmock_auth, gui_owned_process, gui_recovery.
    binaries=[], datas=datas, hiddenimports=list(APP_MODULES) + ["smoke", "manifest", "certifi", "tkinter"],
    hookspath=[str(Path(SPECPATH) / "hooks")],
    excludes=["PySide6", "PyQt5", "PyQt6", "pip", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=APP_NAME,
          console=False, disable_windowed_traceback=True, debug=False,
          strip=False, upx=False, uac_admin=False, target_arch="x86_64")
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=APP_NAME)
