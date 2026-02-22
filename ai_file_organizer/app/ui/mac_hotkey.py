"""
macOS implementations for hotkey and window management functions.

This module provides macOS-native functionality for:
- Global hotkey registration (using pynput)
- Window/application management (using PyObjC/AppKit)
- Focus restoration
"""

import sys
import logging
import subprocess
from typing import Optional, Tuple, Callable, Any, List, Dict

logger = logging.getLogger(__name__)

# Track registered hotkeys for cleanup
_registered_hotkeys: List[Any] = []
_hotkey_listeners: List[Any] = []

# Cache for the last active application before our popup
_last_active_app_info: Dict[str, Any] = {}


def _check_accessibility_permission() -> bool:
    """Check if the app has Accessibility permission (required for some features)."""
    try:
        from ApplicationServices import AXIsProcessTrusted
        return AXIsProcessTrusted()
    except ImportError:
        logger.warning("ApplicationServices not available - cannot check accessibility permission")
        return False
    except Exception as e:
        logger.warning(f"Error checking accessibility permission: {e}")
        return False


def _request_accessibility_permission() -> None:
    """Open System Preferences to the Accessibility pane."""
    try:
        subprocess.run([
            'open',
            'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility'
        ], check=False)
    except Exception as e:
        logger.error(f"Failed to open System Preferences: {e}")


def check_accessibility_permission() -> bool:
    """
    Public function to check if Accessibility permission is granted.
    
    Returns:
        True if permission is granted, False otherwise
    """
    return _check_accessibility_permission()


def request_accessibility_permission() -> None:
    """
    Public function to open System Preferences to grant Accessibility permission.
    """
    _request_accessibility_permission()


# ============================================================================
# PHASE 1: Global Hotkey Registration
# ============================================================================

def _parse_hotkey_for_pynput(sequence: str) -> Optional[set]:
    """
    Parse a hotkey sequence string into pynput format.
    
    Examples:
        'ctrl+alt+h' -> {Key.ctrl, Key.alt, KeyCode.from_char('h')}
        'cmd+shift+space' -> {Key.cmd, Key.shift, Key.space}
    """
    try:
        from pynput.keyboard import Key, KeyCode
    except ImportError:
        logger.error("pynput not installed - cannot parse hotkey")
        return None
    
    seq = (sequence or '').lower().replace(' ', '')
    parts = seq.split('+')
    
    hotkey_set = set()
    
    # Map of modifier names to pynput Key objects
    modifier_map = {
        'ctrl': Key.ctrl,
        'control': Key.ctrl,
        'alt': Key.alt,
        'option': Key.alt,
        'shift': Key.shift,
        'cmd': Key.cmd,
        'command': Key.cmd,
        'meta': Key.cmd,
    }
    
    # Map of special key names to pynput Key objects
    special_key_map = {
        'space': Key.space,
        'enter': Key.enter,
        'return': Key.enter,
        'tab': Key.tab,
        'escape': Key.esc,
        'esc': Key.esc,
        'backspace': Key.backspace,
        'delete': Key.delete,
        'up': Key.up,
        'down': Key.down,
        'left': Key.left,
        'right': Key.right,
        'home': Key.home,
        'end': Key.end,
        'pageup': Key.page_up,
        'pagedown': Key.page_down,
    }
    
    # Add function keys
    for i in range(1, 21):
        special_key_map[f'f{i}'] = getattr(Key, f'f{i}', None)
    
    for part in parts:
        if not part:
            continue
            
        # Check if it's a modifier
        if part in modifier_map:
            hotkey_set.add(modifier_map[part])
        # Check if it's a special key
        elif part in special_key_map and special_key_map[part] is not None:
            hotkey_set.add(special_key_map[part])
        # Single character key
        elif len(part) == 1:
            hotkey_set.add(KeyCode.from_char(part))
        else:
            logger.warning(f"Unknown key in hotkey sequence: {part}")
    
    return hotkey_set if hotkey_set else None


def register_global_hotkey(
    parent: Any,
    sequence: str,
    on_activated: Callable[[], None]
) -> Optional[Tuple[int, Any, int]]:
    """
    Register a global hotkey on macOS using pynput.
    
    Args:
        parent: Parent widget (unused on macOS, kept for API compatibility)
        sequence: Hotkey sequence string (e.g., 'ctrl+alt+h', 'cmd+shift+space')
        on_activated: Callback function to call when hotkey is pressed
    
    Returns:
        Tuple of (hotkey_id, listener, 0) on success, None on failure
    """
    global _hotkey_listeners
    
    try:
        from pynput import keyboard
    except ImportError:
        logger.error("pynput not installed. Install with: pip install pynput")
        logger.error("Note: On macOS, you may need to grant Accessibility permissions")
        return None
    
    # Parse the hotkey sequence
    hotkey_set = _parse_hotkey_for_pynput(sequence)
    if not hotkey_set:
        logger.error(f"Failed to parse hotkey sequence: {sequence}")
        return None
    
    # Convert set to the format pynput expects for GlobalHotKeys
    # pynput GlobalHotKeys expects strings like '<ctrl>+<alt>+h'
    def format_for_global_hotkeys(seq: str) -> str:
        """Convert our format to pynput GlobalHotKeys format."""
        seq = seq.lower().replace(' ', '')
        parts = seq.split('+')
        formatted_parts = []
        
        modifier_format = {
            'ctrl': '<ctrl>',
            'control': '<ctrl>',
            'alt': '<alt>',
            'option': '<alt>',
            'shift': '<shift>',
            'cmd': '<cmd>',
            'command': '<cmd>',
            'meta': '<cmd>',
        }
        
        special_format = {
            'space': '<space>',
            'enter': '<enter>',
            'return': '<enter>',
            'tab': '<tab>',
            'escape': '<esc>',
            'esc': '<esc>',
            'backspace': '<backspace>',
            'delete': '<delete>',
        }
        
        for part in parts:
            if part in modifier_format:
                formatted_parts.append(modifier_format[part])
            elif part in special_format:
                formatted_parts.append(special_format[part])
            elif part.startswith('f') and part[1:].isdigit():
                formatted_parts.append(f'<{part}>')
            elif len(part) == 1:
                formatted_parts.append(part)
            else:
                formatted_parts.append(f'<{part}>')
        
        return '+'.join(formatted_parts)
    
    formatted_sequence = format_for_global_hotkeys(sequence)
    logger.info(f"Registering global hotkey: {sequence} -> {formatted_sequence}")
    
    # Create a wrapper that handles exceptions
    def safe_callback():
        try:
            logger.info(f"Global hotkey activated: {sequence}")
            logger.info(f"Calling on_activated callback...")
            on_activated()
            logger.info(f"on_activated callback completed")
        except Exception as e:
            logger.error(f"Error in hotkey callback: {e}", exc_info=True)
    
    # Use Listener with manual key tracking - more reliable than GlobalHotKeys
    # especially for special keys like space
    try:
        from pynput.keyboard import Key, KeyCode
        
        # Build the set of keys we need to detect
        target_keys = set()
        seq = sequence.lower().replace(' ', '')
        parts = seq.split('+')
        
        for part in parts:
            if part in ('ctrl', 'control'):
                target_keys.add(Key.ctrl)
                target_keys.add(Key.ctrl_l)
                target_keys.add(Key.ctrl_r)
            elif part in ('shift',):
                target_keys.add(Key.shift)
                target_keys.add(Key.shift_l)
                target_keys.add(Key.shift_r)
            elif part in ('alt', 'option'):
                target_keys.add(Key.alt)
                target_keys.add(Key.alt_l)
                target_keys.add(Key.alt_r)
            elif part in ('cmd', 'command', 'meta'):
                target_keys.add(Key.cmd)
                target_keys.add(Key.cmd_l)
                target_keys.add(Key.cmd_r)
            elif part == 'space':
                target_keys.add(Key.space)
            elif part == 'enter' or part == 'return':
                target_keys.add(Key.enter)
            elif part == 'tab':
                target_keys.add(Key.tab)
            elif part == 'escape' or part == 'esc':
                target_keys.add(Key.esc)
            elif len(part) == 1:
                target_keys.add(KeyCode.from_char(part))
        
        logger.info(f"Target keys for hotkey: {target_keys}")
        
        # Track currently pressed keys
        current_keys = set()
        hotkey_triggered = [False]  # Use list to allow modification in nested function
        
        # Define which modifiers we need (simplified check)
        need_ctrl = 'ctrl' in seq or 'control' in seq
        need_shift = 'shift' in seq
        need_alt = 'alt' in seq or 'option' in seq
        need_cmd = 'cmd' in seq or 'command' in seq or 'meta' in seq
        need_space = 'space' in seq
        
        def check_hotkey():
            """Check if the hotkey combination is currently pressed."""
            has_ctrl = any(k in current_keys for k in (Key.ctrl, Key.ctrl_l, Key.ctrl_r))
            has_shift = any(k in current_keys for k in (Key.shift, Key.shift_l, Key.shift_r))
            has_alt = any(k in current_keys for k in (Key.alt, Key.alt_l, Key.alt_r))
            has_cmd = any(k in current_keys for k in (Key.cmd, Key.cmd_l, Key.cmd_r))
            has_space = Key.space in current_keys
            
            # Check all required modifiers
            if need_ctrl and not has_ctrl:
                return False
            if need_shift and not has_shift:
                return False
            if need_alt and not has_alt:
                return False
            if need_cmd and not has_cmd:
                return False
            if need_space and not has_space:
                return False
            
            return True
        
        def on_press(key):
            try:
                current_keys.add(key)
                
                # Check if hotkey is triggered
                if check_hotkey() and not hotkey_triggered[0]:
                    hotkey_triggered[0] = True
                    logger.info(f"Hotkey detected! Current keys: {current_keys}")
                    safe_callback()
            except Exception as e:
                logger.error(f"Error in key press handler: {e}")
        
        def on_release(key):
            try:
                current_keys.discard(key)
                # Reset trigger flag when any key is released
                hotkey_triggered[0] = False
            except Exception:
                pass
        
        listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        listener.start()
        
        hotkey_id = len(_hotkey_listeners)
        _hotkey_listeners.append(listener)
        
        logger.info(f"Successfully registered global hotkey: {sequence} (id={hotkey_id})")
        
        # Check accessibility permission and warn if not granted
        if not _check_accessibility_permission():
            logger.warning("Accessibility permission not granted. Global hotkeys may not work.")
            logger.warning("Go to System Preferences > Security & Privacy > Privacy > Accessibility")
            logger.warning("and add this application to the list.")
        
        return (hotkey_id, listener, 0)
        
    except Exception as e:
        logger.error(f"Failed to register global hotkey '{sequence}': {e}")
        return None


def unregister_global_hotkey(hotkey_id: int, filt: Any) -> None:
    """
    Unregister a global hotkey.
    
    Args:
        hotkey_id: The hotkey ID returned from register_global_hotkey
        filt: The filter/listener object (will be stopped)
    """
    global _hotkey_listeners
    
    try:
        if filt is not None and hasattr(filt, 'stop'):
            filt.stop()
            logger.info(f"Unregistered global hotkey (id={hotkey_id})")
        
        # Remove from our tracking list
        if 0 <= hotkey_id < len(_hotkey_listeners):
            _hotkey_listeners[hotkey_id] = None
            
    except Exception as e:
        logger.error(f"Error unregistering hotkey: {e}")


# ============================================================================
# PHASE 2: Window Management
# ============================================================================

def get_cursor_pos() -> tuple:
    """
    Get current mouse cursor position.
    
    Returns:
        Tuple of (x, y) coordinates, or empty tuple on failure
    """
    try:
        from AppKit import NSEvent
        
        # Get mouse location in screen coordinates
        location = NSEvent.mouseLocation()
        
        # NSEvent.mouseLocation() returns coordinates with origin at bottom-left
        # We need to convert to top-left origin for consistency with Windows
        from AppKit import NSScreen
        main_screen = NSScreen.mainScreen()
        if main_screen:
            screen_height = main_screen.frame().size.height
            # Convert from bottom-left to top-left coordinate system
            x = int(location.x)
            y = int(screen_height - location.y)
            return (x, y)
        
        return (int(location.x), int(location.y))
        
    except ImportError:
        logger.warning("AppKit not available - cannot get cursor position")
        return ()
    except Exception as e:
        logger.error(f"Error getting cursor position: {e}")
        return ()


def set_cursor_pos(x: int, y: int) -> bool:
    """
    Set mouse cursor position.
    
    Args:
        x: X coordinate
        y: Y coordinate
    
    Returns:
        True on success, False on failure
    """
    try:
        from Quartz import CGWarpMouseCursorPosition, CGPoint
        
        # Convert from top-left to bottom-left coordinate system
        from AppKit import NSScreen
        main_screen = NSScreen.mainScreen()
        if main_screen:
            screen_height = main_screen.frame().size.height
            y = int(screen_height - y)
        
        CGWarpMouseCursorPosition(CGPoint(x, y))
        return True
        
    except ImportError:
        logger.warning("Quartz not available - cannot set cursor position")
        return False
    except Exception as e:
        logger.error(f"Error setting cursor position: {e}")
        return False


def get_foreground_hwnd() -> int:
    """
    Get the frontmost application's process ID.
    
    On macOS, we return the PID of the frontmost application,
    which serves a similar purpose to Windows HWND.
    
    Returns:
        Process ID of frontmost app, or 0 on failure
    """
    global _last_active_app_info
    
    try:
        from AppKit import NSWorkspace
        
        workspace = NSWorkspace.sharedWorkspace()
        frontmost_app = workspace.frontmostApplication()
        
        if frontmost_app:
            pid = frontmost_app.processIdentifier()
            
            # Cache app info for later restoration
            _last_active_app_info = {
                'pid': pid,
                'name': frontmost_app.localizedName(),
                'bundle_id': frontmost_app.bundleIdentifier(),
                'app': frontmost_app,
            }
            
            logger.debug(f"Frontmost app: {_last_active_app_info['name']} (PID: {pid})")
            return pid
        
        return 0
        
    except ImportError:
        logger.warning("AppKit not available - cannot get foreground window")
        return 0
    except Exception as e:
        logger.error(f"Error getting foreground window: {e}")
        return 0


def set_foreground_hwnd(hwnd: int) -> bool:
    """
    Bring an application to the foreground by its PID.
    
    Args:
        hwnd: Process ID of the application
    
    Returns:
        True on success, False on failure
    """
    try:
        from AppKit import NSRunningApplication, NSApplicationActivateIgnoringOtherApps
        
        # Find the app by PID
        apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_('')
        
        # Get all running apps and find by PID
        from AppKit import NSWorkspace
        workspace = NSWorkspace.sharedWorkspace()
        running_apps = workspace.runningApplications()
        
        for app in running_apps:
            if app.processIdentifier() == hwnd:
                # Activate the app
                success = app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
                if success:
                    logger.debug(f"Activated app: {app.localizedName()} (PID: {hwnd})")
                return success
        
        logger.warning(f"Could not find app with PID: {hwnd}")
        return False
        
    except ImportError:
        logger.warning("AppKit not available - cannot set foreground window")
        return False
    except Exception as e:
        logger.error(f"Error setting foreground window: {e}")
        return False


def set_foreground_hwnd_robust(hwnd: int) -> bool:
    """
    Robust foreground window setting with multiple attempts.
    
    Args:
        hwnd: Process ID of the application
    
    Returns:
        True on success, False on failure
    """
    import time
    
    # Try multiple times with small delays
    for attempt in range(3):
        if set_foreground_hwnd(hwnd):
            return True
        time.sleep(0.1)
    
    # Try using AppleScript as fallback
    try:
        # Get the app name from our cache
        if _last_active_app_info.get('pid') == hwnd:
            app_name = _last_active_app_info.get('name', '')
            if app_name:
                script = f'tell application "{app_name}" to activate'
                subprocess.run(['osascript', '-e', script], check=False, capture_output=True)
                logger.debug(f"Activated app via AppleScript: {app_name}")
                return True
    except Exception as e:
        logger.error(f"AppleScript activation failed: {e}")
    
    return False


def get_window_rect(hwnd: int) -> tuple:
    """
    Get window rectangle for an application.
    
    Note: On macOS, this returns the bounds of the app's main window.
    
    Args:
        hwnd: Process ID of the application
    
    Returns:
        Tuple of (left, top, right, bottom), or empty tuple on failure
    """
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGWindowListOptionOnScreenOnly,
            kCGNullWindowID,
            kCGWindowOwnerPID,
            kCGWindowBounds,
        )
        
        # Get all on-screen windows
        window_list = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly,
            kCGNullWindowID
        )
        
        if not window_list:
            return ()
        
        # Find windows belonging to this PID
        for window in window_list:
            if window.get(kCGWindowOwnerPID) == hwnd:
                bounds = window.get(kCGWindowBounds)
                if bounds:
                    x = int(bounds.get('X', 0))
                    y = int(bounds.get('Y', 0))
                    width = int(bounds.get('Width', 0))
                    height = int(bounds.get('Height', 0))
                    return (x, y, x + width, y + height)
        
        return ()
        
    except ImportError:
        logger.warning("Quartz not available - cannot get window rect")
        return ()
    except Exception as e:
        logger.error(f"Error getting window rect: {e}")
        return ()


def is_file_dialog(hwnd: int) -> bool:
    """
    Check if the frontmost window is a file dialog.
    
    This is a heuristic check on macOS since file dialogs are
    integrated into applications.
    
    Args:
        hwnd: Process ID of the application
    
    Returns:
        True if likely a file dialog, False otherwise
    """
    try:
        # Use Accessibility API to check for file dialog characteristics
        from ApplicationServices import (
            AXUIElementCreateApplication,
            AXUIElementCopyAttributeValue,
            kAXFocusedWindowAttribute,
            kAXRoleAttribute,
            kAXTitleAttribute,
            kAXChildrenAttribute,
            kAXSubroleAttribute,
        )
        
        app_ref = AXUIElementCreateApplication(hwnd)
        if not app_ref:
            logger.debug(f"[FILE_DIALOG] Could not create AX element for PID {hwnd}")
            return False
        
        # Get the focused window
        err, focused_window = AXUIElementCopyAttributeValue(
            app_ref, kAXFocusedWindowAttribute, None
        )
        
        if err != 0 or not focused_window:
            logger.debug(f"[FILE_DIALOG] Could not get focused window (err={err})")
            return False
        
        # Get window title
        window_title = ""
        err, title = AXUIElementCopyAttributeValue(
            focused_window, kAXTitleAttribute, None
        )
        
        if err == 0 and title:
            window_title = str(title)
            title_lower = window_title.lower()
            # Common file dialog titles - must be at start or be the whole title
            # to avoid false positives like "Open Source" or "Save the Date"
            dialog_title_patterns = [
                'open', 'save', 'save as', 'choose', 'select', 'export', 'import', 
                'browse', 'upload', 'open file', 'save file', 'choose file',
                'select file', 'open folder', 'choose folder', 'select folder'
            ]
            # Check if title starts with or equals a dialog pattern
            for pattern in dialog_title_patterns:
                if title_lower == pattern or title_lower.startswith(pattern + ' ') or title_lower.startswith(pattern + ':'):
                    logger.info(f"[FILE_DIALOG] Detected by title: '{window_title}'")
                    return True
        
        # Check window subrole (file dialogs often have specific subroles)
        err, subrole = AXUIElementCopyAttributeValue(
            focused_window, kAXSubroleAttribute, None
        )
        if err == 0 and subrole:
            subrole_str = str(subrole)
            # AXDialog, AXStandardWindow are common for file dialogs
            if 'Dialog' in subrole_str or 'Sheet' in subrole_str:
                logger.info(f"[FILE_DIALOG] Detected by subrole: {subrole_str}")
                return True
        
        # Check for common file dialog UI elements
        err, children = AXUIElementCopyAttributeValue(
            focused_window, kAXChildrenAttribute, None
        )
        
        if err == 0 and children:
            # Look for file browser elements (outline, browser, etc.)
            file_dialog_indicators = 0
            for child in children:
                err, role = AXUIElementCopyAttributeValue(child, kAXRoleAttribute, None)
                if err == 0 and role:
                    role_str = str(role)
                    # File browser elements
                    if role_str in ['AXBrowser', 'AXOutline', 'AXTable']:
                        file_dialog_indicators += 2
                        logger.debug(f"[FILE_DIALOG] Found browser element: {role_str}")
                    # Text fields (filename input)
                    elif role_str == 'AXTextField':
                        file_dialog_indicators += 1
                    # Buttons (Open, Save, Cancel)
                    elif role_str == 'AXButton':
                        file_dialog_indicators += 0.5
                    # Popup buttons (file type selector)
                    elif role_str == 'AXPopUpButton':
                        file_dialog_indicators += 1
            
            # If we have enough indicators, it's likely a file dialog
            if file_dialog_indicators >= 3:
                logger.info(f"[FILE_DIALOG] Detected by UI elements (score={file_dialog_indicators})")
                return True
        
        logger.debug(f"[FILE_DIALOG] Not detected for window '{window_title}'")
        return False
        
    except ImportError as e:
        logger.warning(f"ApplicationServices not available - cannot detect file dialog: {e}")
        return False
    except Exception as e:
        logger.debug(f"Error checking for file dialog: {e}")
        return False


def get_window_title(hwnd: int) -> str:
    """
    Get the title of the frontmost window for an application.
    
    Args:
        hwnd: Process ID of the application
    
    Returns:
        Window title string, or empty string on failure
    """
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGWindowListOptionOnScreenOnly,
            kCGNullWindowID,
            kCGWindowOwnerPID,
            kCGWindowName,
            kCGWindowLayer,
        )
        
        window_list = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly,
            kCGNullWindowID
        )
        
        if not window_list:
            return ""
        
        # Find the topmost window for this PID (lowest layer number)
        best_window = None
        best_layer = float('inf')
        
        for window in window_list:
            if window.get(kCGWindowOwnerPID) == hwnd:
                layer = window.get(kCGWindowLayer, float('inf'))
                if layer < best_layer:
                    best_layer = layer
                    best_window = window
        
        if best_window:
            return best_window.get(kCGWindowName, '') or ''
        
        return ""
        
    except ImportError:
        logger.warning("Quartz not available - cannot get window title")
        return ""
    except Exception as e:
        logger.error(f"Error getting window title: {e}")
        return ""


def get_window_class(hwnd: int) -> str:
    """
    Get the "class" of a window (on macOS, this returns the bundle identifier).
    
    Args:
        hwnd: Process ID of the application
    
    Returns:
        Bundle identifier string, or empty string on failure
    """
    try:
        from AppKit import NSWorkspace
        
        workspace = NSWorkspace.sharedWorkspace()
        running_apps = workspace.runningApplications()
        
        for app in running_apps:
            if app.processIdentifier() == hwnd:
                return app.bundleIdentifier() or ''
        
        return ""
        
    except ImportError:
        logger.warning("AppKit not available - cannot get window class")
        return ""
    except Exception as e:
        logger.error(f"Error getting window class: {e}")
        return ""


def window_still_exists(hwnd: int) -> bool:
    """
    Check if an application with the given PID is still running.
    
    Args:
        hwnd: Process ID of the application
    
    Returns:
        True if running, False otherwise
    """
    try:
        from AppKit import NSWorkspace
        
        workspace = NSWorkspace.sharedWorkspace()
        running_apps = workspace.runningApplications()
        
        for app in running_apps:
            if app.processIdentifier() == hwnd:
                return True
        
        return False
        
    except ImportError:
        logger.warning("AppKit not available - cannot check if window exists")
        return False
    except Exception as e:
        logger.error(f"Error checking if window exists: {e}")
        return False


def is_window_visible(hwnd: int) -> bool:
    """
    Check if an application has visible windows.
    
    Args:
        hwnd: Process ID of the application
    
    Returns:
        True if visible, False otherwise
    """
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGWindowListOptionOnScreenOnly,
            kCGNullWindowID,
            kCGWindowOwnerPID,
        )
        
        window_list = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly,
            kCGNullWindowID
        )
        
        if not window_list:
            return False
        
        for window in window_list:
            if window.get(kCGWindowOwnerPID) == hwnd:
                return True
        
        return False
        
    except ImportError:
        logger.warning("Quartz not available - cannot check window visibility")
        return False
    except Exception as e:
        logger.error(f"Error checking window visibility: {e}")
        return False


def restore_window_focus_method1(hwnd: int) -> bool:
    """Restore window focus using NSRunningApplication.activate."""
    return set_foreground_hwnd(hwnd)


def restore_window_focus_method2(hwnd: int) -> bool:
    """Restore window focus using AppleScript."""
    try:
        if _last_active_app_info.get('pid') == hwnd:
            app_name = _last_active_app_info.get('name', '')
            if app_name:
                script = f'tell application "{app_name}" to activate'
                result = subprocess.run(
                    ['osascript', '-e', script],
                    check=False,
                    capture_output=True
                )
                return result.returncode == 0
        return False
    except Exception as e:
        logger.error(f"AppleScript focus restoration failed: {e}")
        return False


def restore_window_focus_method3(hwnd: int) -> bool:
    """Restore window focus using bundle identifier."""
    try:
        from AppKit import NSWorkspace, NSRunningApplication, NSApplicationActivateIgnoringOtherApps
        
        if _last_active_app_info.get('pid') == hwnd:
            bundle_id = _last_active_app_info.get('bundle_id', '')
            if bundle_id:
                apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)
                if apps and len(apps) > 0:
                    return apps[0].activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
        return False
    except Exception as e:
        logger.error(f"Bundle ID focus restoration failed: {e}")
        return False


def restore_focus_by_mouse_click(cursor_pos: tuple, window_rect: tuple) -> bool:
    """
    Restore focus by simulating a mouse click.
    
    Args:
        cursor_pos: Original cursor position (x, y)
        window_rect: Window rectangle (left, top, right, bottom)
    
    Returns:
        True on success, False on failure
    """
    try:
        if not cursor_pos or len(cursor_pos) < 2:
            return False
        
        return click_at_position(cursor_pos[0], cursor_pos[1])
        
    except Exception as e:
        logger.error(f"Error restoring focus by mouse click: {e}")
        return False


def restore_dialog_focus_hybrid(
    hwnd: int,
    cursor_pos: tuple,
    window_rect: tuple,
    delay_ms: int = 500
) -> Tuple[bool, str]:
    """
    Hybrid approach to restore focus to a file dialog.
    
    Tries multiple methods in sequence.
    
    Args:
        hwnd: Process ID of the target application
        cursor_pos: Original cursor position
        window_rect: Window rectangle
        delay_ms: Delay in milliseconds between attempts
    
    Returns:
        Tuple of (success: bool, method_used: str)
    """
    import time
    
    delay_sec = delay_ms / 1000.0
    
    # Method 1: Direct activation
    if restore_window_focus_method1(hwnd):
        time.sleep(delay_sec)
        return (True, "direct_activation")
    
    time.sleep(0.1)
    
    # Method 2: AppleScript
    if restore_window_focus_method2(hwnd):
        time.sleep(delay_sec)
        return (True, "applescript")
    
    time.sleep(0.1)
    
    # Method 3: Bundle ID
    if restore_window_focus_method3(hwnd):
        time.sleep(delay_sec)
        return (True, "bundle_id")
    
    time.sleep(0.1)
    
    # Method 4: Mouse click
    if cursor_pos and restore_focus_by_mouse_click(cursor_pos, window_rect):
        time.sleep(delay_sec)
        return (True, "mouse_click")
    
    return (False, "all_methods_failed")


def click_at_position(x: int, y: int) -> bool:
    """
    Perform a mouse click at the specified position.
    
    Args:
        x: X coordinate
        y: Y coordinate
    
    Returns:
        True on success, False on failure
    """
    try:
        from Quartz import (
            CGEventCreateMouseEvent,
            CGEventPost,
            kCGEventLeftMouseDown,
            kCGEventLeftMouseUp,
            kCGMouseButtonLeft,
            kCGHIDEventTap,
        )
        from Quartz import CGPoint
        
        # Convert coordinates if needed (top-left to bottom-left)
        from AppKit import NSScreen
        main_screen = NSScreen.mainScreen()
        if main_screen:
            screen_height = main_screen.frame().size.height
            y = int(screen_height - y)
        
        point = CGPoint(x, y)
        
        # Create and post mouse down event
        mouse_down = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, point, kCGMouseButtonLeft)
        CGEventPost(kCGHIDEventTap, mouse_down)
        
        # Small delay between down and up
        import time
        time.sleep(0.05)
        
        # Create and post mouse up event
        mouse_up = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, point, kCGMouseButtonLeft)
        CGEventPost(kCGHIDEventTap, mouse_up)
        
        return True
        
    except ImportError:
        logger.warning("Quartz not available - cannot click at position")
        return False
    except Exception as e:
        logger.error(f"Error clicking at position: {e}")
        return False


def enumerate_windows_detailed() -> list:
    """
    Get detailed information about all visible windows.
    
    Returns:
        List of dictionaries with window information
    """
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGWindowListOptionOnScreenOnly,
            kCGNullWindowID,
            kCGWindowOwnerPID,
            kCGWindowOwnerName,
            kCGWindowName,
            kCGWindowBounds,
            kCGWindowLayer,
        )
        
        window_list = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly,
            kCGNullWindowID
        )
        
        if not window_list:
            return []
        
        result = []
        for window in window_list:
            bounds = window.get(kCGWindowBounds, {})
            result.append({
                'pid': window.get(kCGWindowOwnerPID, 0),
                'owner_name': window.get(kCGWindowOwnerName, ''),
                'title': window.get(kCGWindowName, ''),
                'layer': window.get(kCGWindowLayer, 0),
                'x': bounds.get('X', 0),
                'y': bounds.get('Y', 0),
                'width': bounds.get('Width', 0),
                'height': bounds.get('Height', 0),
            })
        
        return result
        
    except ImportError:
        logger.warning("Quartz not available - cannot enumerate windows")
        return []
    except Exception as e:
        logger.error(f"Error enumerating windows: {e}")
        return []


def log_system_state(logger_obj: Any, prefix: str = "[QS]") -> None:
    """
    Log current system state for debugging.
    
    Args:
        logger_obj: Logger instance to use
        prefix: Prefix for log messages
    """
    try:
        from AppKit import NSWorkspace
        
        workspace = NSWorkspace.sharedWorkspace()
        frontmost = workspace.frontmostApplication()
        
        if frontmost:
            logger_obj.info(f"{prefix} System State:")
            logger_obj.info(f"{prefix}   Frontmost app: {frontmost.localizedName()}")
            logger_obj.info(f"{prefix}   Bundle ID: {frontmost.bundleIdentifier()}")
            logger_obj.info(f"{prefix}   PID: {frontmost.processIdentifier()}")
        
        # Log cursor position
        cursor = get_cursor_pos()
        if cursor:
            logger_obj.info(f"{prefix}   Cursor position: {cursor}")
            
    except Exception as e:
        logger_obj.error(f"{prefix} Error logging system state: {e}")


def log_window_hierarchy(hwnd: int, logger_obj: Any, prefix: str = "[QS]", max_depth: int = 3) -> None:
    """
    Log window hierarchy for debugging.
    
    Args:
        hwnd: Process ID
        logger_obj: Logger instance
        prefix: Prefix for log messages
        max_depth: Maximum depth to traverse
    """
    try:
        windows = enumerate_windows_detailed()
        app_windows = [w for w in windows if w['pid'] == hwnd]
        
        logger_obj.info(f"{prefix} Window hierarchy for PID {hwnd}:")
        for i, win in enumerate(app_windows[:max_depth]):
            logger_obj.info(f"{prefix}   [{i}] {win['title']} ({win['width']}x{win['height']})")
            
    except Exception as e:
        logger_obj.error(f"{prefix} Error logging window hierarchy: {e}")


def create_autofill_debug_report(
    hwnd: int,
    cursor_pos: tuple,
    window_rect: tuple,
    logger_obj: Any,
    prefix: str = "[QS]"
) -> None:
    """
    Create a comprehensive debug report for autofill troubleshooting.
    
    Args:
        hwnd: Process ID
        cursor_pos: Cursor position
        window_rect: Window rectangle
        logger_obj: Logger instance
        prefix: Prefix for log messages
    """
    try:
        logger_obj.info(f"{prefix} === AUTOFILL DEBUG REPORT ===")
        logger_obj.info(f"{prefix} Target PID: {hwnd}")
        logger_obj.info(f"{prefix} Cursor position: {cursor_pos}")
        logger_obj.info(f"{prefix} Window rect: {window_rect}")
        
        # Check if app still exists
        exists = window_still_exists(hwnd)
        logger_obj.info(f"{prefix} App still running: {exists}")
        
        if exists:
            title = get_window_title(hwnd)
            logger_obj.info(f"{prefix} Window title: {title}")
            
            is_dialog = is_file_dialog(hwnd)
            logger_obj.info(f"{prefix} Is file dialog: {is_dialog}")
        
        # Accessibility permission
        has_accessibility = _check_accessibility_permission()
        logger_obj.info(f"{prefix} Accessibility permission: {has_accessibility}")
        
        logger_obj.info(f"{prefix} === END DEBUG REPORT ===")
        
    except Exception as e:
        logger_obj.error(f"{prefix} Error creating debug report: {e}")


# ============================================================================
# PHASE 3: Autofill Implementation (macOS)
# ============================================================================

def autofill_via_clipboard_paste(path: str) -> bool:
    """
    Autofill a file dialog by copying path to clipboard and pasting.
    
    This is the most reliable cross-application method on macOS.
    
    Args:
        path: The file path to fill
    
    Returns:
        True on success, False on failure
    """
    try:
        from AppKit import NSPasteboard, NSStringPboardType
        
        # Copy path to clipboard
        pasteboard = NSPasteboard.generalPasteboard()
        pasteboard.clearContents()
        pasteboard.setString_forType_(path, NSStringPboardType)
        
        logger.info(f"[QS] Copied path to clipboard: {path}")
        
        # Small delay to ensure clipboard is ready
        import time
        time.sleep(0.1)
        
        # Simulate Cmd+V to paste
        return _simulate_paste_shortcut()
        
    except ImportError:
        logger.warning("AppKit not available - cannot use clipboard autofill")
        return False
    except Exception as e:
        logger.error(f"Error in clipboard autofill: {e}")
        return False


def _simulate_paste_shortcut() -> bool:
    """Simulate Cmd+V keyboard shortcut to paste."""
    try:
        from Quartz import (
            CGEventCreateKeyboardEvent,
            CGEventPost,
            CGEventSetFlags,
            kCGEventFlagMaskCommand,
            kCGHIDEventTap,
        )
        
        # 'v' key code is 9 on macOS
        v_keycode = 9
        
        # Key down with Cmd modifier
        key_down = CGEventCreateKeyboardEvent(None, v_keycode, True)
        CGEventSetFlags(key_down, kCGEventFlagMaskCommand)
        CGEventPost(kCGHIDEventTap, key_down)
        
        import time
        time.sleep(0.05)
        
        # Key up
        key_up = CGEventCreateKeyboardEvent(None, v_keycode, False)
        CGEventPost(kCGHIDEventTap, key_up)
        
        logger.debug("[QS] Simulated Cmd+V paste")
        return True
        
    except ImportError:
        logger.warning("Quartz not available - cannot simulate paste")
        return False
    except Exception as e:
        logger.error(f"Error simulating paste: {e}")
        return False


def autofill_via_applescript(path: str, app_name: str = None) -> bool:
    """
    Autofill using AppleScript to type the path.
    
    This method uses System Events to simulate keystrokes.
    
    Args:
        path: The file path to fill
        app_name: Optional app name to target (uses frontmost if None)
    
    Returns:
        True on success, False on failure
    """
    try:
        # Escape the path for AppleScript
        escaped_path = path.replace('\\', '\\\\').replace('"', '\\"')
        
        if app_name:
            # Target specific app
            script = f'''
            tell application "System Events"
                tell process "{app_name}"
                    keystroke "{escaped_path}"
                end tell
            end tell
            '''
        else:
            # Use frontmost app
            script = f'''
            tell application "System Events"
                keystroke "{escaped_path}"
            end tell
            '''
        
        result = subprocess.run(
            ['osascript', '-e', script],
            check=False,
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            logger.info(f"[QS] AppleScript autofill succeeded")
            return True
        else:
            logger.warning(f"[QS] AppleScript autofill failed: {result.stderr}")
            return False
            
    except Exception as e:
        logger.error(f"Error in AppleScript autofill: {e}")
        return False


def autofill_via_keyboard_simulation(path: str) -> bool:
    """
    Autofill by simulating keyboard input character by character.
    
    This is slower but more compatible with various dialogs.
    
    Args:
        path: The file path to fill
    
    Returns:
        True on success, False on failure
    """
    try:
        from pynput.keyboard import Controller, Key
        
        keyboard = Controller()
        
        # Clear any existing text first (Cmd+A then type)
        keyboard.press(Key.cmd)
        keyboard.press('a')
        keyboard.release('a')
        keyboard.release(Key.cmd)
        
        import time
        time.sleep(0.1)
        
        # Type the path
        keyboard.type(path)
        
        logger.info(f"[QS] Keyboard simulation autofill succeeded")
        return True
        
    except ImportError:
        logger.warning("pynput not available - cannot simulate keyboard")
        return False
    except Exception as e:
        logger.error(f"Error in keyboard simulation autofill: {e}")
        return False


def autofill_via_go_to_folder(path: str) -> bool:
    """
    Use the macOS "Go to Folder" dialog (Cmd+Shift+G) in Finder dialogs.
    
    This is specific to native macOS file dialogs.
    
    Args:
        path: The file path to fill
    
    Returns:
        True on success, False on failure
    """
    try:
        from pynput.keyboard import Controller, Key
        
        keyboard = Controller()
        
        # Open "Go to Folder" dialog with Cmd+Shift+G
        keyboard.press(Key.cmd)
        keyboard.press(Key.shift)
        keyboard.press('g')
        keyboard.release('g')
        keyboard.release(Key.shift)
        keyboard.release(Key.cmd)
        
        import time
        time.sleep(0.3)  # Wait for dialog to appear
        
        # Type the path
        keyboard.type(path)
        
        time.sleep(0.1)
        
        # Press Enter to confirm
        keyboard.press(Key.enter)
        keyboard.release(Key.enter)
        
        logger.info(f"[QS] Go to Folder autofill succeeded")
        return True
        
    except ImportError:
        logger.warning("pynput not available - cannot use Go to Folder")
        return False
    except Exception as e:
        logger.error(f"Error in Go to Folder autofill: {e}")
        return False


def copy_path_to_clipboard(path: str) -> bool:
    """
    Copy a path to the system clipboard.
    
    Args:
        path: The file path to copy
    
    Returns:
        True on success, False on failure
    """
    try:
        from AppKit import NSPasteboard, NSStringPboardType
        
        pasteboard = NSPasteboard.generalPasteboard()
        pasteboard.clearContents()
        pasteboard.setString_forType_(path, NSStringPboardType)
        
        logger.info(f"[QS] Copied to clipboard: {path}")
        return True
        
    except ImportError:
        # Fallback to pbcopy command
        try:
            result = subprocess.run(
                ['pbcopy'],
                input=path.encode('utf-8'),
                check=False
            )
            return result.returncode == 0
        except Exception:
            pass
        return False
    except Exception as e:
        logger.error(f"Error copying to clipboard: {e}")
        return False


def try_macos_autofill_strategies(path: str, hwnd: int = None, app_name: str = None) -> Tuple[bool, str]:
    """
    Try multiple macOS autofill strategies in order of reliability.
    
    Args:
        path: The file path to fill
        hwnd: Optional PID of target app
        app_name: Optional name of target app
    
    Returns:
        Tuple of (success: bool, method_used: str)
    """
    import time
    
    # Get app name from cache if not provided
    if not app_name and hwnd:
        info = get_last_active_app_info()
        if info.get('pid') == hwnd:
            app_name = info.get('name')
    
    # Strategy 1: Clipboard + Paste (most reliable)
    logger.info("[QS] Trying strategy 1: Clipboard + Paste")
    if autofill_via_clipboard_paste(path):
        return (True, "clipboard_paste")
    
    time.sleep(0.2)
    
    # Strategy 2: AppleScript keystroke
    logger.info("[QS] Trying strategy 2: AppleScript keystroke")
    if autofill_via_applescript(path, app_name):
        return (True, "applescript")
    
    time.sleep(0.2)
    
    # Strategy 3: Go to Folder (for native file dialogs)
    logger.info("[QS] Trying strategy 3: Go to Folder (Cmd+Shift+G)")
    if autofill_via_go_to_folder(path):
        return (True, "go_to_folder")
    
    time.sleep(0.2)
    
    # Strategy 4: Direct keyboard simulation
    logger.info("[QS] Trying strategy 4: Keyboard simulation")
    if autofill_via_keyboard_simulation(path):
        return (True, "keyboard_simulation")
    
    # All strategies failed - at least copy to clipboard
    logger.warning("[QS] All autofill strategies failed, copying to clipboard as fallback")
    copy_path_to_clipboard(path)
    
    return (False, "all_failed_clipboard_fallback")


# ============================================================================
# Utility Functions
# ============================================================================

def get_last_active_app_info() -> Dict[str, Any]:
    """Get cached information about the last active application."""
    return _last_active_app_info.copy()


def cleanup_hotkeys() -> None:
    """Clean up all registered hotkeys."""
    global _hotkey_listeners
    
    for listener in _hotkey_listeners:
        if listener is not None and hasattr(listener, 'stop'):
            try:
                listener.stop()
            except Exception:
                pass
    
    _hotkey_listeners.clear()
    logger.info("Cleaned up all registered hotkeys")


def is_macos() -> bool:
    """Check if running on macOS."""
    return sys.platform == 'darwin'


def show_accessibility_permission_dialog() -> None:
    """Show a dialog explaining how to grant Accessibility permissions."""
    try:
        script = '''
        display dialog "This app requires Accessibility permissions to work properly.

Please go to:
System Preferences > Security & Privacy > Privacy > Accessibility

And add this application to the list." buttons {"Open System Preferences", "Later"} default button 1

if button returned of result is "Open System Preferences" then
    tell application "System Preferences"
        activate
        set current pane to pane "com.apple.preference.security"
        reveal anchor "Privacy_Accessibility" of pane "com.apple.preference.security"
    end tell
end if
'''
        subprocess.run(['osascript', '-e', script], check=False)
    except Exception as e:
        logger.error(f"Error showing permission dialog: {e}")
        # Fallback: just open System Preferences
        _request_accessibility_permission()
