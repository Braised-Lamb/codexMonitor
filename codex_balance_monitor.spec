# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files


project_dir = Path(SPECPATH)

block_cipher = None


# ============================================================
# Collect third-party package resources
# ============================================================

# PySide6 resource files
pyside6_datas = collect_data_files("PySide6")


# ============================================================
# Analysis
# ============================================================

a = Analysis(
    [
        str(project_dir / "codex_balance_monitor.py"),
    ],

    pathex=[
        str(project_dir),
    ],

    binaries=[],

    datas=[
        (
            str(project_dir / "resources" / "codex_monitor_logo.ico"),
            "resources"
        ),
        (
            str(project_dir / "resources" / "codex_monitor_tray.ico"),
            "resources"
        ),
        (
            str(project_dir / "resources" / "codex_monitor_logo.png"),
            "resources"
        ),
        (
            str(project_dir / "config" / "codex_balance_monitor_settings.json"),
            "config"
        ),
        (
            str(project_dir / "config" / "codex_balance_monitor_quota_history.json"),
            "config"
        ),
    ],

    hiddenimports=[
        # PySide6
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "PySide6.QtSvg",

        # System tray support
        "PySide6.QtNetwork",
    ],

    hookspath=[],

    hooksconfig={},

    runtime_hooks=[],

    excludes=[
        # unused large packages
        "matplotlib",
        "numpy",
        "pandas",
        "tkinter",
    ],

    win_no_prefer_redirects=False,

    win_private_assemblies=False,

    cipher=block_cipher,

    noarchive=False,
)


# ============================================================
# Python archive
# ============================================================

pyz = PYZ(
    a.pure,
    a.zipped_data,
    cipher=block_cipher
)


# ============================================================
# Executable
# ============================================================

exe = EXE(
    pyz,

    a.scripts,

    a.binaries,

    a.datas,

    [],

    name="CodexBalanceMonitor",

    debug=False,

    bootloader_ignore_signals=False,

    strip=False,

    upx=True,

    upx_exclude=[],

    runtime_tmpdir=None,


    # Hide console window
    console=False,


    # Application icon
    icon=str(
        project_dir /
        "resources" /
        "codex_monitor_logo.ico"
    ),


    # Windows version information
    version=str(
        project_dir /
        "codex_monitor_version.txt"
    ),


    disable_windowed_traceback=False,
)