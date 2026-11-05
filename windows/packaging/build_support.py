"""Local build checks/staging/inventory only. Never install or download anything."""

import argparse
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import sys
import tomllib

from manifest import (APPLICATION_PINS, APP_MODULES, BUILD_PINS, ENTRY_FILES,
                      PRIVATE_NAMES)

HERE = Path(__file__).resolve().parent
WINDOWS = HERE.parent


def preflight():
    errors = []
    if sys.platform != "win32" or struct.calcsize("P") != 8 or platform.machine().lower() not in {"amd64", "x86_64"}:
        errors.append("Use native Windows x64 CPython")
    if sys.version_info[:2] != (3, 12):
        errors.append("Use CPython 3.12 x64")
    if sys.prefix == sys.base_prefix:
        errors.append("Use a clean, prepared build venv (not system Python)")
    for name, expected in {**APPLICATION_PINS, **BUILD_PINS}.items():
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError:
            actual = "missing"
        if actual != expected:
            errors.append(f"{name}: expected {expected}, found {actual}")
    if errors:
        raise RuntimeError("Build prerequisites unavailable:\n" + "\n".join(errors))
    # Resolve installed constraints offline, including requests[socks]. No pip resolver/network.
    distributions = dependency_closure()
    import tkinter
    import _ssl
    import certifi
    assert Path(certifi.where()).is_file()
    print("Build preflight OK; application dependency closure:", len(distributions))


def dependency_closure():
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    selected, requested = {}, {}
    pending = [Requirement(f"{name}=={version}") for name, version in APPLICATION_PINS.items()]
    while pending:
        requirement = pending.pop()
        name = canonicalize_name(requirement.name)
        distribution = metadata.distribution(name)
        if not requirement.specifier.contains(distribution.version, prereleases=True):
            raise RuntimeError("Incompatible installed dependency: " + name)
        extras = requested.get(name, set()) | set(requirement.extras)
        if name in selected and extras == requested[name]:
            continue
        selected[name], requested[name] = distribution, extras
        for text in distribution.requires or []:
            child = Requirement(text)
            if child.marker is None or any(child.marker.evaluate({"extra": extra}) for extra in {"", *extras}):
                pending.append(child)
    return selected


def validate_template(path):
    config = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    if set(config) != {"general", "openai"} or set(config["general"]) != {"cover_letter_prompt"}:
        raise ValueError("Generic template schema changed; review before packaging")
    if set(config["openai"]) != {"default", "cover_letter"}:
        raise ValueError("Unexpected template model sections")
    expected = {"base_url", "model", "delay", "timeout", "connect_timeout", "max_retries", "max_completion_tokens"}
    default = config["openai"]["default"]
    if (set(default) != expected or default["base_url"] != "http://127.0.0.1:8000/v1"
            or default["model"] != "CHOOSE_AFTER_LOGIN"
            or set(config["openai"]["cover_letter"]) != {"system_prompt", "temperature"}):
        raise ValueError("Generic template has unexpected configuration")
    return config


def stage_sources(destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)  # Never reuse potentially contaminated staging.
    for name in APP_MODULES:
        source = WINDOWS / (name + ".py")
        if source.is_symlink() or not source.is_file():
            raise ValueError("Missing or linked application source: " + name)
        shutil.copyfile(source, destination / source.name)
    for name in ENTRY_FILES:
        shutil.copyfile(HERE / name, destination / name)
    validate_template(HERE / "config.example.toml")
    shutil.copyfile(HERE / "config.example.toml", destination / "config.example.toml")
    return destination


def stage_dependencies(destination):
    destination = Path(destination)
    target = destination / "metadata"
    target.mkdir(exist_ok=False)
    inventory, notices = [], [
        "THIRD-PARTY COMPONENT INVENTORY (development build; not a license clearance)",
        "Inspect bundled metadata/license texts before any redistribution.",
        "No license grant is inferred when upstream metadata or texts are missing.",
        "HH 2.3.7 wheel inspected locally had NO license declaration or license file.",
        "Obtain and review upstream HH and application redistribution permissions before release.",
        "",
    ]
    components = dependency_closure()
    # The bootloader is part of the installed executable, not merely a build tool.
    components["pyinstaller"] = metadata.distribution("pyinstaller")
    for name, distribution in sorted(components.items()):
        files = distribution.files or []
        metadata_file = next((Path(str(p)) for p in files if str(p).replace("\\", "/").endswith(".dist-info/METADATA")), None)
        if metadata_file is None or len(metadata_file.parts) != 2:
            raise RuntimeError("Unsupported dependency metadata layout: " + name)
        directory = target / metadata_file.parent
        directory.mkdir()
        # Do not copy direct_url.json, installer state, repository paths or RECORD.
        for filename in ("METADATA", "WHEEL", "entry_points.txt", "top_level.txt"):
            text = distribution.read_text(filename)
            if text is not None:
                (directory / filename).write_text(text, encoding="utf-8")
        license_files = []
        for recorded in files:
            relative = Path(str(recorded))
            if relative.is_absolute() or ".." in relative.parts:
                continue
            basename = relative.name.lower()
            if not ("licenses" in [p.lower() for p in relative.parts]
                    or basename.startswith(("license", "copying", "notice"))):
                continue
            source = Path(distribution.locate_file(recorded))
            if source.is_file() and not source.is_symlink():
                output = target / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, output)
                license_files.append(relative.as_posix())
        declaration = distribution.metadata.get("License-Expression") or distribution.metadata.get("License") or "UNSPECIFIED"
        inventory.append({"name": name, "version": distribution.version, "license": declaration,
                          "license_files": sorted(license_files)})
        notices.append(f"{name}=={distribution.version}: license={declaration}; texts={len(license_files)}")
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError("CPython LICENSE.txt missing; prepare a complete official Python installation")
    shutil.copyfile(python_license, target / "Python-LICENSE.txt")
    notices += ["", "Python LICENSE.txt includes CPython and bundled third-party notices.",
                "PyInstaller bootloader license/exception: review PyInstaller 6.16.0 distribution before release.",
                "This inventory does not certify source-offer, attribution or redistribution obligations."]
    (destination / "THIRD-PARTY-NOTICES.txt").write_text("\n".join(notices) + "\n", encoding="utf-8")
    # Keep inventory alongside notices in the final runtime without local installation paths.
    (target / "dependency-inventory.json").write_text(json.dumps({
        "python": platform.python_version(), "dependencies": inventory, "build_tools": BUILD_PINS,
    }, indent=2) + "\n", encoding="utf-8")


def audit_tree(root):
    root = Path(root)
    paths, pending = [], [root]
    while pending:
        directory = pending.pop()
        if directory.is_symlink() or directory.is_junction():
            raise ValueError("Linked artifact root/directory")
        for path in sorted(directory.iterdir()):
            # Check before descent, never traverse a junction into external data.
            if path.is_symlink() or path.is_junction():
                raise ValueError("Linked path in generated artifact")
            if path.name.lower() in PRIVATE_NAMES or path.suffix.lower() in {".log", ".pid"}:
                raise ValueError("Private/generated state in artifact: " + path.name)
            if path.name.lower() in {"venvs", ".venv", ".git", "__pycache__"}:
                raise ValueError("Unexpected source environment in artifact")
            paths.append(path)
            if path.is_dir():
                pending.append(path)
    return sorted(paths)


def audit_runtime(root):
    root = Path(root)
    audit_tree(root)
    internal = root / "_internal"
    required = [root / "PocketNodeHH.exe", internal / "config.example.toml",
                internal / "THIRD-PARTY-NOTICES.txt", internal / "dependency-inventory.json",
                internal / "Python-LICENSE.txt", internal / "certifi/cacert.pem",
                internal / "hh_applicant_tool/operations/authorize.py",
                internal / "hh_applicant_tool/operations/apply_vacancies.py",
                internal / "hh_applicant_tool/operations/reply_chats.py"]
    if not all(path.is_file() for path in required):
        raise RuntimeError("Frozen runtime missing required data, licenses or discovery sources")
    if not (list(internal.glob("python312.dll")) and list(internal.glob("_ssl.pyd"))
            and (internal / "_tcl_data").is_dir() and (internal / "_tk_data").is_dir()):
        raise RuntimeError("Frozen Python/SSL/Tcl/Tk runtime incomplete")
    validate_template(internal / "config.example.toml")
    print("Frozen runtime privacy/resource audit OK")


def hashes(root):
    root = Path(root)
    lines = []
    for path in audit_tree(root):
        if path.is_file() and path.name != "SHA256SUMS.txt":
            lines.append(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.relative_to(root).as_posix())
    (root / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="ascii")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("preflight", "stage", "audit", "hashes"))
    parser.add_argument("path", nargs="?", type=Path)
    args = parser.parse_args()
    if args.command == "preflight":
        preflight()
    elif args.path is None:
        parser.error("path is required")
    elif args.command == "stage":
        stage_sources(args.path)
        stage_dependencies(args.path)
        audit_tree(args.path)
        hashes(args.path)
    elif args.command == "audit":
        audit_runtime(args.path)
    else:
        hashes(args.path)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, metadata.PackageNotFoundError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
