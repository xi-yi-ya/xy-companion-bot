"""Small original speech-bubble icon, drawn locally with Qt."""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

COLORS = {
    "running": "#567d60",
    "busy": "#d49a47",
    "starting": "#d49a47",
    "stopped": "#89938c",
    "error": "#c26959",
}


def bot_icon(state="running", size=64):
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / 64, size / 64)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#345943"))
    painter.drawRoundedRect(QRectF(2, 2, 60, 60), 18, 18)
    painter.setBrush(QColor("#f4f4e7"))
    bubble = QPainterPath()
    bubble.addRoundedRect(QRectF(13, 15, 38, 29), 12, 12)
    bubble.moveTo(22, 41)
    bubble.lineTo(18, 50)
    bubble.lineTo(33, 42)
    bubble.closeSubpath()
    painter.drawPath(bubble)
    painter.setBrush(QColor("#345943"))
    painter.drawEllipse(QPointF(25, 29), 2.1, 2.8)
    painter.drawEllipse(QPointF(39, 29), 2.1, 2.8)
    painter.setPen(QPen(QColor("#fffdf7"), 2.5))
    painter.setBrush(QColor(COLORS.get(state, COLORS["stopped"])))
    painter.drawEllipse(QPointF(53, 53), 8, 8)
    painter.end()
    return QIcon(pixmap)
