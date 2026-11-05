from PyInstaller.utils.hooks import collect_submodules

hiddenimports = collect_submodules("hh_applicant_tool")
# HH 2.3.7 discovers operations through pkgutil.iter_modules(package.__path__).
# Keep loose .py files as well as compiled PYZ modules; test the unpatched parser.
module_collection_mode = {"hh_applicant_tool.operations": "pyz+py"}
