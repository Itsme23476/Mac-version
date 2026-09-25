"""
macOS private CGS/SkyLight API: move a window onto the CURRENT Space.

This is the one call that makes a floating overlay appear over the app the user is
in — including a fullscreen Space. Setting NSWindowCollectionBehavior alone does NOT
pull the window onto the active fullscreen Space; you have to CGSAddWindowsToSpaces
(or the SkyLight SLS* equivalent on newer macOS) it there.

Kept in its own tiny module (only ctypes + Foundation/Quartz/objc, no app imports) so
the dictation overlay can import it without dragging in the heavy quick-search /
search-service chain. The quick-search overlay has its own historical copy; this is a
lift of that proven implementation.
ponytail: duplicated from quick_search_overlay.move_window_to_active_space — collapse
both (and file_preview_window's lazy import) onto this module when touching that file.
"""
import sys
import logging

logger = logging.getLogger(__name__)


def move_window_to_active_space(window_number) -> bool:
    """Move the NSWindow with this windowNumber onto the currently active Space
    (works for fullscreen Spaces too). Returns True on success."""
    if sys.platform != 'darwin':
        return False
    try:
        from Foundation import NSArray, NSNumber
        import ctypes
        import ctypes.util

        cg_path = ctypes.util.find_library('CoreGraphics')
        cg = ctypes.CDLL(cg_path)

        cg._CGSDefaultConnection.restype = ctypes.c_uint32
        conn = cg._CGSDefaultConnection()

        cg.CGSGetActiveSpace.restype = ctypes.c_uint64
        cg.CGSGetActiveSpace.argtypes = [ctypes.c_uint32]
        active_space = cg.CGSGetActiveSpace(conn)

        logger.info(f"[CGS] Connection: {conn}, Active space: {active_space}, Window: {window_number}")

        window_array = NSArray.arrayWithObject_(NSNumber.numberWithInt_(window_number))
        space_array = NSArray.arrayWithObject_(NSNumber.numberWithLongLong_(active_space))

        cg.CGSAddWindowsToSpaces.restype = ctypes.c_int32
        cg.CGSAddWindowsToSpaces.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]

        from objc import pyobjc_id
        result = cg.CGSAddWindowsToSpaces(conn, pyobjc_id(window_array), pyobjc_id(space_array))
        logger.info(f"[CGS] CGSAddWindowsToSpaces result: {result}")
        return result == 0
    except ImportError as e:
        logger.warning(f"[CGS] Import error (trying alternate method): {e}")
        return _move_window_to_space_alternate(window_number)
    except Exception as e:
        logger.error(f"[CGS] Error moving window to space: {e}")
        return _move_window_to_space_alternate(window_number)


def _move_window_to_space_alternate(window_number) -> bool:
    """SkyLight (SLS*) fallback for newer macOS, plus CGS variants."""
    try:
        import ctypes
        import ctypes.util
        from Foundation import NSArray, NSNumber
        from objc import pyobjc_id

        skylight_path = '/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight'
        try:
            sls = ctypes.CDLL(skylight_path)
            use_skylight = True
            logger.info("[CGS] Using SkyLight framework")
        except OSError:
            cg_path = ctypes.util.find_library('CoreGraphics')
            sls = ctypes.CDLL(cg_path)
            use_skylight = False
            logger.info("[CGS] Using CoreGraphics framework")

        if use_skylight:
            sls.SLSMainConnectionID.restype = ctypes.c_uint32
            conn = sls.SLSMainConnectionID()
        else:
            sls._CGSDefaultConnection.restype = ctypes.c_uint32
            conn = sls._CGSDefaultConnection()

        active_space = None
        if use_skylight:
            try:
                sls.SLSGetActiveSpace.restype = ctypes.c_uint64
                sls.SLSGetActiveSpace.argtypes = [ctypes.c_uint32]
                active_space = sls.SLSGetActiveSpace(conn)
            except AttributeError:
                pass
        if not active_space:
            try:
                sls.CGSGetActiveSpace.restype = ctypes.c_uint64
                sls.CGSGetActiveSpace.argtypes = [ctypes.c_uint32]
                active_space = sls.CGSGetActiveSpace(conn)
            except AttributeError:
                pass
        if not active_space:
            logger.warning("[CGS] Could not get active space")
            return False

        logger.info(f"[CGS ALT] Connection: {conn}, Active space: {active_space}, Window: {window_number}")

        window_array = NSArray.arrayWithObject_(NSNumber.numberWithInt_(window_number))
        space_array = NSArray.arrayWithObject_(NSNumber.numberWithLongLong_(active_space))

        methods_to_try = [
            ('SLSAddWindowsToSpaces', sls if use_skylight else None),
            ('CGSAddWindowsToSpaces', sls),
            ('SLSMoveWindowsToManagedSpace', sls if use_skylight else None),
        ]
        for method_name, lib in methods_to_try:
            if lib is None:
                continue
            try:
                func = getattr(lib, method_name)
                func.restype = ctypes.c_int32
                func.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
                result = func(conn, pyobjc_id(window_array), pyobjc_id(space_array))
                logger.info(f"[CGS ALT] {method_name} result: {result}")
                if result == 0:
                    return True
            except AttributeError:
                continue
            except Exception as e:
                logger.debug(f"[CGS ALT] {method_name} error: {e}")
                continue
        logger.warning("[CGS ALT] All space move methods failed")
        return False
    except Exception as e:
        logger.error(f"[CGS ALT] Error: {e}")
        return False
