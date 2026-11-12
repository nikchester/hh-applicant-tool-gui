# PyInstaller 6.16.0, CPython 3.12 x64. No repository-wide data collection.
import hashlib
import json
import os
from pathlib import Path
import sys

stage = Path(os.environ["HH_PACKAGING_STAGE"]).resolve()
sys.path.insert(0, str(stage))
from manifest import APP_MODULES, APP_NAME, APP_VERSION

datas = [(str(stage / "config.example.toml"), ".")]
for path in sorted((stage / "metadata").rglob("*")):
    if path.is_file():
        datas.append((str(path), str(path.parent.relative_to(stage / "metadata"))))
datas.append((str(stage / "THIRD-PARTY-NOTICES.txt"), "."))
a = Analysis(
    [str(stage / "entrypoint.py")], pathex=[str(stage)],
    # Application modules are analysed only to collect their libraries; their own code
    # ships as files in _internal/code (see entrypoint.py and updater.py).
    binaries=[], datas=datas,
    hiddenimports=list(APP_MODULES) + ["smoke", "manifest", "certifi", "tkinter",
                                       "cryptography.hazmat.primitives.asymmetric.ed25519"],
    hookspath=[str(Path(SPECPATH) / "hooks")],
    excludes=["PySide6", "PyQt5", "PyQt6", "pip", "pytest"],
    noarchive=False,
)
app_modules = set(APP_MODULES)
a.pure = [entry for entry in a.pure if entry[0] not in app_modules]

# Runtime fingerprint: the frozen modules and binaries. Downloaded code runs only on the
# runtime it was built for; another fingerprint means a full installer update.
runtime_names = sorted(entry[0] for entry in a.pure) + sorted(entry[0] for entry in a.binaries)
runtime = hashlib.sha256(("\n".join([sys.version] + runtime_names)).encode("utf-8")).hexdigest()
code = stage / "code-meta"
code.mkdir(exist_ok=True)
(code / "version.json").write_text(json.dumps({"version": APP_VERSION, "runtime": runtime}),
                                   encoding="utf-8")
code_files = [stage / (name + ".py") for name in sorted(APP_MODULES)]
code_files += [stage / "config.example.toml", code / "version.json"]
a.datas += [("code/" + path.name, str(path), "DATA") for path in code_files]

pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=APP_NAME,
          console=False, disable_windowed_traceback=True, debug=False,
          strip=False, upx=False, uac_admin=False, target_arch="x86_64")
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=APP_NAME)
