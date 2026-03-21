"""py2app build configuration for OpenUdang.app.

Build a self-contained macOS .app bundle:

    python setup_app.py py2app

For a development (alias) build that symlinks into the source tree:

    python setup_app.py py2app -A
"""

from pathlib import Path

from setuptools import setup

_VERSION = Path("VERSION").read_text().strip()

APP = ["src/open_udang/platform/macos/app.py"]
DATA_FILES: list[tuple[str, list[str]]] = []

_RESOURCES = Path("src/open_udang/platform/macos/resources")

# Include the icon if it exists (may not be generated yet)
_ICON = _RESOURCES / "icon.icns"
_ICON_FILE = str(_ICON) if _ICON.exists() else None

OPTIONS: dict = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": "OpenUdang",
        "CFBundleDisplayName": "OpenUdang",
        "CFBundleIdentifier": "com.openudang.app",
        "CFBundleVersion": _VERSION,
        "CFBundleShortVersionString": _VERSION,
        "LSUIElement": True,  # No Dock icon — menu bar only
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
    },
    "packages": [
        "open_udang",
        "telegram",
        "httpx",
        "httpcore",
        "aiosqlite",
        "yaml",
        "mistune",
        "starlette",
        "uvicorn",
        "tree_sitter",
        "tree_sitter_bash",
        "platformdirs",
        "rumps",
    ],
    "includes": [
        "rumps",
        "open_udang.platform.macos.app",
        "open_udang.platform.macos.app_setup",
    ],
    "excludes": [
        # Trim unnecessary stdlib modules to reduce bundle size
        "tkinter",
        "unittest",
        "test",
    ],
    "resources": [],
}

if _ICON_FILE:
    OPTIONS["iconfile"] = _ICON_FILE

# Include menu bar template images if they exist
for _img in ("menubar-icon.png", "menubar-icon@2x.png"):
    _path = _RESOURCES / _img
    if _path.exists():
        OPTIONS["resources"].append(str(_path))

setup(
    name="OpenUdang",
    version=_VERSION,
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
)
