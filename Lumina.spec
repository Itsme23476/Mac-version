# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for Lumina - macOS build
"""

import sys
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# Project paths
SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
APP_DIR = os.path.join(SPEC_DIR, 'ai_file_organizer')

# Collect all app submodules
hidden_imports = [
    # Core app modules
    'app',
    'app.core',
    'app.core.ai_organizer',
    'app.core.apply',
    'app.core.auto_updater',
    'app.core.auto_watcher',
    'app.core.categorize',
    'app.core.database',
    'app.core.embeddings',
    'app.core.exif_utils',
    'app.core.file_operations',
    'app.core.logging_config',
    'app.core.metadata_utils',
    'app.core.ocr',
    'app.core.plan',
    'app.core.query_parser',
    'app.core.redesign_dialog',
    'app.core.scan',
    'app.core.search',
    'app.core.settings',
    'app.core.smart_categorizer',
    'app.core.supabase_client',
    'app.core.text_extract',
    'app.core.update_checker',
    'app.core.vision',
    'app.ui',
    'app.ui.main_window',
    'app.ui.auth_dialog',
    'app.ui.theme_manager',
    'app.ui.organize_page',
    'app.ui.onboarding',
    'app.ui.quick_search_overlay',
    'app.ui.file_preview_window',
    'app.ui.mac_hotkey',
    'app.ui.win_hotkey',
    'app.ui.contextual_tips',
    'app.version',
    
    # PySide6 modules
    'PySide6.QtCore',
    'PySide6.QtGui',
    'PySide6.QtWidgets',
    'PySide6.QtMultimedia',
    'PySide6.QtMultimediaWidgets',
    'PySide6.QtPdf',
    'PySide6.QtPdfWidgets',
    
    # macOS specific
    'AppKit',
    'Foundation',
    'Quartz',
    'objc',
    
    # Other dependencies
    'PIL',
    'PIL.Image',
    'filetype',
    'requests',
    'certifi',
    'openai',
    'httpx',
    'json5',
    'pynput',
    'pynput.keyboard',
    'pynput.mouse',
    'dateparser',
    'dateparser.data',
    'regex',
    'PyPDF2',
    'rapidfuzz',
    'spellchecker',
    'sounddevice',
    'scipy',
    'scipy.io',
    'scipy.io.wavfile',
    'packaging',
    'packaging.version',
]

# Data files to include
datas = [
    # Resources
    (os.path.join(APP_DIR, 'resources', 'icon.icns'), 'resources'),
    (os.path.join(APP_DIR, 'resources', 'iconnn.ico'), 'resources'),
    (os.path.join(APP_DIR, 'resources', 'category_defaults.json'), 'resources'),
    
    # App modules (ensure they're found)
    (os.path.join(APP_DIR, 'app'), 'app'),
]

# Collect PySide6 data files (plugins, etc.)
datas += collect_data_files('PySide6', include_py_files=False)

# Collect certifi certificates
datas += collect_data_files('certifi')

# Collect spellchecker language dictionaries
datas += collect_data_files('spellchecker')

# Collect dateparser data files (language definitions)
datas += collect_data_files('dateparser')

# Collect regex data files (used by dateparser)
try:
    datas += collect_data_files('regex')
except Exception:
    pass  # regex might not have data files in all versions

a = Analysis(
    [os.path.join(APP_DIR, 'main.py')],
    pathex=[APP_DIR, SPEC_DIR],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'numpy.testing',
        'pytest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Lumina',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # No console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=os.path.join(SPEC_DIR, 'build', 'entitlements.plist'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Lumina',
)

app = BUNDLE(
    coll,
    name='Lumina.app',
    icon=os.path.join(APP_DIR, 'resources', 'icon.icns'),
    bundle_identifier='com.lumina.filesearch',
    info_plist=os.path.join(SPEC_DIR, 'build', 'Info.plist'),
)
