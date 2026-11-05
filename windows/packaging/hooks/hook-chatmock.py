from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hiddenimports = collect_submodules("chatmock", filter=lambda name: not name.startswith("chatmock.gui"))
datas = collect_data_files("chatmock", includes=[
    "**/*.html", "**/*.css", "**/*.js", "**/*.svg", "**/*.png", "**/*.json", "**/*.txt",
], excludes=["**/auth.json", "**/token.json", "**/config.toml", "**/direct_url.json"])
