from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLineEdit, QTableWidget, QTableWidgetItem, 
    QPushButton, QAbstractItemView, QFrame, QGraphicsDropShadowEffect, QHeaderView,
    QWidget, QSizePolicy
)
from PySide6.QtCore import Qt, QTimer, Signal, QRect, QPropertyAnimation, QEasingCurve, QPoint, QThread, QSize
from PySide6.QtGui import QGuiApplication, QColor, QIcon
from app.core.search import search_service
from app.core.settings import settings
from app.core.query_parser import parse_query
from app.ui.win_hotkey import (
    get_cursor_pos, get_foreground_hwnd, get_window_rect, 
    is_file_dialog, get_window_title, get_window_class,
    restore_dialog_focus_hybrid, window_still_exists,
    log_system_state, create_autofill_debug_report, log_window_hierarchy
)
import logging
import sys

logger = logging.getLogger(__name__)


# ============================================================================
# macOS Private CGS APIs for moving windows to spaces (including fullscreen)
# ============================================================================
def _get_cgs_functions():
    """
    Load private CGS functions using ctypes.
    These are undocumented APIs that allow moving windows between spaces.
    """
    if sys.platform != 'darwin':
        return None
    
    try:
        import ctypes
        import ctypes.util
        
        # Load CoreGraphics framework
        cg_path = ctypes.util.find_library('CoreGraphics')
        if not cg_path:
            logger.warning("[CGS] CoreGraphics library not found")
            return None
        
        cg = ctypes.CDLL(cg_path)
        
        # Define function signatures
        # CGSConnectionID _CGSDefaultConnection(void)
        cg._CGSDefaultConnection.restype = ctypes.c_uint32
        cg._CGSDefaultConnection.argtypes = []
        
        # CGSSpaceID CGSGetActiveSpace(CGSConnectionID cid)
        # Note: This might be CGSManagedDisplayGetCurrentSpace on newer macOS
        try:
            cg.CGSGetActiveSpace.restype = ctypes.c_uint64
            cg.CGSGetActiveSpace.argtypes = [ctypes.c_uint32]
            has_get_active_space = True
        except AttributeError:
            has_get_active_space = False
        
        # CGError CGSAddWindowsToSpaces(CGSConnectionID cid, CFArrayRef windowIDs, CFArrayRef spaceIDs)
        try:
            cg.CGSAddWindowsToSpaces.restype = ctypes.c_int32
            cg.CGSAddWindowsToSpaces.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
            has_add_to_spaces = True
        except AttributeError:
            has_add_to_spaces = False
        
        return {
            'lib': cg,
            'has_get_active_space': has_get_active_space,
            'has_add_to_spaces': has_add_to_spaces
        }
    except Exception as e:
        logger.error(f"[CGS] Error loading CGS functions: {e}")
        return None


def move_window_to_active_space(window_number):
    """
    Move a window to the currently active space using private CGS APIs.
    This works for fullscreen spaces too.
    
    Args:
        window_number: The NSWindow's windowNumber property
    
    Returns:
        True if successful, False otherwise
    """
    if sys.platform != 'darwin':
        return False
    
    try:
        from Foundation import NSArray, NSNumber
        from Quartz import CGSGetActiveSpace, kCGSAllSpacesMask
        import ctypes
        import ctypes.util
        
        # Load CoreGraphics
        cg_path = ctypes.util.find_library('CoreGraphics')
        cg = ctypes.CDLL(cg_path)
        
        # Get default connection
        cg._CGSDefaultConnection.restype = ctypes.c_uint32
        conn = cg._CGSDefaultConnection()
        
        # Get active space
        cg.CGSGetActiveSpace.restype = ctypes.c_uint64
        cg.CGSGetActiveSpace.argtypes = [ctypes.c_uint32]
        active_space = cg.CGSGetActiveSpace(conn)
        
        logger.info(f"[CGS] Connection: {conn}, Active space: {active_space}, Window: {window_number}")
        
        # Create arrays for CGSAddWindowsToSpaces
        window_array = NSArray.arrayWithObject_(NSNumber.numberWithInt_(window_number))
        space_array = NSArray.arrayWithObject_(NSNumber.numberWithLongLong_(active_space))
        
        # Add window to active space
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


def _move_window_to_space_alternate(window_number):
    """
    Alternate method using SkyLight framework (SLS) for newer macOS versions.
    Also tries multiple CGS function variants.
    """
    try:
        import ctypes
        import ctypes.util
        from Foundation import NSArray, NSNumber
        from objc import pyobjc_id
        
        # Try SkyLight framework first (used in newer macOS)
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
        
        # Get connection
        if use_skylight:
            sls.SLSMainConnectionID.restype = ctypes.c_uint32
            conn = sls.SLSMainConnectionID()
        else:
            sls._CGSDefaultConnection.restype = ctypes.c_uint32
            conn = sls._CGSDefaultConnection()
        
        # Get active space
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
        
        # Create arrays
        window_array = NSArray.arrayWithObject_(NSNumber.numberWithInt_(window_number))
        space_array = NSArray.arrayWithObject_(NSNumber.numberWithLongLong_(active_space))
        
        # Try different methods to add window to space
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
                logger.debug(f"[CGS ALT] {method_name} not available")
                continue
            except Exception as e:
                logger.debug(f"[CGS ALT] {method_name} error: {e}")
                continue
        
        # If all methods failed, try a simpler approach: just set collection behavior
        # to force window to current space
        logger.warning("[CGS ALT] All space move methods failed, trying workaround")
        return False
        
    except Exception as e:
        logger.error(f"[CGS ALT] Error: {e}")
        return False


class SearchWorker(QThread):
    """Background thread for performing search without blocking UI."""
    results_ready = Signal(list)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self._query = ""
        self._limit = 20
    
    def set_query(self, query: str, limit: int = 20):
        self._query = query
        self._limit = limit
    
    def run(self):
        try:
            if self._query:
                # Use the same NLP parsing as the main window
                parsed = parse_query(self._query)
                
                clean_query = parsed.get('clean_query', self._query)
                type_filter = parsed.get('type_filter')
                date_range = parsed.get('date_range', (None, None))
                date_start, date_end = date_range
                extensions = parsed.get('extensions')
                
                # Log parsing results for debugging
                logger.debug(f"[QS_SEARCH] Original: '{self._query}' -> Clean: '{clean_query}', "
                            f"type={type_filter}, date={date_start} to {date_end}")
                
                results = search_service.search_files(
                    clean_query, 
                    limit=self._limit,
                    type_filter=type_filter,
                    date_start=date_start,
                    date_end=date_end,
                    extensions=extensions
                )
            else:
                results = []
            self.results_ready.emit(results)
        except Exception as e:
            logger.error(f"Search worker error: {e}")
            self.results_ready.emit([])


class QuickSearchOverlay(QDialog):
    pathSelected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        import sys
        
        # Frameless, translucent, always on top
        # Use Qt.Tool on macOS to make it a floating panel that doesn't bring main window
        self.setWindowFlags(
            Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("Quick Search")
        self.setModal(False)
        self.resize(720, 300)
        
        # On macOS, we need to configure the NSPanel AFTER the window is created
        # This flag tracks whether we've done the one-time native configuration
        self._macos_panel_configured = False

        # Phase 1: State Capture Variables
        self._saved_cursor_pos = None
        self._saved_window_hwnd = None
        self._saved_window_rect = None
        self._saved_window_title = ""
        self._saved_window_class = ""
        self._is_dialog_verified = False
        
        # Dragging state
        self._drag_pos = None
        
        # Flag to control re-activation after opening files
        # Set to False when user clicks outside popup to prevent timers from stealing focus back
        self._allow_reactivation = True

        # Main layout for the dialog (transparent)
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)  # Margins for shadow

        # Container Frame (The visible "Window")
        self.container = QFrame()
        self.container.setObjectName("overlayFrame")
        
        # Shadow Effect
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setColor(QColor(0, 0, 0, 150))
        shadow.setOffset(0, 5)
        self.container.setGraphicsEffect(shadow)
        
        main_layout.addWidget(self.container)

        # Layout inside the container
        layout = QVBoxLayout(self.container)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # === Header row: Search input + X close button ===
        header_row = QHBoxLayout()
        header_row.setSpacing(8)
        
        self.input = QLineEdit()
        self.input.setObjectName("overlayInput")
        self.input.setPlaceholderText("Type to search... (Enter to fill, Ctrl+O to open)")
        header_row.addWidget(self.input, 1)  # Stretch factor 1
        
        # X close button at top-right
        self.btn_close = QPushButton("X")
        self.btn_close.setObjectName("overlayCloseBtn")
        self.btn_close.setFixedSize(28, 28)
        self.btn_close.setCursor(Qt.PointingHandCursor)
        self.btn_close.setToolTip("Close (Esc)")
        self.btn_close.clicked.connect(self.hide)
        header_row.addWidget(self.btn_close)
        
        layout.addLayout(header_row)

        # === Results table with Open button column ===
        self.results = QTableWidget()
        self.results.setObjectName("overlayResults")
        self.results.setColumnCount(4)  # Open btn, Name, Label, Tags
        self.results.setHorizontalHeaderLabels(["", "Name", "Label", "Tags"])
        self.results.horizontalHeader().setVisible(False)  # Cleaner look without headers
        self.results.verticalHeader().setVisible(False)
        self.results.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results.setSelectionMode(QAbstractItemView.SingleSelection)
        self.results.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.results.setFocusPolicy(Qt.ClickFocus)  # Only focus when clicked, not when items added
        self.results.setShowGrid(False)
        
        # Disable mouse tracking to prevent hover-based selection changes
        self.results.setMouseTracking(False)
        self.results.viewport().setMouseTracking(False)
        
        # Disable drag and drop which can interfere with selection
        self.results.setDragEnabled(False)
        self.results.setDragDropMode(QAbstractItemView.NoDragDrop)
        
        # Install event filter to block unwanted selection changes
        self.results.viewport().installEventFilter(self)
        
        # Column widths: Open btn fixed, others stretch
        self.results.setColumnWidth(0, 90)  # Open button column - wide enough for full label
        self.results.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.results.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.results.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.results.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        
        layout.addWidget(self.results)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(180)
        self._debounce.timeout.connect(self._run_search)
        self.input.textChanged.connect(self._debounce.start)

        self.input.returnPressed.connect(self._accept_selection)
        self.results.itemDoubleClicked.connect(self._accept_selection)
        self.results.itemSelectionChanged.connect(self._on_selection_changed)
        self.results.cellClicked.connect(self._on_cell_clicked)

        # === Footer: Fill + Copy Path buttons ===
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        
        self.btn_fill = QPushButton("Fill")
        self.btn_fill.setObjectName("overlayFillBtn")
        self.btn_fill.setMinimumWidth(100)
        self.btn_fill.setDefault(True)
        self.btn_fill.setEnabled(False)
        self.btn_fill.setToolTip("Fill path into file dialog (Enter)")
        self.btn_fill.clicked.connect(self._accept_selection)
        btn_row.addWidget(self.btn_fill)

        self.btn_copy_path = QPushButton("Copy Path")
        self.btn_copy_path.setObjectName("overlayCopyBtn")
        self.btn_copy_path.setMinimumWidth(110)
        self.btn_copy_path.setEnabled(False)
        self.btn_copy_path.setToolTip("Copy selected file path to clipboard")
        self.btn_copy_path.clicked.connect(self._copy_current_path)
        btn_row.addWidget(self.btn_copy_path)
        
        btn_row.addStretch()
        layout.addLayout(btn_row)
        
        # Opacity animation
        self.opacity_anim = QPropertyAnimation(self, b"windowOpacity")
        self.opacity_anim.setDuration(200)
        self.opacity_anim.setEasingCurve(QEasingCurve.OutCubic)
        
        # Background search worker
        self._search_worker = SearchWorker(self)
        self._search_worker.results_ready.connect(self._on_search_results)
        self._pending_query = None  # Track if a new search is needed

    def capture_state_before_popup(self):
        """Phase 1: Capture current state before showing popup."""
        try:
            logger.info("[QS] Phase 1: Capturing state before popup")
            
            # Save mouse cursor position
            self._saved_cursor_pos = get_cursor_pos()
            if self._saved_cursor_pos:
                logger.info(f"[QS] Saved cursor position: {self._saved_cursor_pos}")
            else:
                logger.warning("[QS] Failed to get cursor position")
            
            # Save active window handle
            self._saved_window_hwnd = get_foreground_hwnd()
            if self._saved_window_hwnd:
                logger.info(f"[QS] Saved window handle: {self._saved_window_hwnd}")
                
                # Get window details for verification
                self._saved_window_title = get_window_title(self._saved_window_hwnd)
                self._saved_window_class = get_window_class(self._saved_window_hwnd)
                self._saved_window_rect = get_window_rect(self._saved_window_hwnd)
                
                logger.info(f"[QS] Window title: '{self._saved_window_title}'")
                logger.info(f"[QS] Window class: '{self._saved_window_class}'")
                logger.info(f"[QS] Window rect: {self._saved_window_rect}")
                
                # Check if it appears to be a file dialog
                self._is_dialog_verified = is_file_dialog(self._saved_window_hwnd)
                logger.info(f"[QS] Is file dialog: {self._is_dialog_verified}")
                
            else:
                logger.warning("[QS] Failed to get foreground window handle")
                self._saved_window_title = ""
                self._saved_window_class = ""
                self._saved_window_rect = None
                self._is_dialog_verified = False
                
        except Exception as e:
            logger.error(f"[QS] Error capturing state: {e}")
            self._reset_saved_state()
    
    def _reset_saved_state(self):
        """Reset all saved state variables."""
        self._saved_cursor_pos = None
        self._saved_window_hwnd = None
        self._saved_window_rect = None
        self._saved_window_title = ""
        self._saved_window_class = ""
        self._is_dialog_verified = False
    
    def _remove_stay_on_top(self):
        """Temporarily remove WindowStaysOnTopHint so opened files can appear on top."""
        try:
            flags = self.windowFlags()
            if flags & Qt.WindowStaysOnTopHint:
                flags &= ~Qt.WindowStaysOnTopHint
                self.setWindowFlags(flags)
                self.show()  # Required after changing window flags
                logger.info("[QS] Removed WindowStaysOnTopHint - popup can now go behind other windows")
        except Exception as e:
            logger.error(f"[QS] Error removing stay-on-top: {e}")
    
    def _restore_stay_on_top(self):
        """Restore WindowStaysOnTopHint so popup stays on top again."""
        try:
            flags = self.windowFlags()
            if not (flags & Qt.WindowStaysOnTopHint):
                flags |= Qt.WindowStaysOnTopHint
                self.setWindowFlags(flags)
                self.show()  # Required after changing window flags
                self.raise_()
                self.activateWindow()
                logger.info("[QS] Restored WindowStaysOnTopHint - popup is now always on top")
        except Exception as e:
            logger.error(f"[QS] Error restoring stay-on-top: {e}")
    
    def focusInEvent(self, event):
        """Restore stay-on-top when the popup regains focus."""
        self._allow_reactivation = True  # Re-enable reactivation when user clicks back
        self._restore_stay_on_top()
        super().focusInEvent(event)
    
    def focusOutEvent(self, event):
        """Remove stay-on-top when popup loses focus (user clicked on an opened file)."""
        self._allow_reactivation = False  # Stop timers from stealing focus back
        self._remove_stay_on_top()
        super().focusOutEvent(event)
    
    def log_saved_state(self):
        """Log the current saved state for debugging."""
        logger.info("[QS] === SAVED STATE SUMMARY ===")
        logger.info(f"[QS] Cursor position: {self._saved_cursor_pos}")
        logger.info(f"[QS] Window handle: {self._saved_window_hwnd}")
        logger.info(f"[QS] Window title: '{self._saved_window_title}'")
        logger.info(f"[QS] Window class: '{self._saved_window_class}'")
        logger.info(f"[QS] Window rect: {self._saved_window_rect}")
        logger.info(f"[QS] Is file dialog: {self._is_dialog_verified}")
        logger.info("[QS] === END STATE SUMMARY ===")
    
    def has_valid_saved_state(self) -> bool:
        """Check if we have valid saved state to work with."""
        return (self._saved_cursor_pos is not None and 
                self._saved_window_hwnd is not None and 
                self._saved_window_rect is not None)
    
    def verify_focus_restoration(self) -> bool:
        """Verify that the target dialog is now in focus."""
        try:
            if not self._saved_window_hwnd:
                return False
                
            current_fg = get_foreground_hwnd()
            is_focused = current_fg == self._saved_window_hwnd
            
            logger.info(f"[QS] Focus verification: target={self._saved_window_hwnd}, current={current_fg}, match={is_focused}")
            return is_focused
        except Exception as e:
            logger.error(f"[QS] Error verifying focus: {e}")
            return False
    
    def restore_dialog_focus_with_retries(self, max_retries: int = 3, delay_ms: int = 500):
        """
        Phase 2: Restore focus with retry logic.
        
        Returns: (success: bool, method_used: str)
        """
        try:
            logger.info(f"[QS] Phase 2: Starting focus restoration (max_retries={max_retries})")
            
            if not self.has_valid_saved_state():
                logger.warning("[QS] No valid saved state for focus restoration")
                return False, "no_saved_state"
            
            # Check if target window still exists
            if not window_still_exists(self._saved_window_hwnd):
                logger.warning(f"[QS] Target window {self._saved_window_hwnd} no longer exists")
                return False, "window_gone"
            
            logger.info(f"[QS] Target window: {self._saved_window_hwnd} ('{self._saved_window_title}')")
            
            # Try multiple times with increasing delays
            for attempt in range(max_retries):
                current_delay = delay_ms + (attempt * 200)  # Increase delay each attempt
                
                logger.info(f"[QS] Attempt {attempt + 1}/{max_retries} with {current_delay}ms delay")
                
                # Use the hybrid restoration approach
                success, method = restore_dialog_focus_hybrid(
                    self._saved_window_hwnd,
                    self._saved_cursor_pos,
                    self._saved_window_rect,
                    current_delay
                )
                
                if success:
                    # Verify the restoration actually worked
                    if self.verify_focus_restoration():
                        logger.info(f"[QS] Focus restoration SUCCESS on attempt {attempt + 1} using {method}")
                        return True, f"{method}_attempt{attempt + 1}"
                    else:
                        logger.warning(f"[QS] Method {method} reported success but verification failed")
                else:
                    logger.warning(f"[QS] Attempt {attempt + 1} failed: {method}")
            
            logger.error("[QS] All focus restoration attempts failed")
            return False, "all_attempts_failed"
            
        except Exception as e:
            logger.error(f"[QS] Exception during focus restoration: {e}")
            return False, f"exception_{str(e)[:20]}"
    
    def restore_dialog_focus(self, delay_ms: int = 500):
        """
        Phase 2: Restore focus to the previously active file dialog.
        
        Returns: (success: bool, method_used: str)
        """
        return self.restore_dialog_focus_with_retries(max_retries=3, delay_ms=delay_ms)
    
    def log_debug_system_state(self):
        """Phase 4: Log comprehensive system state for debugging."""
        try:
            logger.info("[QS] === DEBUG: System State Before Popup ===")
            log_system_state(logger, "[QS]")
        except Exception as e:
            logger.error(f"[QS] Error logging system state: {e}")
    
    def log_debug_target_window(self):
        """Phase 4: Log detailed information about the target window."""
        try:
            if not self.has_valid_saved_state():
                logger.warning("[QS] No saved state for target window debugging")
                return
            
            logger.info("[QS] === DEBUG: Target Window Details ===")
            hwnd = self._saved_window_hwnd
            
            # Basic window info
            logger.info(f"[QS] Target HWND: {hwnd}")
            logger.info(f"[QS] Title: '{self._saved_window_title}'")
            logger.info(f"[QS] Class: '{self._saved_window_class}'")
            logger.info(f"[QS] Rect: {self._saved_window_rect}")
            logger.info(f"[QS] Is Dialog: {self._is_dialog_verified}")
            logger.info(f"[QS] Cursor: {self._saved_cursor_pos}")
            
            # Current state
            if window_still_exists(hwnd):
                current_title = get_window_title(hwnd)
                current_class = get_window_class(hwnd)
                current_rect = get_window_rect(hwnd)
                
                logger.info(f"[QS] Current Title: '{current_title}'")
                logger.info(f"[QS] Current Class: '{current_class}'")
                logger.info(f"[QS] Current Rect: {current_rect}")
                
                # Check for changes
                if current_title != self._saved_window_title:
                    logger.warning(f"[QS] TITLE CHANGED!")
                if current_class != self._saved_window_class:
                    logger.warning(f"[QS] CLASS CHANGED!")
                if current_rect != self._saved_window_rect:
                    logger.warning(f"[QS] WINDOW MOVED/RESIZED!")
                
                # Log window hierarchy
                log_window_hierarchy(hwnd, logger, "[QS]")
            else:
                logger.error("[QS] TARGET WINDOW NO LONGER EXISTS!")
                
        except Exception as e:
            logger.error(f"[QS] Error logging target window: {e}")
    
    def create_comprehensive_debug_report(self):
        """Phase 4: Create a comprehensive debug report for troubleshooting."""
        try:
            logger.info("[QS] === COMPREHENSIVE DEBUG REPORT ===")
            
            # System state
            self.log_debug_system_state()
            
            # Target window details
            self.log_debug_target_window()
            
            # Autofill debug report
            if self.has_valid_saved_state():
                create_autofill_debug_report(
                    self._saved_window_hwnd,
                    self._saved_cursor_pos,
                    self._saved_window_rect,
                    logger,
                    "[QS]"
                )
            
            logger.info("[QS] === END COMPREHENSIVE DEBUG REPORT ===")
            
        except Exception as e:
            logger.error(f"[QS] Error creating comprehensive debug report: {e}")

    def show_centered_bottom(self):
        # Capture state BEFORE showing the popup
        self.capture_state_before_popup()
        
        screen = QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        # Use saved geometry if available; otherwise bottom-center
        g = settings.quick_search_geometry
        if g and all(k in g for k in ('x','y','w','h')):
            x, y, w, h = g['x'], g['y'], g['w'], g['h']
            self.setGeometry(QRect(x, y, w, h))
        else:
            w = self.width()
            h = self.height()
            x = geo.x() + (geo.width() - w) // 2
            y = geo.y() + geo.height() - h - 100 # Higher up for spotlight feel
            self.setGeometry(QRect(x, y, w, h))
        
        import sys
        
        logger.info("[QS] show_centered_bottom: Starting to show popup")
        
        # Start Fade In Animation
        self.setWindowOpacity(0)
        
        logger.info("[QS] About to call self.show()")
        self.show()
        logger.info("[QS] self.show() completed")
        
        self.opacity_anim.setStartValue(0)
        self.opacity_anim.setEndValue(1)
        self.opacity_anim.start()

        # On macOS, we MUST configure the window AFTER show() completes
        # because Qt resets window properties during show()
        # Use a short timer to ensure Qt has finished its internal processing
        if sys.platform == 'darwin':
            # Immediate configuration
            self._bring_to_front()
            # Delayed re-configuration to fight Qt's resets
            QTimer.singleShot(10, self._bring_to_front)
            QTimer.singleShot(50, self._bring_to_front)
            QTimer.singleShot(100, self._ensure_macos_panel_visible)
        else:
            self._bring_to_front()
        
        logger.info("[QS] _bring_to_front() completed")
        
        self.input.setFocus()
        self.input.selectAll()
        logger.info("[QS] show_centered_bottom: Completed")
    
    def _ensure_macos_panel_visible(self):
        """
        Final check to ensure the panel is visible and properly configured.
        Called after a delay to fight Qt's window property resets.
        """
        try:
            from AppKit import NSApp, NSScreen
            
            popup_window = None
            for ns_window in NSApp.windows():
                try:
                    if ns_window.title() == "Quick Search":
                        popup_window = ns_window
                        break
                except:
                    continue
            
            if popup_window:
                current_level = popup_window.level()
                if current_level < 1000:
                    logger.warning(f"[QS] Panel level was reset to {current_level}, re-applying 1000")
                    popup_window.setLevel_(1000)
                    popup_window.setHidesOnDeactivate_(False)
                    # CanJoinAllSpaces | FullScreenAuxiliary = 257
                    popup_window.setCollectionBehavior_((1 << 0) | (1 << 8))
                    popup_window.orderFrontRegardless()
                
                # Log detailed state
                frame = popup_window.frame()
                screen = popup_window.screen()
                screen_frame = screen.frame() if screen else "No screen"
                logger.info(f"[QS] Final panel state: level={popup_window.level()}, visible={popup_window.isVisible()}")
                logger.info(f"[QS] Panel frame: ({frame.origin.x}, {frame.origin.y}, {frame.size.width}, {frame.size.height})")
                logger.info(f"[QS] Panel screen: {screen_frame}")
                logger.info(f"[QS] Collection behavior: {popup_window.collectionBehavior()}")
                logger.info(f"[QS] hidesOnDeactivate: {popup_window.hidesOnDeactivate()}")
                
                # Check if we're on the same screen as the active app
                main_screen = NSScreen.mainScreen()
                if main_screen:
                    main_frame = main_screen.frame()
                    logger.info(f"[QS] Main screen: ({main_frame.origin.x}, {main_frame.origin.y}, {main_frame.size.width}, {main_frame.size.height})")
                
        except Exception as e:
            logger.error(f"[QS] _ensure_macos_panel_visible error: {e}")
    
    def _bring_to_front(self):
        """Platform-specific method to bring window to front and focus it."""
        import sys
        
        if sys.platform == 'darwin':
            self._bring_to_front_macos()
        else:
            # Windows/Linux - standard Qt should work
            self.raise_()
            self.activateWindow()
            self.setFocus()
    
    def _bring_to_front_macos(self):
        """
        macOS-specific window handling for fullscreen overlay.
        
        Since the app is now an agent app (LSUIElement), it can appear over
        fullscreen apps. We use private CGS APIs to move the window to the
        active space (including fullscreen spaces).
        """
        try:
            from AppKit import NSApp
            
            logger.info("[QS] _bring_to_front_macos: Starting (agent app mode)")
            
            # Find our popup window
            popup_window = None
            for ns_window in NSApp.windows():
                try:
                    if ns_window.title() == "Quick Search":
                        popup_window = ns_window
                        break
                except:
                    continue
            
            if not popup_window:
                logger.warning("[QS] Could not find Quick Search window")
                self.raise_()
                return
            
            logger.info(f"[QS] Found popup window: {type(popup_window).__name__}")
            
            # Configure the panel for fullscreen compatibility
            self._configure_macos_panel(popup_window)
            
            # CRITICAL: Use private CGS API to move window to active space
            # This is what makes the popup appear on fullscreen spaces!
            try:
                window_number = popup_window.windowNumber()
                logger.info(f"[QS] Window number: {window_number}")
                
                if move_window_to_active_space(window_number):
                    logger.info("[QS] Successfully moved window to active space via CGS API")
                else:
                    logger.warning("[QS] CGS API failed, window may not appear on fullscreen space")
            except Exception as e:
                logger.error(f"[QS] Error with CGS API: {e}")
            
            # Order front - as an agent app, this should work in fullscreen Spaces
            try:
                popup_window.orderFrontRegardless()
                logger.info("[QS] Called orderFrontRegardless()")
            except Exception as e:
                logger.error(f"[QS] Error with orderFrontRegardless: {e}")
            
            # Make it the key window for keyboard input
            try:
                popup_window.makeKeyAndOrderFront_(None)
                is_key = popup_window.isKeyWindow()
                logger.info(f"[QS] makeKeyAndOrderFront_() called, isKeyWindow={is_key}")
                
                # If not key window yet, try makeKeyWindow() directly
                if not is_key:
                    popup_window.makeKeyWindow()
                    is_key = popup_window.isKeyWindow()
                    logger.info(f"[QS] makeKeyWindow() called, isKeyWindow={is_key}")
            except Exception as e:
                logger.error(f"[QS] Error making key window: {e}")
            
            # Schedule delayed focus with activation fix
            # This is critical: after a short delay, we allow activation and claim keyboard focus
            QTimer.singleShot(50, self._macos_claim_keyboard_focus)
            QTimer.singleShot(100, self._macos_focus_input)
            
            logger.info("[QS] macOS fullscreen overlay configuration complete")
            
        except ImportError as e:
            logger.warning(f"[QS] AppKit not available: {e}")
            self.raise_()
        except Exception as e:
            logger.error(f"[QS] Error in _bring_to_front_macos: {e}", exc_info=True)
            self.raise_()
    
    def _configure_macos_panel(self, ns_window):
        """
        Configure NSPanel/NSWindow for fullscreen overlay capability.
        
        Key settings based on research:
        - NSWindowCollectionBehaviorCanJoinAllSpaces (1 << 0 = 1) - appears on ALL spaces
        - NSWindowCollectionBehaviorFullScreenAuxiliary (1 << 8 = 256) - can appear in fullscreen
        Note: CanJoinAllSpaces and MoveToActiveSpace are MUTUALLY EXCLUSIVE!
        - High window level (above fullscreen apps)
        - hidesOnDeactivate = False (critical!)
        """
        try:
            # Collection behavior flags
            # NSWindowCollectionBehaviorCanJoinAllSpaces = 1 << 0 = 1 (appears on all spaces)
            # NSWindowCollectionBehaviorFullScreenAuxiliary = 1 << 8 = 256 (can appear in fullscreen)
            # Combined = 257
            COLLECTION_BEHAVIOR = (1 << 0) | (1 << 8)  # 257 - CanJoinAllSpaces | FullScreenAuxiliary
            
            # Window level - use a high level to ensure visibility
            # NSScreenSaverWindowLevel = 1000 (very high, above most things)
            # NSStatusWindowLevel = 25
            # NSMainMenuWindowLevel = 24
            # Try NSScreenSaverWindowLevel for maximum visibility
            WINDOW_LEVEL = 1000  # NSScreenSaverWindowLevel
            
            # Set collection behavior
            current_behavior = ns_window.collectionBehavior()
            ns_window.setCollectionBehavior_(COLLECTION_BEHAVIOR)
            new_behavior = ns_window.collectionBehavior()
            logger.info(f"[QS] Collection behavior: {current_behavior} -> {new_behavior} (target: {COLLECTION_BEHAVIOR})")
            
            # Set window level
            current_level = ns_window.level()
            ns_window.setLevel_(WINDOW_LEVEL)
            new_level = ns_window.level()
            logger.info(f"[QS] Window level: {current_level} -> {new_level} (target: {WINDOW_LEVEL})")
            
            # CRITICAL: Prevent panel from hiding when app loses focus
            if hasattr(ns_window, 'setHidesOnDeactivate_'):
                ns_window.setHidesOnDeactivate_(False)
                logger.info("[QS] Set hidesOnDeactivate=False (CRITICAL)")
            
            # CRITICAL: Set the prevents-activation tag that AppKit normally sets during init
            # This is the workaround for Qt not setting nonactivatingPanel style mask at creation
            # Without this, the window cannot appear over fullscreen apps properly
            # NOTE: Only set this on FIRST call - once we've claimed keyboard focus, don't re-set it!
            if not getattr(self, '_keyboard_focus_claimed', False):
                try:
                    # Use objc to call the private method _setPreventsActivation:
                    import objc
                    if hasattr(ns_window, '_setPreventsActivation_'):
                        ns_window._setPreventsActivation_(True)
                        logger.info("[QS] Called _setPreventsActivation_(True) - CRITICAL for fullscreen")
                    else:
                        # Try using objc.objc_msgSend as fallback
                        try:
                            objc.objc_msgSend(ns_window, objc.selector(None, selector=b'_setPreventsActivation:', signature=b'v@:c'), True)
                            logger.info("[QS] Called _setPreventsActivation_ via objc_msgSend")
                        except Exception as e2:
                            logger.warning(f"[QS] Could not call _setPreventsActivation: {e2}")
                except Exception as e:
                    logger.error(f"[QS] Error calling _setPreventsActivation: {e}")
            else:
                logger.info("[QS] Skipping _setPreventsActivation_ - keyboard focus already claimed")
            
            # Try to set nonactivating panel style if this is an NSPanel
            try:
                class_name = type(ns_window).__name__
                logger.info(f"[QS] Window class: {class_name}")
                
                if hasattr(ns_window, 'setFloatingPanel_'):
                    ns_window.setFloatingPanel_(True)
                    logger.info("[QS] Set floatingPanel=True")
                
                if hasattr(ns_window, 'setBecomesKeyOnlyIfNeeded_'):
                    ns_window.setBecomesKeyOnlyIfNeeded_(False)  # False = always become key window
                    logger.info("[QS] Set becomesKeyOnlyIfNeeded=False (force key window)")
                
                if hasattr(ns_window, 'setWorksWhenModal_'):
                    ns_window.setWorksWhenModal_(True)
                    logger.info("[QS] Set worksWhenModal=True")
                    
            except Exception as e:
                logger.debug(f"[QS] Could not set panel-specific properties: {e}")
            
            try:
                can_become_key = ns_window.canBecomeKeyWindow()
                logger.info(f"[QS] canBecomeKeyWindow: {can_become_key}")
            except Exception as e:
                logger.debug(f"[QS] Could not check canBecomeKeyWindow: {e}")
            
            self._macos_panel_configured = True
            
        except Exception as e:
            logger.error(f"[QS] Error configuring macOS panel: {e}")
    
    def _macos_claim_keyboard_focus(self):
        """
        Delayed method to claim keyboard focus after window is visible.
        This reverses _setPreventsActivation_ and makes the window the key window.
        """
        try:
            from AppKit import NSApp
            
            # Find our popup window
            popup_window = None
            for ns_window in NSApp.windows():
                try:
                    if ns_window.title() == "Quick Search":
                        popup_window = ns_window
                        break
                except:
                    continue
            
            if not popup_window:
                logger.warning("[QS] _macos_claim_keyboard_focus: Could not find window")
                return
            
            # CRITICAL: Now allow activation so we can receive keyboard input
            # The window is already visible on the correct space, so this won't cause a switch
            try:
                import objc
                if hasattr(popup_window, '_setPreventsActivation_'):
                    popup_window._setPreventsActivation_(False)
                    self._keyboard_focus_claimed = True  # Set flag to prevent re-setting to True
                    logger.info("[QS] Called _setPreventsActivation_(False) - enabling keyboard input")
            except Exception as e:
                logger.warning(f"[QS] Could not reverse _setPreventsActivation: {e}")
            
            # Activate the app - this is safe now that the window is on the correct space
            try:
                NSApp.activateIgnoringOtherApps_(True)
                logger.info("[QS] Called activateIgnoringOtherApps_(True)")
            except Exception as e:
                logger.warning(f"[QS] Could not activate app: {e}")
            
            # Now make it the key window
            try:
                popup_window.makeKeyWindow()
                is_key = popup_window.isKeyWindow()
                logger.info(f"[QS] makeKeyWindow() in delayed focus, isKeyWindow={is_key}")
                
                # If still not key, try more aggressive approach
                if not is_key:
                    popup_window.makeKeyAndOrderFront_(None)
                    is_key = popup_window.isKeyWindow()
                    logger.info(f"[QS] makeKeyAndOrderFront_() retry, isKeyWindow={is_key}")
            except Exception as e:
                logger.error(f"[QS] Error making key window in delayed focus: {e}")
            
        except Exception as e:
            logger.error(f"[QS] Error in _macos_claim_keyboard_focus: {e}")
    
    def _macos_focus_input(self):
        """Delayed focus for macOS to ensure window is ready."""
        import sys
        try:
            if sys.platform == 'darwin':
                # On macOS, just focus the input without activating the window again
                self.input.setFocus()
                self.input.selectAll()
                logger.info("[QS] macOS: Focus applied to input")
            else:
                self.raise_()
                self.activateWindow()
                self.input.setFocus()
                self.input.selectAll()
        except Exception as e:
            logger.error(f"[QS] Error in delayed focus: {e}")

    def enable_focus_mode(self):
        """Temporarily allow this window to accept focus and focus the input."""
        flags = self.windowFlags()
        # Remove DoesNotAcceptFocus
        flags &= ~Qt.WindowDoesNotAcceptFocus
        # Ensure normal activation
        flags |= Qt.Window
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_ShowWithoutActivating, False)
        self.show()
        self.raise_()
        self.activateWindow()
        self.input.setFocus()

    def showEvent(self, e):
        """Called every time the window is shown. Set window level permanently."""
        super().showEvent(e)
        import sys
        if sys.platform == 'darwin':
            # Set window level immediately and start enforcement timer
            self._enforce_window_level()
            # Start a timer to keep enforcing the level while visible
            if not hasattr(self, '_level_timer'):
                self._level_timer = QTimer(self)
                self._level_timer.timeout.connect(self._enforce_window_level)
            self._level_timer.start(100)  # Check every 100ms

    def _enforce_window_level(self):
        """Continuously enforce window level to prevent Qt from resetting it."""
        try:
            from AppKit import NSApp
            for ns_window in NSApp.windows():
                try:
                    if ns_window.title() == "Quick Search":
                        current_level = ns_window.level()
                        if current_level < 1000:
                            ns_window.setLevel_(1000)
                            ns_window.setHidesOnDeactivate_(False)
                            ns_window.setCollectionBehavior_((1 << 0) | (1 << 8))  # 257
                        break
                except:
                    continue
        except Exception as e:
            logger.debug(f"[QS] _enforce_window_level error: {e}")

    def hideEvent(self, e):
        # Stop the level enforcement timer
        if hasattr(self, '_level_timer'):
            self._level_timer.stop()
        # Reset keyboard focus flag so next show can properly configure the panel
        self._keyboard_focus_claimed = False
        # Persist geometry on close/hide
        try:
            g = self.geometry()
            settings.quick_search_geometry = {'x': g.x(), 'y': g.y(), 'w': g.width(), 'h': g.height()}
            settings._save_config()
        except Exception:
            pass
        super().hideEvent(e)

    def _run_search(self):
        """Start a background search. If a search is already running, queue the new query."""
        q = self.input.text().strip()
        
        if not q:
            # Empty query - clear results immediately
            self._rows = []
            self.results.setRowCount(0)
            self.btn_fill.setEnabled(False)
            return
        
        if self._search_worker.isRunning():
            # A search is in progress - save this query to run after
            self._pending_query = q
            return
        
        # Start the search in background
        self._search_worker.set_query(q, limit=20)
        self._search_worker.start()
    
    def _on_search_results(self, rows):
        """Handle search results from the background worker."""
        self._rows = rows
        self.results.setRowCount(len(rows))
        for i, r in enumerate(rows):
            # Column 0: Open button fills entire cell
            open_btn = QPushButton("Open")
            open_btn.setFocusPolicy(Qt.NoFocus)  # Prevent button from stealing focus from input
            open_btn.setCursor(Qt.PointingHandCursor)
            open_btn.setToolTip("Open file")
            open_btn.clicked.connect(lambda checked, row=i: self._open_row(row))
            open_btn.setStyleSheet("""
                QPushButton {
                    background-color: #7C4DFF;
                    border: none;
                    border-radius: 8px;
                    color: white;
                    font-size: 12px;
                    font-weight: bold;
                    padding: 0px;
                    margin: 2px;
                }
                QPushButton:hover {
                    background-color: #9575FF;
                }
                QPushButton:pressed {
                    background-color: #6A3DE8;
                }
            """)
            self.results.setCellWidget(i, 0, open_btn)
            self.results.setRowHeight(i, 34)
            
            # Column 1: Name
            name_text = r.get('file_name') or ''
            name = QTableWidgetItem(name_text)
            name.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            self.results.setItem(i, 1, name)
            
            # Column 2: Label
            label_text = r.get('label') or ''
            label = QTableWidgetItem(label_text)
            label.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            self.results.setItem(i, 2, label)
            
            # Column 3: Tags
            tags_val = r.get('tags') or ''
            if isinstance(tags_val, (list, tuple)):
                tags_val = ', '.join(tags_val)
            tags = QTableWidgetItem(tags_val)
            tags.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            self.results.setItem(i, 3, tags)
        
        # Auto-select first result only if nothing is currently selected
        # (Don't override user's selection when results refresh)
        if rows:
            if self.results.currentRow() < 0:
                self.results.selectRow(0)
            self.btn_fill.setEnabled(True)
            self.btn_copy_path.setEnabled(True)
        else:
            self.btn_fill.setEnabled(False)
            self.btn_copy_path.setEnabled(False)
        
        # If there's a pending query (user typed while searching), run it now
        if self._pending_query:
            pending = self._pending_query
            self._pending_query = None
            # Check if query still matches current input
            if pending == self.input.text().strip():
                self._search_worker.set_query(pending, limit=20)
                self._search_worker.start()
        
        # CRITICAL: Restore focus to input after populating results
        # This prevents the table/buttons from stealing focus
        self.input.setFocus()
    
    def _open_row(self, row: int):
        """Open the file at the specified row. Popup stays open to allow viewing multiple files."""
        try:
            if 0 <= row < len(self._rows):
                path = self._rows[row].get('file_path') or ''
                if path:
                    self.pathSelected.emit(path + "||OPEN")
                    # Don't hide - let user open multiple files
        except Exception as e:
            logger.error(f"Error opening row {row}: {e}")

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.hide()
            return
        if e.modifiers() == Qt.ControlModifier and e.key() == Qt.Key_O:
            self._open_selection()
            return
        # Don't let Enter close the dialog - we handle it via returnPressed signal
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            # If no selection, select first row first
            if self.results.currentRow() < 0 and self.results.rowCount() > 0:
                self.results.selectRow(0)
            self._accept_selection()
            return
        super().keyPressEvent(e)
    
    # === Dragging support for frameless window ===
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            # Only initiate drag if NOT clicking on interactive widgets (table, buttons, input)
            click_pos = event.position().toPoint()
            child = self.childAt(click_pos)
            
            # Check if click is on an interactive widget - if so, let it handle the event
            if child is not None:
                # Don't drag when clicking on table, buttons, or input
                widget = child
                while widget is not None:
                    if widget in (self.results, self.input, self.btn_fill, self.btn_copy_path, self.btn_close):
                        super().mousePressEvent(event)
                        return
                    # Also check for buttons inside the table (Open buttons)
                    if isinstance(widget, QPushButton):
                        super().mousePressEvent(event)
                        return
                    widget = widget.parent()
            
            # Click is on empty area - initiate drag
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
        else:
            super().mousePressEvent(event)
    
    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton and self._drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
        else:
            super().mouseMoveEvent(event)
    
    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)
    
    def eventFilter(self, obj, event):
        """Filter events on the table viewport to prevent unwanted selection changes."""
        from PySide6.QtCore import QEvent
        
        # Block mouse move events on the viewport that aren't button presses
        # This prevents hover-based selection changes
        if obj == self.results.viewport():
            if event.type() == QEvent.MouseMove:
                # Only allow mouse move if a button is pressed (for drag selection)
                if not event.buttons():
                    return True  # Block the event
        
        return super().eventFilter(obj, event)

    def _current_path(self) -> str:
        sel = self.results.currentRow()
        if sel < 0 or not hasattr(self, '_rows'):
            return ''
        try:
            return self._rows[sel].get('file_path') or ''
        except Exception:
            return ''

    def _copy_current_path(self):
        path = self._current_path()
        if not path:
            return
        try:
            clipboard = QGuiApplication.clipboard()
            clipboard.setText(path)
        except Exception as e:
            logger.error(f"Failed to copy path: {e}")

    def _accept_selection(self):
        path = self._current_path()
        if path:
            # Phase 4: Create comprehensive debug report before processing
            logger.info("[QS] === STARTING AUTOFILL SEQUENCE ===")
            self.create_comprehensive_debug_report()
            
            # Stop level enforcement timer before hiding (critical for macOS)
            if hasattr(self, '_level_timer'):
                self._level_timer.stop()
                logger.info("[QS] Stopped level enforcement timer")
            
            # Reset keyboard focus flag
            self._keyboard_focus_claimed = False
            
            # Hide the popup first
            self.hide()
            logger.info("[QS] Called hide() on popup")
            
            # IMPORTANT: Wait for Enter key to be fully released before restoring focus
            # Otherwise the Enter keypress leaks to the file dialog and briefly opens files
            import time
            time.sleep(0.15)  # Let Enter key release complete
            
            # Phase 2: Restore focus to the file dialog
            logger.info("[QS] Phase 2: Starting focus restoration")
            success, method = self.restore_dialog_focus(delay_ms=500)
            
            if success:
                logger.info(f"[QS] Focus restored successfully using {method}")
                # Log post-restoration state
                logger.info("[QS] === POST-RESTORATION STATE ===")
                self.log_debug_target_window()
            else:
                logger.warning(f"[QS] Focus restoration failed ({method})")
                # Still log current state for debugging
                logger.warning("[QS] === FAILED RESTORATION STATE ===")
                self.log_debug_target_window()
            
            # Emit the path for autofill processing (Phase 3 will handle it)
            logger.info(f"[QS] Emitting path for autofill: {path}")
            
            try:
                logger.info(f"[QS] *** About to emit pathSelected signal with: {path}")
                self.pathSelected.emit(path)
                logger.info(f"[QS] *** pathSelected signal emitted successfully")
            except Exception as e:
                logger.error(f"[QS] *** ERROR emitting pathSelected signal: {e}", exc_info=True)
            
            logger.info("[QS] === AUTOFILL SEQUENCE COMPLETE ===")

    def _open_selection(self):
        path = self._current_path()
        if path:
            self.pathSelected.emit(path + "||OPEN")
            self.hide()

    def _on_selection_changed(self):
        has_path = bool(self._current_path())
        self.btn_fill.setEnabled(has_path)
        self.btn_copy_path.setEnabled(has_path)

    def _on_cell_clicked(self, row: int, col: int):
        try:
            # Don't select row if clicking the open button column
            if col == 0:
                return
            self.results.selectRow(row)
            self.btn_fill.setEnabled(True)
            self.btn_copy_path.setEnabled(True)
        except Exception:
            pass


