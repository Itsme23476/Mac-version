"""Thin line-icons drawn with QPainter (no QtSvg / no bundled assets, so they render
identically in dev and in the frozen app). Visual-only helper for the UI redesign.

Usage:
    from app.ui.icons import line_icon, line_pixmap
    label.setPixmap(line_pixmap("sparkle", 16, "#7C4DFF"))
    button.setIcon(line_icon("search", on_color="#7C4DFF", off_color="#8C8AA0"))
"""
import math
from PySide6.QtCore import Qt, QRectF, QLineF
from PySide6.QtGui import QPixmap, QPainter, QPen, QColor, QPainterPath, QIcon

ACCENT = "#7C4DFF"


# --- individual icon drawers (24x24 logical coordinate box) ---------------

def _search(p):
    p.drawEllipse(QRectF(4, 4, 12, 12))
    p.drawLine(QLineF(14.6, 14.6, 20, 20))

def _folder(p):
    path = QPainterPath()
    path.moveTo(3, 18.5); path.lineTo(3, 7); path.lineTo(9, 7)
    path.lineTo(11, 9); path.lineTo(21, 9); path.lineTo(21, 18.5)
    path.closeSubpath()
    p.drawPath(path)

def _layers(p):
    p.drawRoundedRect(QRectF(4, 3, 16, 18), 2, 2)
    p.drawLine(QLineF(7.5, 8, 16.5, 8))
    p.drawLine(QLineF(7.5, 12, 16.5, 12))
    p.drawLine(QLineF(7.5, 16, 13, 16))

def _mic(p):
    p.drawRoundedRect(QRectF(9, 3, 6, 11), 3, 3)
    p.drawArc(QRectF(5, 4, 14, 14), 180 * 16, 180 * 16)   # bottom cradle
    p.drawLine(QLineF(12, 18, 12, 21.2))
    p.drawLine(QLineF(8.5, 21.2, 15.5, 21.2))

def _gear(p):
    p.drawEllipse(QRectF(8.7, 8.7, 6.6, 6.6))
    for i in range(8):
        a = math.radians(i * 45)
        c, s = math.cos(a), math.sin(a)
        p.drawLine(QLineF(12 + 5 * c, 12 + 5 * s, 12 + 8 * c, 12 + 8 * s))

def _appearance(p):      # half-filled circle (light/dark / theme)
    p.drawEllipse(QRectF(4, 4, 16, 16))
    half = QPainterPath()
    half.moveTo(12, 4)
    half.arcTo(QRectF(4, 4, 16, 16), 90, -180)
    half.closeSubpath()
    p.fillPath(half, p.pen().color())

def _book(p):
    p.drawRoundedRect(QRectF(6, 3.5, 12, 17), 1.5, 1.5)
    p.drawLine(QLineF(9.5, 3.8, 9.5, 20.2))
    p.drawLine(QLineF(12, 7.5, 15.5, 7.5))
    p.drawLine(QLineF(12, 10.5, 15.5, 10.5))

def _chat(p):
    p.drawRoundedRect(QRectF(3, 4, 18, 12.5), 4, 4)
    tail = QPainterPath()
    tail.moveTo(8, 16); tail.lineTo(8, 20.5); tail.lineTo(12.5, 16)
    p.drawPath(tail)

def _sparkle(p):
    path = QPainterPath()
    path.moveTo(12, 3); path.lineTo(13.6, 10.4); path.lineTo(21, 12)
    path.lineTo(13.6, 13.6); path.lineTo(12, 21); path.lineTo(10.4, 13.6)
    path.lineTo(3, 12); path.lineTo(10.4, 10.4); path.closeSubpath()
    p.drawPath(path)

def _user(p):
    p.drawEllipse(QRectF(8, 4, 8, 8))
    p.drawArc(QRectF(4.5, 14, 15, 15), 25 * 16, 130 * 16)

def _shield(p):
    path = QPainterPath()
    path.moveTo(12, 3); path.lineTo(20, 6); path.lineTo(20, 11.5)
    path.lineTo(12, 21); path.lineTo(4, 11.5); path.lineTo(4, 6)
    path.closeSubpath()
    p.drawPath(path)

def _globe(p):
    p.drawEllipse(QRectF(3, 3, 18, 18))
    p.drawLine(QLineF(3, 12, 21, 12))
    p.drawEllipse(QRectF(8, 3, 8, 18))

def _mute(p):
    sp = QPainterPath()
    sp.moveTo(4, 9.5); sp.lineTo(7, 9.5); sp.lineTo(11, 6)
    sp.lineTo(11, 18); sp.lineTo(7, 14.5); sp.lineTo(4, 14.5)
    sp.closeSubpath()
    p.drawPath(sp)
    p.drawLine(QLineF(15, 9, 20, 15))
    p.drawLine(QLineF(20, 9, 15, 15))

def _clock(p):
    p.drawEllipse(QRectF(3, 3, 18, 18))
    p.drawLine(QLineF(12, 7.5, 12, 12))
    p.drawLine(QLineF(12, 12, 15.5, 14))

def _refresh(p):
    path = QPainterPath()
    path.arcMoveTo(QRectF(5, 5, 14, 14), 70)
    path.arcTo(QRectF(5, 5, 14, 14), 70, 250)
    p.drawPath(path)
    a = math.radians(70)
    sx, sy = 12 + 7 * math.cos(a), 12 - 7 * math.sin(a)
    p.drawLine(QLineF(sx, sy, sx - 3.2, sy - 1.2))
    p.drawLine(QLineF(sx, sy, sx - 0.6, sy + 3.1))

def _type(p):
    p.drawLine(QLineF(6, 6.5, 18, 6.5))
    p.drawLine(QLineF(12, 6.5, 12, 18))
    p.drawLine(QLineF(9.5, 18, 14.5, 18))


_DRAW = {
    "search": _search, "folder": _folder, "layers": _layers, "mic": _mic,
    "gear": _gear, "appearance": _appearance, "book": _book, "chat": _chat,
    "sparkle": _sparkle, "user": _user, "shield": _shield, "globe": _globe,
    "mute": _mute, "clock": _clock, "refresh": _refresh, "type": _type,
}

_cache = {}


def line_pixmap(name: str, size: int = 16, color: str = ACCENT) -> QPixmap:
    """Return a crisp (2x) transparent pixmap of the named line-icon."""
    key = (name, size, color)
    if key in _cache:
        return _cache[key]
    drawer = _DRAW.get(name)
    ratio = 2
    px = max(2, int(round(size * ratio)))
    pm = QPixmap(px, px)
    pm.fill(Qt.transparent)
    if drawer is not None:
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.scale(px / 24.0, px / 24.0)
        pen = QPen(QColor(color))
        pen.setWidthF(2.0)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        try:
            drawer(p)
        finally:
            p.end()
    pm.setDevicePixelRatio(ratio)
    _cache[key] = pm
    return pm


def line_icon(name: str, size: int = 18, on_color: str = ACCENT,
              off_color: str = None) -> QIcon:
    """QIcon for a (possibly checkable) button. off_color is used for the
    unchecked/normal state, on_color for the checked/selected state."""
    icon = QIcon()
    off = off_color or on_color
    icon.addPixmap(line_pixmap(name, size, off), QIcon.Normal, QIcon.Off)
    icon.addPixmap(line_pixmap(name, size, on_color), QIcon.Normal, QIcon.On)
    icon.addPixmap(line_pixmap(name, size, on_color), QIcon.Active, QIcon.Off)
    icon.addPixmap(line_pixmap(name, size, on_color), QIcon.Selected, QIcon.On)
    return icon
