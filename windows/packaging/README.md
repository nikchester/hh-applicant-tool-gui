# Windows x64 installer (developer builds)

Produces a real **unsigned** Inno Setup `.exe`, not a launcher requiring Python.
The installed onedir runtime contains CPython 3.12, Tcl/Tk, SSL, CA certificates,
application modules, HH operations, ChatMock resources and distribution metadata.
No runtime pip, venv, PowerShell, account import or manual Python installation.

## Prerequisites and boundary

Build on native Windows x64 using a **clean CPython 3.12 x64 venv** and the exact
tools in `requirements-build.txt`. Separately obtain/review **Inno Setup 6.4.3**
from its official source. The build script checks the compiler version and all
Python pins and constraints; it never installs/downloads anything. Missing tools
fail before creating output. Tool authenticity/signatures are an operator check;
a matching version is not an authenticity guarantee.

The compiler pin is checked using its `VER` preprocessor constant with output
disabled: the authenticated official 6.4.3 binaries on this host expose a PE
product version of `0.0.0.0`. Development installers use `zip/1` without solid
compression after solid LZMA exceeded bounded local compilation attempts; this
trades a larger artifact for fast low-memory compression/decompression.

The two application pins are `hh-applicant-tool==2.3.7` and `chatmock==1.40`.
Their inspected core dependency constraints are compatible: HH permits Requests
2.32.5; ChatMock pins it. HH requires Pillow >=12.3,<13; ChatMock's Pillow 11.3
pin applies **only to its unused `[gui]` extra**. Do **not** install `chatmock[gui]`.
The GUI here is Tk, not ChatMock's Qt GUI. `pip check` and an offline dependency
graph check block incompatible prepared environments. If upstream constraints
change, stop: separate frozen runtimes require an explicit `gui_runtime` routing
change; do not override constraints or silently change the app.

## Prepare tools (developer action, not runtime installation)

From repository root, with an already reviewed, complete offline wheelhouse:

```powershell
py -3.12 -m venv windows/build/venv
windows/build/venv/Scripts/python.exe -m pip install --no-index --find-links C:/reviewed-wheelhouse -r windows/requirements-hh.txt -r windows/requirements-chatmock.txt -r windows/packaging/requirements-build.txt
windows/build/venv/Scripts/python.exe -m pip check
python windows/check_packaging.py
windows/build/venv/Scripts/python.exe windows/check_gui.py
windows/build/venv/Scripts/python.exe -B windows/check_gui_auth.py
windows/build/venv/Scripts/python.exe -B windows/packaging/check_source_smoke.py
./windows/build-installer.ps1 -Python ./windows/build/venv/Scripts/python.exe -Iscc 'C:/Program Files (x86)/Inno Setup 6/ISCC.exe'
```

To validate/build only the unsigned portable directory before separately
approving an Inno compiler installation (same freeze/audit/smoke/hash checks):

```powershell
./windows/build-installer.ps1 -Python ./windows/build/venv/Scripts/python.exe -PortableOnly
```

This mode needs all pinned Python build dependencies, but does not need or run
Inno Setup and does not create an installer.

If a wheel/tool is missing, obtain it through the normal reviewed dependency
process. Do not make the build script download a compiler or use `--no-deps`.
No network, auth, application actions, deployment, signing, installer execution,
Git commit or push is part of this local build. CI's existing dependency install
uses the package index; the optional manual artifact job also installs build
dependencies and requires the runner's preinstalled compiler to match 6.4.3.
It does not install Inno or publish a release. Missing compiler is a job blocker.

## Outputs and privacy

Each build has a fresh ID; existing output/user work is never deleted:

* `windows/build/<id>/stage`: exact source allowlist and generic template.
* `windows/build/<id>/pyinstaller`: local intermediate diagnostics (may contain
  developer installation paths; **do not distribute**).
* `windows/dist/<id>/app/PocketNodeHH/PocketNodeHH.exe`: portable onedir app;
  keep its entire directory together.
* `windows/dist/<id>/installer/PocketNodeHH-0.1.0-windows-x64-setup.exe`.
* `windows/dist/<id>/SHA256SUMS.txt`: relative-path SHA-256 inventory.
  The installer directory also contains a standalone hash manifest for the EXE.

`manifest.py` is a closed source allowlist, not a recursive repository copy.
Only `packaging/config.example.toml` becomes the generic template beside frozen
`gui_profile.py`. The personal repository config, resume map, history, contact
data, auth, logs, venvs and environment dumps are not inputs. Dependency metadata
is staged without `direct_url.json`/RECORD/local install paths. PyInstaller hooks
may additionally collect upstream wheel RECORD/INSTALLER/SBOM metadata from the
clean build venv; the closed source allowlist is unchanged. Artifact checks
reject known private/state names and linked files. This is a safety layer, **not
a general secret scanner**: review allowlisted source changes before distribution.

Hashes detect changes to these artifacts; verify with a trusted external copy of
the manifest. They are not signatures or proof of provenance. Build-tool versions
are pinned, but transitive application dependencies and wheel hashes are not
locked, and compiler/PE timestamps may vary: **not bit-for-bit reproducible**.
The final runtime carries `dependency-inventory.json` with the actual versions.

## Offline frozen validation

The build audits resource/privacy boundaries and runs the windowed executable
with `--packaging-smoke-check` using hidden private pipes and a timeout. This mode
imports all local runtime modules/HH/ChatMock, checks package versions, uses the
**unpatched** HH parser and `pkgutil.iter_modules` operation discovery, loads the
generic template, SSL/CA data and Tcl/Tk, then round-trips a hidden worker pipe
and rejects an invalid real `--worker` start before profile initialization.
An audit guard prohibits sockets and known profile/state files in the smoke
process. No GUI login, gateway bind, HH call, candidate lookup or profile creation.

The exclusive GUI ChatMock auth hook is installed **before any ChatMock import**.
A separate fresh-process `--packaging-gateway-smoke` additionally runs the real
`gui_runtime.gateway_main` import order against one newly generated temporary
dummy auth file. The real `chatmock_server` module is imported through its loader,
then only its serve entrypoint is replaced: no login, token refresh or server
startup runs. Captured ChatMock auth aliases are checked; deleting/restoring only
that disposable fixture verifies rejection/recovery without account fallback.
The smoke IO guard permits exactly that fixture, not other auth/profile paths.

Before build tools are installed, run both source smoke checks in fresh processes
with the existing separate local runtime dependencies (no installation/network):

```powershell
python -B windows/packaging/check_source_smoke.py --hh-site-packages ./venvs/hh/Lib/site-packages --chatmock-site-packages ./venvs/chatmock/Lib/site-packages
```

The runner creates a fresh allowlisted source stage with the packaging-owned
generic template. Its dependency overlay is scoped to child processes, prefers
ChatMock's pinned shared dependencies, disables user site/bytecode writes, and
does not alter either venv. This is source validation, **not a frozen build**.

To re-run, use Python only on the **developer machine** to wait/capture a
windowed EXE reliably (not `Start-Process` without all private streams):

```powershell
python -c "import subprocess,sys; p=subprocess.run([sys.argv[1],'--packaging-smoke-check'],input='',capture_output=True,text=True,encoding='utf-8',timeout=120,creationflags=subprocess.CREATE_NO_WINDOW); print(p.stdout); print(p.stderr); sys.exit(p.returncode)" windows/dist/<id>/app/PocketNodeHH/PocketNodeHH.exe
```

Real installer install/uninstall/upgrade and standard-user/DPI/antivirus testing
still need an explicitly approved disposable Windows VM. No installer is run
automatically. Tk smoke proves resource loading, not interactive usability.

## Installation, uninstall and rollback

Per-user `{localappdata}/Programs/Автоотклики HH`; no admin elevation. Start-menu
shortcut is per-user; desktop shortcut is optional. The installer never launches
the app, registers an auto-start task, stops processes or deletes profile data.
Close the app/owned workers manually before upgrading/uninstalling.
Uninstall removes installed files/shortcuts but deliberately preserves
`~/.config/pocketnode-hh-gui` and `~/.local/state/pocketnode-hh-gui` (including auth).
Warn shared-machine users that uninstall is **not** account-data erasure.

Keep the previous installer and its hash manifest. To roll back, close the app,
uninstall the new binaries and reinstall the reviewed prior installer; preserve
and back up profiles separately. Profile backward compatibility is not promised.
No cleanup of build output/profile data is automatic.

## Licensing and signing blockers

Installed dependency license texts and CPython `LICENSE.txt` are copied when
discoverable. ChatMock 1.40 includes `.dist-info/licenses/LICENSE`; the inspected
HH 2.3.7 wheel provides **neither license declaration nor license text**. This
repository also had no root license file at inspection. `THIRD-PARTY-NOTICES.txt`
reports uncertainty, not permission. Resolve application/HH redistribution
rights, source-offer/notice obligations, CPython/OpenSSL/Pillow/cryptography and
all dependency licenses before sharing installers. Review PyInstaller's bootloader
license/exception too; packaging tooling does not grant application rights.

Unsigned binaries/installers can trigger SmartScreen and antivirus warnings.
Do not tell users to bypass Windows security. Signing/notarization-equivalent
release decisions, certificate use and publication require separate approval.

Version-specific references (not fetched during offline implementation):
[PyInstaller 6.16.0 hooks and collection modes](https://pyinstaller.org/en/v6.16.0/hooks.html),
[runtime resources](https://pyinstaller.org/en/v6.16.0/runtime-information.html),
[windowed stdio caveat](https://pyinstaller.org/en/v6.16.0/common-issues-and-pitfalls.html),
[Inno privileges](https://jrsoftware.org/ishelp/topic_setup_privilegesrequired.htm),
[Inno architectures](https://jrsoftware.org/ishelp/topic_setup_architecturesallowed.htm).
The installed HH/ChatMock metadata/parser source were inspected locally. A local
CPython 3.12.10 x64 / PyInstaller 6.16.0 / authenticated Inno 6.4.3 build completed;
both frozen smoke modes passed. Installer execution/upgrade/uninstall remain
untested: the generated application installer was deliberately never run.
