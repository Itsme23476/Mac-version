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


# macOS virtual keycodes for the special keys we support in hotkeys.
_MAC_KEYCODES = {
    'space': 49, 'enter': 36, 'return': 36, 'tab': 48, 'escape': 53, 'esc': 53,
    'backspace': 51, 'delete': 117,
    'up': 126, 'down': 125, 'left': 123, 'right': 124,
    'home': 115, 'end': 119, 'pageup': 116, 'pagedown': 121,
    'f1': 122, 'f2': 120, 'f3': 99, 'f4': 118, 'f5': 96, 'f6': 97,
    'f7': 98, 'f8': 100, 'f9': 101, 'f10': 109, 'f11': 103, 'f12': 111,
    'a': 0, 'b': 11, 'c': 8, 'd': 2, 'e': 14, 'f': 3, 'g': 5, 'h': 4,
    'i': 34, 'j': 38, 'k': 40, 'l': 37, 'm': 46, 'n': 45, 'o': 31,
    'p': 35, 'q': 12, 'r': 15, 's': 1, 't': 17, 'u': 32, 'v': 9,
    'w': 13, 'x': 7, 'y': 16, 'z': 6,
    '0': 29, '1': 18, '2': 19, '3': 20, '4': 21, '5': 23,
    '6': 22, '7': 26, '8': 28, '9': 25,
}


def _register_hotkey_carbon(sequence: str, on_activated: Callable[[], None]
                            ) -> Optional[Tuple[int, Any, int]]:
    """Register a TRUE system-wide hotkey via Carbon's RegisterEventHotKey.

    Unlike NSEvent global monitors — which silently receive nothing unless the
    app has Accessibility / Input-Monitoring permission — RegisterEventHotKey is
    an OS-level hotkey that fires regardless of which app is frontmost and needs
    NO permission whatsoever. This is the mechanism Spotlight/Alfred/Raycast use,
    and it's the reliable path on modern macOS (incl. Sequoia/Tahoe).
    """
    import ctypes
    import ctypes.util

    carbon_path = ctypes.util.find_library('Carbon')
    if not carbon_path:
        raise OSError("Carbon framework not found")
    carbon = ctypes.CDLL(carbon_path)

    class _EventTypeSpec(ctypes.Structure):
        _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]

    class _EventHotKeyID(ctypes.Structure):
        _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]

    kEventClassKeyboard = 0x6B657962  # 'keyb'
    kEventHotKeyPressed = 5
    # Carbon event modifier masks (NOT the same as NSEvent masks).
    CARBON_CMD, CARBON_SHIFT, CARBON_OPTION, CARBON_CONTROL = 0x0100, 0x0200, 0x0800, 0x1000

    parts = (sequence or '').lower().replace(' ', '').split('+')
    modifiers = 0
    keycode = None
    for p in parts:
        if not p:
            continue
        if p in ('cmd', 'command', 'meta'):
            modifiers |= CARBON_CMD
        elif p == 'shift':
            modifiers |= CARBON_SHIFT
        elif p in ('alt', 'option'):
            modifiers |= CARBON_OPTION
        elif p in ('ctrl', 'control'):
            modifiers |= CARBON_CONTROL
        elif p in _MAC_KEYCODES:
            keycode = _MAC_KEYCODES[p]
        else:
            logger.warning(f"Carbon hotkey: unknown key part '{p}'")
    if keycode is None:
        raise ValueError(f"hotkey sequence '{sequence}' has no key, only modifiers")

    # Handler callback type: OSStatus (*)(EventHandlerCallRef, EventRef, void*)
    _HandlerProc = ctypes.CFUNCTYPE(
        ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)

    import time
    _last_fire = [0.0]

    def _handler(_next_handler, _event, _user_data):
        try:
            # Coalesce rapid double-fires (key auto-repeat / synthetic injection)
            # so a single press can't trigger the popup twice.
            now = time.monotonic()
            if now - _last_fire[0] < 0.25:
                return 0  # noErr
            _last_fire[0] = now
            logger.info(f"Hotkey activated (Carbon): {sequence}")
            on_activated()
        except Exception as e:
            logger.error(f"Error in Carbon hotkey callback: {e}", exc_info=True)
        return 0  # noErr

    handler_proc = _HandlerProc(_handler)

    # Function signatures.
    carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
    carbon.GetApplicationEventTarget.argtypes = []
    carbon.InstallEventHandler.restype = ctypes.c_int32
    carbon.InstallEventHandler.argtypes = [
        ctypes.c_void_p, _HandlerProc, ctypes.c_uint32,
        ctypes.POINTER(_EventTypeSpec), ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    carbon.RegisterEventHotKey.restype = ctypes.c_int32
    carbon.RegisterEventHotKey.argtypes = [
        ctypes.c_uint32, ctypes.c_uint32, _EventHotKeyID,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p),
    ]
    carbon.UnregisterEventHotKey.restype = ctypes.c_int32
    carbon.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
    carbon.RemoveEventHandler.restype = ctypes.c_int32
    carbon.RemoveEventHandler.argtypes = [ctypes.c_void_p]

    target = carbon.GetApplicationEventTarget()
    if not target:
        raise OSError("GetApplicationEventTarget returned NULL")

    spec = _EventTypeSpec(kEventClassKeyboard, kEventHotKeyPressed)
    handler_ref = ctypes.c_void_p()
    err = carbon.InstallEventHandler(
        target, handler_proc, 1, ctypes.byref(spec), None, ctypes.byref(handler_ref))
    if err != 0:
        raise OSError(f"InstallEventHandler failed (OSStatus={err})")

    hk_id = _EventHotKeyID(0x464C4354, 1)  # signature 'FLCT', id 1
    hotkey_ref = ctypes.c_void_p()
    err = carbon.RegisterEventHotKey(
        keycode, modifiers, hk_id, target, 0, ctypes.byref(hotkey_ref))
    if err != 0:
        try:
            carbon.RemoveEventHandler(handler_ref)
        except Exception:
            pass
        raise OSError(f"RegisterEventHotKey failed (OSStatus={err})")

    # Keep handler_proc/spec alive — if they're GC'd, the native callback dies.
    handle = {
        'kind': 'carbon',
        'carbon': carbon,
        'hotkey_ref': hotkey_ref,
        'handler_ref': handler_ref,
        'handler_proc': handler_proc,
        'spec': spec,
    }
    _hotkey_listeners.append(handle)
    hid = len(_hotkey_listeners) - 1
    logger.info(f"Successfully registered global hotkey via Carbon: {sequence} "
                f"(keycode={keycode}, mods={modifiers}, id={hid})")
    return (hid, handle, 0)


def _register_hotkey_nsevent(sequence: str, on_activated: Callable[[], None]
                             ) -> Optional[Tuple[int, Any, int]]:
    """Register a global hotkey using Cocoa's native NSEvent monitors.

    Runs on the main thread (via the main run loop), unlike pynput's
    CGEventTap which runs on a background thread and crashes on CapsLock.
    """
    from AppKit import NSEvent
    from Foundation import NSRunLoop
    # Modifier mask constants (10.10+). NSEventModifier* values:
    #   Caps=1<<16 Shift=1<<17 Control=1<<18 Option=1<<19 Command=1<<20
    MASK_CAPS    = 1 << 16
    MASK_SHIFT   = 1 << 17
    MASK_CONTROL = 1 << 18
    MASK_OPTION  = 1 << 19
    MASK_COMMAND = 1 << 20
    DEVICE_MASK  = MASK_SHIFT | MASK_CONTROL | MASK_OPTION | MASK_COMMAND
    # NSEventMaskKeyDown = 1 << 10
    NSEventMaskKeyDown = 1 << 10

    parts = (sequence or '').lower().replace(' ', '').split('+')
    need_mask = 0
    target_keycode = None
    for p in parts:
        if not p:
            continue
        if p in ('ctrl', 'control'): need_mask |= MASK_CONTROL
        elif p == 'shift':           need_mask |= MASK_SHIFT
        elif p in ('alt', 'option'): need_mask |= MASK_OPTION
        elif p in ('cmd', 'command', 'meta'): need_mask |= MASK_COMMAND
        elif p in _MAC_KEYCODES:     target_keycode = _MAC_KEYCODES[p]
        else:
            logger.warning(f"NSEvent hotkey: unknown key part '{p}'")
    if target_keycode is None:
        raise ValueError(f"hotkey sequence '{sequence}' has no key, only modifiers")

    def _matches(event) -> bool:
        try:
            mods = int(event.modifierFlags()) & DEVICE_MASK
            return mods == need_mask and int(event.keyCode()) == target_keycode
        except Exception:
            return False

    def _fire():
        try:
            logger.info(f"Hotkey activated (NSEvent): {sequence}")
            on_activated()
        except Exception as e:
            logger.error(f"Error in hotkey callback: {e}", exc_info=True)

    def _global_handler(event):
        if _matches(event):
            _fire()

    def _local_handler(event):
        if _matches(event):
            _fire()
            return None  # swallow when our app is frontmost
        return event

    # Global monitor: fires when another app is frontmost (needs Accessibility).
    # Local monitor: fires when our app is frontmost.
    g_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
        NSEventMaskKeyDown, _global_handler)
    l_monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
        NSEventMaskKeyDown, _local_handler)

    # Keep refs alive — if the Python objects are GC'd, the monitors die silently.
    handle = {'global': g_monitor, 'local': l_monitor, 'kind': 'nsevent'}
    _hotkey_listeners.append(handle)
    hotkey_id = len(_hotkey_listeners) - 1
    logger.info(f"Successfully registered global hotkey via NSEvent: {sequence} (id={hotkey_id})")

    if not _check_accessibility_permission():
        logger.warning("Accessibility permission not granted — global hotkey will only fire when our app is frontmost.")
    return (hotkey_id, handle, 0)


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
    
    # Use Cocoa's native NSEvent global+local monitors instead of pynput.
    # pynput runs its CGEventTap on a background thread, and on macOS Sequoia
    # the path `NSEvent.eventWithCGEvent_` → TSMSetCapsLockKeyTransitionDetected
    # hits a dispatch_assert_queue check that kills the process whenever the
    # user toggles CapsLock. NSEvent monitors run on the main run loop, so
    # this whole class of crashes goes away.
    # PRIMARY: Carbon RegisterEventHotKey — a true system-wide hotkey that needs
    # NO Accessibility/Input-Monitoring permission and fires no matter which app
    # is frontmost. This is the reliable path on modern macOS; NSEvent monitors
    # below only work over other apps if Accessibility happens to be granted.
    try:
        result = _register_hotkey_carbon(sequence, on_activated)
        if result:
            return result
    except Exception as e:
        logger.error(f"Carbon hotkey registration failed, falling back to NSEvent: {e}", exc_info=True)

    try:
        return _register_hotkey_nsevent(sequence, on_activated)
    except Exception as e:
        logger.error(f"NSEvent hotkey registration failed, falling back: {e}", exc_info=True)

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
        
        # macOS Sequoia hard-kills the process (SIGTRAP, _dispatch_assert_queue_fail)
        # when pynput tries to build an NSEvent for a CapsLock transition from its
        # background CGEventTap thread. Drop CapsLock events at the tap so they
        # never reach +[NSEvent eventWithCGEvent:].
        def _darwin_intercept(event_type, event):
            try:
                from Quartz import CGEventGetIntegerValueField, kCGKeyboardEventKeycode
                if CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode) == 57:
                    return None
            except Exception:
                pass
            return event

        listener = keyboard.Listener(
            on_press=on_press,
            on_release=on_release,
            darwin_intercept=_darwin_intercept,
        )
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
        if isinstance(filt, dict) and filt.get('kind') == 'carbon':
            carbon = filt.get('carbon')
            try:
                if carbon is not None and filt.get('hotkey_ref') is not None:
                    carbon.UnregisterEventHotKey(filt['hotkey_ref'])
            except Exception: pass
            try:
                if carbon is not None and filt.get('handler_ref') is not None:
                    carbon.RemoveEventHandler(filt['handler_ref'])
            except Exception: pass
            logger.info(f"Unregistered Carbon hotkey (id={hotkey_id})")
        elif isinstance(filt, dict) and filt.get('kind') == 'nsevent':
            from AppKit import NSEvent
            for k in ('global', 'local'):
                m = filt.get(k)
                if m is not None:
                    try: NSEvent.removeMonitor_(m)
                    except Exception: pass
            logger.info(f"Unregistered NSEvent hotkey (id={hotkey_id})")
        elif filt is not None and hasattr(filt, 'stop'):
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

def log_autofill_diagnostics(prefix: str = "[QS-DIAG]") -> None:
    """Capture exactly what will receive synthesized keystrokes during autofill.

    Logs: Accessibility trust, the frontmost app, whether the native Open/Save
    panel service is hosting the dialog, our own key window, the SYSTEM-WIDE AX
    focused UI element (the real keystroke target — role/subrole/title), and the
    clipboard. This is what tells us why a ⌘V lands (or doesn't). Never raises.
    """
    # Accessibility trust — without it the AX focused-element read below is blind.
    try:
        from ApplicationServices import AXIsProcessTrusted
        logger.info(f"{prefix} AXIsProcessTrusted={AXIsProcessTrusted()}")
    except Exception as e:
        logger.info(f"{prefix} AXIsProcessTrusted check failed: {e}")

    try:
        from AppKit import NSWorkspace, NSPasteboard, NSStringPboardType, NSApp
        ws = NSWorkspace.sharedWorkspace()

        fm = ws.frontmostApplication()
        if fm:
            logger.info(f"{prefix} frontmostApp: name={fm.localizedName()} "
                        f"pid={fm.processIdentifier()} bundle={fm.bundleIdentifier()}")

        # Native Open/Save panels are hosted out-of-process for sandboxed apps.
        panels = []
        for app in ws.runningApplications():
            bid = (app.bundleIdentifier() or "")
            nm = (app.localizedName() or "")
            if "openAndSavePanel" in bid or "Open and Save" in nm or "Powerbox" in nm:
                panels.append(f"{nm}({bid}, pid={app.processIdentifier()}, active={app.isActive()})")
        logger.info(f"{prefix} open/save panel service running: {panels or 'NONE (dialog is in-process)'}")

        try:
            kw = NSApp.keyWindow()
            logger.info(f"{prefix} OUR keyWindow: {kw.title() if kw else None} "
                        f"(if not None, keystrokes go to FILECT)")
        except Exception as e:
            logger.info(f"{prefix} keyWindow read failed: {e}")

        try:
            pb = NSPasteboard.generalPasteboard()
            logger.info(f"{prefix} clipboard: {str(pb.stringForType_(NSStringPboardType))[:80]!r}")
        except Exception as e:
            logger.info(f"{prefix} clipboard read failed: {e}")
    except Exception as e:
        logger.warning(f"{prefix} AppKit diagnostics failed: {e}")

    # The decisive fact: which UI element actually has keyboard focus system-wide.
    try:
        from ApplicationServices import (
            AXUIElementCreateSystemWide, AXUIElementCopyAttributeValue,
            kAXFocusedUIElementAttribute, kAXRoleAttribute,
            kAXSubroleAttribute, kAXTitleAttribute,
        )
        sysw = AXUIElementCreateSystemWide()
        err, el = AXUIElementCopyAttributeValue(sysw, kAXFocusedUIElementAttribute, None)
        if err == 0 and el:
            def _a(attr):
                try:
                    e, v = AXUIElementCopyAttributeValue(el, attr, None)
                    return v if e == 0 else None
                except Exception:
                    return None
            logger.info(f"{prefix} AX focused element: role={_a(kAXRoleAttribute)} "
                        f"subrole={_a(kAXSubroleAttribute)} title={_a(kAXTitleAttribute)!r} "
                        f"-> a paste/keystroke only lands if this is a text field")
        else:
            logger.info(f"{prefix} AX focused element: NONE (err={err}) — "
                        f"no text target, so ⌘V has nowhere to go")
    except Exception as e:
        logger.warning(f"{prefix} AX focused-element read failed: {e}")


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
        
        # Simulate Cmd+V to paste — log the exact keystroke target at this instant.
        log_autofill_diagnostics("[QS-DIAG at-paste]")
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


def is_native_open_save_panel() -> bool:
    """True if the element with keyboard focus belongs to the system Open/Save
    panel (hosted out-of-process as com.apple.appkit.xpc.openAndSavePanelService).

    These panels focus a file LIST, not a text field — so a plain paste is a
    no-op and a blind Enter imports whatever is highlighted. When this is True we
    must drive the panel via "Go to Folder" instead.
    """
    try:
        from ApplicationServices import (
            AXUIElementCreateSystemWide, AXUIElementCopyAttributeValue,
            AXUIElementGetPid, kAXFocusedUIElementAttribute, kAXRoleAttribute,
        )
        from AppKit import NSRunningApplication
        sysw = AXUIElementCreateSystemWide()
        err, el = AXUIElementCopyAttributeValue(sysw, kAXFocusedUIElementAttribute, None)
        if err != 0 or not el:
            return False

        # Role of the focused element.
        role = ""
        try:
            rerr, rval = AXUIElementCopyAttributeValue(el, kAXRoleAttribute, None)
            if rerr == 0 and rval:
                role = str(rval)
        except Exception:
            pass

        # Owning process bundle id.
        bid = ""
        err2, pid = AXUIElementGetPid(el, None)
        if err2 == 0 and pid:
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            bid = (app.bundleIdentifier() or "") if app else ""

        # A native Open/Save panel can be hosted out-of-process (sandboxed apps,
        # via openAndSavePanelService) OR in-process (non-sandboxed apps like
        # CapCut). In BOTH cases the focused element is the file-browser list, and
        # Go-to-Folder is the correct driver. Detect by either signal.
        FILE_BROWSER_ROLES = {"AXList", "AXOutline", "AXBrowser", "AXTable"}
        is_panel_service = (bid == "com.apple.appkit.xpc.openAndSavePanelService")
        is_browser_list = (role in FILE_BROWSER_ROLES and bid != "com.filect.filesearch")
        result = is_panel_service or is_browser_list
        logger.info(f"[QS] open/save panel detect: result={result} "
                    f"role={role} pid={pid} bundle={bid}")
        return result
    except Exception as e:
        logger.warning(f"[QS] is_native_open_save_panel error: {e}")
        return False


def autofill_via_go_to_folder(path: str, press_enter: bool = True) -> bool:
    """Fill a native macOS Open/Save panel via "Go to Folder" (Cmd+Shift+G).

    The panel's file LIST is focused (not a text field), so a plain paste does
    nothing. Go-to-Folder opens a REAL text field; we paste the FULL FILE PATH
    there and press Return — macOS navigates to the folder AND selects that exact
    file. A second Return then activates the default button (Open/Import) on the
    correct selection, instead of importing whatever was randomly highlighted.

    Args:
        path: The full file path to select.
        press_enter: If True, send the final Return to confirm/import.
    """
    try:
        from pynput.keyboard import Controller, Key
        import time

        kb = Controller()

        # 1) Open "Go to Folder".
        kb.press(Key.cmd); kb.press(Key.shift); kb.press('g')
        kb.release('g'); kb.release(Key.shift); kb.release(Key.cmd)
        time.sleep(0.5)  # let the sheet appear and focus its text field

        # Diagnostic: confirm a TEXT FIELD now holds focus (not the file list).
        log_autofill_diagnostics("[QS-DIAG go-to-folder-sheet]")

        # 2) TYPE the path char-by-char. Clipboard ⌘V (both Quartz and pynput) was
        #    not landing in this sheet; typing sends key events straight to the
        #    focused field and is clipboard-independent.
        for ch in path:
            kb.type(ch)
            time.sleep(0.012)
        time.sleep(0.25)

        # 3) Return: resolve the path -> navigate to the folder AND select the file.
        kb.press(Key.enter); kb.release(Key.enter)
        time.sleep(0.45)  # give the panel time to land on the file

        # 4) Return again: confirm/Import with the correct file now selected.
        if press_enter:
            kb.press(Key.enter); kb.release(Key.enter)

        logger.info("[QS] Go to Folder autofill completed (typed path)")
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


def try_macos_autofill_strategies(path: str, hwnd: int = None, app_name: str = None, press_enter: bool = True) -> Tuple[bool, str]:
    """
    Try multiple macOS autofill strategies in order of reliability.
    
    Args:
        path: The file path to fill
        hwnd: Optional PID of target app
        app_name: Optional name of target app
        press_enter: If True, press Enter after successful fill to confirm/open
    
    Returns:
        Tuple of (success: bool, method_used: str)
    """
    import time
    
    # Get app name from cache if not provided
    if not app_name and hwnd:
        info = get_last_active_app_info()
        if info.get('pid') == hwnd:
            app_name = info.get('name')
    
    # DIAGNOSTICS: log exactly what holds keyboard focus before we touch anything,
    # so we can see whether there's a real text target for the paste to land in.
    log_autofill_diagnostics("[QS-DIAG before-pipeline]")

    # NATIVE OPEN/SAVE PANEL (e.g. CapCut "Select a media resource"): the file
    # LIST is focused, not a text field — so a plain paste is a no-op and the
    # blind Enter below would import whatever was randomly highlighted. Drive it
    # with Go-to-Folder, which deterministically selects the exact file first.
    if is_native_open_save_panel():
        logger.info("[QS] Native open/save panel detected -> Go to Folder (primary strategy)")
        if autofill_via_go_to_folder(path, press_enter=press_enter):
            return (True, "go_to_folder")
        logger.warning("[QS] Go to Folder failed on native panel; falling through to other strategies")

    # Strategy 1: Clipboard + Paste (most reliable)
    logger.info("[QS] Trying strategy 1: Clipboard + Paste")
    if autofill_via_clipboard_paste(path):
        if press_enter:
            _press_enter_to_confirm()
        return (True, "clipboard_paste")
    
    time.sleep(0.2)
    
    # Strategy 2: AppleScript keystroke
    logger.info("[QS] Trying strategy 2: AppleScript keystroke")
    if autofill_via_applescript(path, app_name):
        if press_enter:
            _press_enter_to_confirm()
        return (True, "applescript")
    
    time.sleep(0.2)
    
    # Strategy 3: Go to Folder (for native file dialogs)
    # Note: This strategy already presses Enter internally
    logger.info("[QS] Trying strategy 3: Go to Folder (Cmd+Shift+G)")
    if autofill_via_go_to_folder(path):
        return (True, "go_to_folder")
    
    time.sleep(0.2)
    
    # Strategy 4: Direct keyboard simulation
    logger.info("[QS] Trying strategy 4: Keyboard simulation")
    if autofill_via_keyboard_simulation(path):
        if press_enter:
            _press_enter_to_confirm()
        return (True, "keyboard_simulation")
    
    # All strategies failed - at least copy to clipboard
    logger.warning("[QS] All autofill strategies failed, copying to clipboard as fallback")
    copy_path_to_clipboard(path)
    
    return (False, "all_failed_clipboard_fallback")


def _press_enter_to_confirm() -> bool:
    """
    Press Enter key to confirm the file selection in a file dialog.
    Called after path is filled to automatically open/select the file.
    """
    import time
    try:
        from pynput.keyboard import Controller, Key
        
        keyboard = Controller()
        
        # Small delay to let the path be fully entered
        time.sleep(0.2)
        
        # Press Enter to confirm
        keyboard.press(Key.enter)
        keyboard.release(Key.enter)
        
        logger.info("[QS] Pressed Enter to confirm file selection")
        return True
        
    except ImportError:
        logger.warning("[QS] pynput not available - cannot press Enter")
        return False
    except Exception as e:
        logger.error(f"[QS] Error pressing Enter: {e}")
        return False


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
        if listener is None:
            continue
        if isinstance(listener, dict) and listener.get('kind') == 'carbon':
            carbon = listener.get('carbon')
            try:
                if carbon is not None and listener.get('hotkey_ref') is not None:
                    carbon.UnregisterEventHotKey(listener['hotkey_ref'])
                if carbon is not None and listener.get('handler_ref') is not None:
                    carbon.RemoveEventHandler(listener['handler_ref'])
            except Exception:
                pass
        elif isinstance(listener, dict) and listener.get('kind') == 'nsevent':
            try:
                from AppKit import NSEvent
                for k in ('global', 'local'):
                    m = listener.get(k)
                    if m is not None:
                        NSEvent.removeMonitor_(m)
            except Exception:
                pass
        elif hasattr(listener, 'stop'):
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
