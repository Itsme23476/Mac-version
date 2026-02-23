"""
Auto-updater for the Lumina app.
Downloads installer from releases and runs it to apply updates.
"""

import logging
import os
import sys
import shutil
import tempfile
import subprocess
import ssl
import certifi
from pathlib import Path
from typing import Optional, Callable

# Use requests library - much better SSL handling for PyInstaller apps
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    import urllib.request
    HAS_REQUESTS = False

logger = logging.getLogger(__name__)


def get_app_dir() -> Path:
    """Get the directory where the app is installed."""
    if getattr(sys, 'frozen', False):
        # Running as compiled exe
        return Path(sys.executable).parent
    else:
        # Running as script - use the ai_file_organizer folder
        return Path(__file__).parent.parent.parent


def get_update_dir() -> Path:
    """Get temporary directory for update downloads."""
    update_dir = Path(tempfile.gettempdir()) / "lumina_update"
    
    # Clean up any existing directory to avoid permission issues
    if update_dir.exists():
        try:
            shutil.rmtree(update_dir)
        except Exception as e:
            logger.warning(f"Could not clean update dir: {e}")
            # Try alternative directory with timestamp
            import time
            update_dir = Path(tempfile.gettempdir()) / f"lumina_update_{int(time.time())}"
    
    update_dir.mkdir(exist_ok=True)
    return update_dir


def download_update(
    download_url: str,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    status_callback: Optional[Callable[[str], None]] = None
) -> Optional[Path]:
    """
    Download update installer from URL.
    
    Args:
        download_url: URL to download from (GitHub Release asset)
        progress_callback: Optional callback(downloaded_bytes, total_bytes)
        status_callback: Optional callback(status_message) for UI updates
        
    Returns:
        Path to downloaded installer file, or None on failure
    """
    def update_status(msg: str):
        logger.info(msg)
        if status_callback:
            status_callback(msg)
    
    try:
        update_dir = get_update_dir()
        
        # Determine filename from URL based on platform
        filename = download_url.split('/')[-1]
        if sys.platform == 'darwin':
            # macOS: expect .dmg file
            if not filename.endswith('.dmg'):
                filename = "Lumina.dmg"
        else:
            # Windows: expect .exe file
            if not filename.endswith('.exe'):
                filename = "Lumina-Setup.exe"
        
        installer_path = update_dir / filename
        
        # Clean up any previous download
        if installer_path.exists():
            installer_path.unlink()
        
        update_status("Connecting to server...")
        logger.info(f"Downloading update from: {download_url}")
        
        if HAS_REQUESTS:
            return _download_with_requests(download_url, installer_path, progress_callback, update_status)
        else:
            return _download_with_urllib(download_url, installer_path, progress_callback, update_status)
        
    except Exception as e:
        logger.error(f"Download failed: {e}", exc_info=True)
        if status_callback:
            status_callback(f"Download failed: {str(e)[:50]}")
        return None


def _download_with_requests(
    download_url: str,
    installer_path: Path,
    progress_callback: Optional[Callable[[int, int], None]],
    update_status: Callable[[str], None]
) -> Optional[Path]:
    """Download using requests library - better SSL handling."""
    try:
        update_status("Establishing secure connection...")
        
        # Use requests with streaming for large files
        # verify=True uses certifi's certificates which work in PyInstaller
        response = requests.get(
            download_url,
            stream=True,
            timeout=(30, 300),  # (connect timeout, read timeout)
            headers={
                'User-Agent': 'Lumina-Updater/2.0',
                'Accept': 'application/octet-stream'
            },
            allow_redirects=True
        )
        response.raise_for_status()
        
        total_size = int(response.headers.get('content-length', 0))
        downloaded = 0
        chunk_size = 131072  # 128KB chunks
        
        if total_size > 0:
            update_status(f"Downloading... 0 / {total_size / (1024*1024):.1f} MB")
            logger.info(f"Download size: {total_size / (1024*1024):.2f} MB")
        else:
            update_status("Downloading...")
        
        # Initial progress callback
        if progress_callback:
            progress_callback(0, total_size if total_size > 0 else 1)
        
        with open(installer_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    
                    if progress_callback:
                        progress_callback(downloaded, total_size if total_size > 0 else downloaded)
                    
                    # Update status every ~5MB
                    if total_size > 0 and downloaded % (5 * 1024 * 1024) < chunk_size:
                        percent = int((downloaded / total_size) * 100)
                        update_status(f"Downloading... {downloaded / (1024*1024):.1f} / {total_size / (1024*1024):.1f} MB ({percent}%)")
        
        # Verify the file was downloaded
        if installer_path.exists() and installer_path.stat().st_size > 0:
            actual_size = installer_path.stat().st_size
            logger.info(f"Download complete: {installer_path} ({actual_size / (1024*1024):.2f} MB)")
            update_status("Download complete!")
            return installer_path
        else:
            logger.error("Downloaded file is empty or missing")
            update_status("Download failed - file is empty")
            return None
            
    except requests.exceptions.SSLError as e:
        logger.error(f"SSL Error: {e}")
        update_status("SSL certificate error - trying fallback...")
        # Try with SSL verification disabled as fallback (not ideal but works)
        return _download_with_requests_no_verify(download_url, installer_path, progress_callback, update_status)
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connection Error: {e}")
        update_status("Connection failed - check internet")
        return None
    except requests.exceptions.Timeout as e:
        logger.error(f"Timeout: {e}")
        update_status("Connection timed out")
        return None
    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP Error: {e.response.status_code} {e.response.reason}")
        update_status(f"Server error: {e.response.status_code}")
        return None
    except Exception as e:
        logger.error(f"Download error: {e}", exc_info=True)
        update_status(f"Download error: {str(e)[:40]}")
        return None


def _download_with_requests_no_verify(
    download_url: str,
    installer_path: Path,
    progress_callback: Optional[Callable[[int, int], None]],
    update_status: Callable[[str], None]
) -> Optional[Path]:
    """Fallback download without SSL verification."""
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
        update_status("Retrying download...")
        
        response = requests.get(
            download_url,
            stream=True,
            timeout=(30, 300),
            headers={
                'User-Agent': 'Lumina-Updater/2.0',
                'Accept': 'application/octet-stream'
            },
            allow_redirects=True,
            verify=False  # Disable SSL verification as fallback
        )
        response.raise_for_status()
        
        total_size = int(response.headers.get('content-length', 0))
        downloaded = 0
        chunk_size = 131072
        
        if progress_callback:
            progress_callback(0, total_size if total_size > 0 else 1)
        
        with open(installer_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total_size if total_size > 0 else downloaded)
        
        if installer_path.exists() and installer_path.stat().st_size > 0:
            logger.info(f"Fallback download complete: {installer_path}")
            update_status("Download complete!")
            return installer_path
        return None
        
    except Exception as e:
        logger.error(f"Fallback download also failed: {e}")
        update_status("Download failed")
        return None


def _download_with_urllib(
    download_url: str,
    installer_path: Path,
    progress_callback: Optional[Callable[[int, int], None]],
    update_status: Callable[[str], None]
) -> Optional[Path]:
    """Fallback download using urllib (when requests not available)."""
    import urllib.request
    import urllib.error
    
    try:
        update_status("Establishing connection...")
        
        # Create SSL context with certifi certificates
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        
        request = urllib.request.Request(
            download_url,
            headers={
                'User-Agent': 'Lumina-Updater/2.0',
                'Accept': 'application/octet-stream'
            }
        )
        
        with urllib.request.urlopen(request, timeout=120, context=ssl_context) as response:
            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0
            chunk_size = 131072
            
            if total_size > 0:
                update_status(f"Downloading... 0 / {total_size / (1024*1024):.1f} MB")
                logger.info(f"Download size: {total_size / (1024*1024):.2f} MB")
            
            if progress_callback:
                progress_callback(0, total_size if total_size > 0 else 1)
            
            with open(installer_path, 'wb') as f:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    
                    if progress_callback:
                        progress_callback(downloaded, total_size if total_size > 0 else downloaded)
        
        if installer_path.exists() and installer_path.stat().st_size > 0:
            logger.info(f"Download complete: {installer_path}")
            update_status("Download complete!")
            return installer_path
        else:
            logger.error("Downloaded file is empty or missing")
            return None
            
    except urllib.error.HTTPError as e:
        logger.error(f"HTTP Error: {e.code} {e.reason}")
        update_status(f"Server error: {e.code}")
        return None
    except urllib.error.URLError as e:
        logger.error(f"URL Error: {e.reason}")
        update_status("Connection failed")
        return None
    except Exception as e:
        logger.error(f"urllib download failed: {e}", exc_info=True)
        return None


def _install_mac_update(dmg_path: Path) -> bool:
    """
    Install update from DMG on macOS.
    
    1. Mount the DMG
    2. Find the .app inside
    3. Copy it to /Applications (replacing old version)
    4. Unmount DMG
    5. Relaunch the new app
    6. Quit current app
    
    Args:
        dmg_path: Path to the downloaded .dmg file
        
    Returns:
        True if update was applied successfully
    """
    import re
    
    mount_point = None
    
    try:
        logger.info(f"[MAC UPDATE] Starting macOS update from: {dmg_path}")
        
        # Step 1: Mount the DMG
        logger.info("[MAC UPDATE] Mounting DMG...")
        mount_result = subprocess.run(
            ['hdiutil', 'attach', str(dmg_path), '-nobrowse', '-plist'],
            capture_output=True,
            text=True,
            timeout=60
        )
        
        if mount_result.returncode != 0:
            logger.error(f"[MAC UPDATE] Failed to mount DMG: {mount_result.stderr}")
            return False
        
        # Parse plist output to find mount point
        # Look for the mount point in the output
        import plistlib
        try:
            plist_data = plistlib.loads(mount_result.stdout.encode())
            for entity in plist_data.get('system-entities', []):
                if 'mount-point' in entity:
                    mount_point = entity['mount-point']
                    break
        except Exception as e:
            logger.warning(f"[MAC UPDATE] Could not parse plist, trying regex: {e}")
            # Fallback: use regex to find mount point
            match = re.search(r'/Volumes/[^\s]+', mount_result.stdout)
            if match:
                mount_point = match.group(0)
        
        if not mount_point:
            logger.error("[MAC UPDATE] Could not find mount point")
            return False
        
        logger.info(f"[MAC UPDATE] DMG mounted at: {mount_point}")
        
        # Step 2: Find the .app inside the mounted volume
        mount_path = Path(mount_point)
        app_files = list(mount_path.glob('*.app'))
        
        if not app_files:
            logger.error(f"[MAC UPDATE] No .app found in mounted DMG at {mount_point}")
            _unmount_dmg(mount_point)
            return False
        
        source_app = app_files[0]  # Use first .app found (should be Lumina.app)
        logger.info(f"[MAC UPDATE] Found app: {source_app.name}")
        
        # Step 3: Determine destination (usually /Applications)
        dest_app = Path('/Applications') / source_app.name
        
        # Step 4: Remove old version if exists
        if dest_app.exists():
            logger.info(f"[MAC UPDATE] Removing old version: {dest_app}")
            try:
                shutil.rmtree(dest_app)
            except PermissionError:
                logger.error("[MAC UPDATE] Permission denied removing old app. May need admin privileges.")
                _unmount_dmg(mount_point)
                return False
        
        # Step 5: Copy new app to /Applications
        logger.info(f"[MAC UPDATE] Copying {source_app.name} to /Applications...")
        try:
            shutil.copytree(source_app, dest_app, symlinks=True)
        except PermissionError:
            logger.error("[MAC UPDATE] Permission denied copying to /Applications")
            _unmount_dmg(mount_point)
            return False
        
        logger.info("[MAC UPDATE] App copied successfully")
        
        # Step 6: Unmount DMG
        _unmount_dmg(mount_point)
        
        # Step 7: Create a script to relaunch the app after current app quits
        relaunch_script = dmg_path.parent / "relaunch.sh"
        script_content = f'''#!/bin/bash
sleep 2
open "{dest_app}"
rm -f "$0"
'''
        with open(relaunch_script, 'w') as f:
            f.write(script_content)
        os.chmod(relaunch_script, 0o755)
        
        # Launch relaunch script in background
        subprocess.Popen(
            ['/bin/bash', str(relaunch_script)],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        
        logger.info("[MAC UPDATE] Update complete - quitting current app to relaunch new version")
        
        # Step 8: Quit current app
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app:
            app.quit()
        
        return True
        
    except subprocess.TimeoutExpired:
        logger.error("[MAC UPDATE] Operation timed out")
        if mount_point:
            _unmount_dmg(mount_point)
        return False
    except Exception as e:
        logger.error(f"[MAC UPDATE] Update failed: {e}", exc_info=True)
        if mount_point:
            _unmount_dmg(mount_point)
        return False


def _unmount_dmg(mount_point: str) -> None:
    """Unmount a DMG volume."""
    try:
        logger.info(f"[MAC UPDATE] Unmounting: {mount_point}")
        subprocess.run(
            ['hdiutil', 'detach', mount_point, '-quiet'],
            capture_output=True,
            timeout=30
        )
    except Exception as e:
        logger.warning(f"[MAC UPDATE] Could not unmount DMG: {e}")


def run_installer_and_exit(installer_path: Path) -> bool:
    """
    Run the installer and exit the current app.
    
    The installer will handle updating the app files.
    
    Args:
        installer_path: Path to the downloaded installer
        
    Returns:
        True if installer was launched successfully
    """
    try:
        if not installer_path.exists():
            logger.error(f"Installer not found: {installer_path}")
            return False
        
        logger.info(f"Launching installer: {installer_path}")
        
        if sys.platform == 'win32':
            # Create a VBS script that:
            # 1. Waits for the app to close
            # 2. Runs installer and WAITS for it to complete
            # 3. Inno Setup will auto-launch the app (skipifnotsilent flag)
            # 4. Fallback: launch app if not already running
            vbs_script = installer_path.parent / "run_update.vbs"
            
            # Escape backslashes for VBS string
            installer_str = str(installer_path).replace("\\", "\\\\")
            
            vbs_content = f'''
On Error Resume Next
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Wait for the old app to fully close
WScript.Sleep 3000

' Run the installer with /SILENT flag
' The installer will auto-launch the app via [Run] section with skipifnotsilent flag
installerPath = "{installer_str}"
returnCode = WshShell.Run(Chr(34) & installerPath & Chr(34) & " /SILENT", 1, True)

' Wait for Windows to finish any cleanup
WScript.Sleep 2000

' Clean up this script
fso.DeleteFile WScript.ScriptFullName
'''
            with open(vbs_script, 'w') as f:
                f.write(vbs_content)
            
            # Launch the VBS script
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            subprocess.Popen(
                ['wscript', str(vbs_script)],
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            
            logger.info("Update script launched - app will close now")
            
            # Exit the application
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app:
                logger.info("Closing application for update...")
                app.quit()
        elif sys.platform == 'darwin':
            # macOS: Mount DMG, copy app to /Applications, unmount, and relaunch
            return _install_mac_update(installer_path)
        else:
            # Other platforms: just open the installer
            subprocess.Popen(
                [str(installer_path)],
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        
        logger.info("Installer launched successfully")
        return True
        
    except Exception as e:
        logger.error(f"Failed to launch installer: {e}", exc_info=True)
        return False


def apply_update_and_restart(installer_path: Path) -> bool:
    """
    Apply update by running the installer and closing the app.
    
    Args:
        installer_path: Path to downloaded installer
        
    Returns:
        True if update process started successfully
    """
    return run_installer_and_exit(installer_path)


def cleanup_update_files():
    """Clean up any leftover update files."""
    try:
        update_dir = get_update_dir()
        if update_dir.exists():
            shutil.rmtree(update_dir)
            logger.info("Cleaned up update files")
    except Exception as e:
        logger.debug(f"Could not clean up update files: {e}")
