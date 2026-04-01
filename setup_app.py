"""py2app build configuration for OpenShrimp.app.

Build a self-contained macOS .app bundle.  Setuptools auto-discovers
``pyproject.toml`` in the working directory and conflicts with the
hatchling build backend, so hide it before running::

    mv pyproject.toml pyproject.toml.bak
    python setup_app.py py2app
    mv pyproject.toml.bak pyproject.toml

For a development (alias) build that symlinks into the source tree::

    mv pyproject.toml pyproject.toml.bak
    python setup_app.py py2app -A
    mv pyproject.toml.bak pyproject.toml
"""

from pathlib import Path

from setuptools import setup

_VERSION = Path("VERSION").read_text().strip()

APP = ["src/open_shrimp/platform/macos/app.py"]
DATA_FILES: list[tuple[str, list[str]]] = []

_RESOURCES = Path("src/open_shrimp/platform/macos/resources")

# Include the icon if it exists (may not be generated yet)
_ICON = _RESOURCES / "icon.icns"
_ICON_FILE = str(_ICON) if _ICON.exists() else None

OPTIONS: dict = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": "OpenShrimp",
        "CFBundleDisplayName": "OpenShrimp",
        "CFBundleIdentifier": "com.openshrimp.app",
        "CFBundleVersion": _VERSION,
        "CFBundleShortVersionString": _VERSION,
        "LSUIElement": True,  # No Dock icon — menu bar only
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
    },
    "packages": [
        "open_shrimp",
        "telegram",
        "httpx",
        "httpcore",
        "anyio",
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
        "open_shrimp.platform.macos.app",
        "open_shrimp.platform.macos.app_setup",
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
    name="OpenShrimp",
    version=_VERSION,
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
)
