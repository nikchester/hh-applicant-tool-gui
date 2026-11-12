"""Release-only: build the signed self-update files next to the installer.

    python update_bundle.py <frozen app dir> <installer exe> <output dir>

Needs UPDATE_SIGNING_KEY: base64 of the raw 32-byte ed25519 private key (CI secret).
Writes <output>/<code zip>, update.json and update.json.sig. The code zip holds exactly
the files of <app>/_internal/code, the same code the installer ships.
"""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import zipfile

CODE_FILE = re.compile(r"^(?:[a-z_][a-z0-9_]{0,60}\.py|config\.example\.toml|version\.json)$")
FIXED_TIME = (2020, 1, 1, 0, 0, 0)  # Same bytes for the same code.


def describe(path):
    data = Path(path).read_bytes()
    return {"name": Path(path).name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def build(app, installer, output, private_key):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    code = Path(app) / "_internal" / "code"
    info = json.loads((code / "version.json").read_text(encoding="utf-8"))
    files = sorted(path for path in code.iterdir())
    if not files or any(not path.is_file() or not CODE_FILE.match(path.name) for path in files):
        raise SystemExit("Unexpected file in the code directory")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"hh-applicant-tool-gui-{info['version']}-code.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            entry = zipfile.ZipInfo(path.name, FIXED_TIME)
            entry.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(entry, path.read_bytes())
    metadata = json.dumps({"version": info["version"], "runtime": info["runtime"],
                           "code": describe(archive), "installer": describe(installer)},
                          indent=2).encode("utf-8")
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key))
    (output / "update.json").write_bytes(metadata)
    (output / "update.json.sig").write_text(base64.b64encode(key.sign(metadata)).decode("ascii") + "\n",
                                            encoding="ascii")
    return archive


def main(argv):
    if len(argv) != 3:
        raise SystemExit(__doc__)
    key = os.environ.get("UPDATE_SIGNING_KEY", "").strip()
    if not key:
        raise SystemExit("UPDATE_SIGNING_KEY is not set: self-update files cannot be signed")
    print(build(*argv, key))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
