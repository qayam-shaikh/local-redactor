from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

from PIL import Image
import pytesseract
from PySide6.QtCore import Qt, QRectF, Signal, QPointF
from PySide6.QtGui import QAction, QColor, QPainter, QPen, QPixmap, QImage, QBrush
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QGraphicsRectItem, QGraphicsScene,
    QGraphicsView, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton,
    QSplitter, QStatusBar, QToolButton, QVBoxLayout, QWidget, QLineEdit,
    QRadioButton, QButtonGroup, QSizePolicy, QScrollArea,
)

from engine import Document, Redaction, Region, export_image, export_pdf, apply_redactions, detection_stack_status

APP_NAME = "Local Redactor"


def pil_to_pixmap(image: Image.Image) -> QPixmap:
    rgb = image.convert("RGB")
    data = rgb.tobytes("raw", "RGB")
    qimage = QImage(data, rgb.width, rgb.height, rgb.width * 3, QImage.Format.Format_RGB888).copy()
    return QPixmap.fromImage(qimage)


class PreviewView(QGraphicsView):
    """Pixel-coordinate document canvas with reliable drag-to-redact support."""
    region_added = Signal(object)

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setBackgroundBrush(QColor("#e7eaf0"))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        # Important: do not let the page's pixmap item consume mouse events.
        # The view owns pointer handling so manual regions always reach our code.
        self.setInteractive(False)
        self.setMouseTracking(True)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self._manual = False
        self._start = None
        self._rubber = None
        self._image_size = (0, 0)
        self._zoom = 1.0

    def show_page(self, image: Image.Image, detections: list[Region], redactions: list[Redaction]):
        self.scene().clear()
        preview = apply_redactions(image, redactions)
        self._image_size = (preview.width, preview.height)
        self.scene().addPixmap(pil_to_pixmap(preview))
        for region in detections:
            item = QGraphicsRectItem(QRectF(region.x, region.y, region.w, region.h))
            item.setPen(QPen(QColor("#d18a12"), max(3, preview.width / 1000), Qt.PenStyle.DashLine))
            item.setBrush(QBrush(QColor(244, 183, 64, 30)))
            item.setZValue(4)
            self.scene().addItem(item)
        self.scene().setSceneRect(0, 0, preview.width, preview.height)
        self.fit_image()

    def fit_image(self):
        if self._image_size[0] > 0:
            self.resetTransform()
            self.fitInView(self.scene().sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
            self._zoom = self.transform().m11()

    def zoom_in(self):
        if self._image_size[0]:
            self.scale(1.18, 1.18)
            self._zoom *= 1.18

    def zoom_out(self):
        if self._image_size[0]:
            self.scale(1 / 1.18, 1 / 1.18)
            self._zoom /= 1.18

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._image_size[0] and not self._manual:
            self.fit_image()

    def set_manual_mode(self, enabled: bool):
        self._manual = enabled
        self.setCursor(Qt.CursorShape.CrossCursor if enabled else Qt.CursorShape.ArrowCursor)
        if not enabled and self._rubber is not None:
            self.scene().removeItem(self._rubber)
            self._rubber = None
            self._start = None

    def _scene_point(self, event):
        point = self.mapToScene(event.position().toPoint())
        bounds = self.scene().sceneRect()
        return QPointF(
            max(bounds.left(), min(bounds.right(), point.x())),
            max(bounds.top(), min(bounds.bottom(), point.y())),
        )

    def mousePressEvent(self, event):
        if self._manual and event.button() == Qt.MouseButton.LeftButton and self._image_size[0]:
            point = self._scene_point(event)
            if self.scene().sceneRect().contains(point):
                self._start = point
                self._rubber = QGraphicsRectItem()
                self._rubber.setPen(QPen(QColor("#5b78ee"), max(3, self._image_size[0] / 1000), Qt.PenStyle.DashLine))
                self._rubber.setBrush(QBrush(QColor(91, 120, 238, 48)))
                self._rubber.setZValue(20)
                self.scene().addItem(self._rubber)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._manual and self._start is not None and self._rubber is not None:
            current = self._scene_point(event)
            self._rubber.setRect(QRectF(self._start, current).normalized())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._manual and self._start is not None and self._rubber is not None and event.button() == Qt.MouseButton.LeftButton:
            rect = self._rubber.rect().normalized().intersected(self.scene().sceneRect())
            self.scene().removeItem(self._rubber)
            self._rubber = None
            self._start = None
            if rect.width() >= 4 and rect.height() >= 4:
                self.region_added.emit((rect.x(), rect.y(), rect.width(), rect.height()))
            event.accept()
            return
        super().mouseReleaseEvent(event)


class LocalRedactor(QMainWindow):
    def __init__(self):
        super().__init__()
        self.document: Document | None = None
        self.current_page = 0
        self.redaction_mode = "black"
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(1100, 720)
        self.resize(1450, 900)
        self.setAcceptDrops(True)
        self._build_ui()
        self._apply_style()
        self._set_enabled(False)

    def _build_ui(self):
        root = QWidget()
        root.setObjectName("Root")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        header = QFrame()
        header.setObjectName("Header")
        header.setFixedHeight(76)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(26, 12, 26, 12)
        header_layout.setSpacing(13)
        logo = QLabel("◈")
        logo.setObjectName("LogoMark")
        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        title = QLabel("Local Redactor")
        title.setObjectName("Brand")
        subtitle = QLabel("PRIVATE DOCUMENTS · PROCESSED ON THIS DEVICE")
        subtitle.setObjectName("HeaderSub")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header_layout.addWidget(logo)
        header_layout.addLayout(title_box)
        header_layout.addStretch()
        self.local_badge = QLabel("●  LOCAL ONLY")
        self.local_badge.setObjectName("LocalBadge")
        header_layout.addWidget(self.local_badge)
        root_layout.addWidget(header)

        body = QSplitter(Qt.Orientation.Horizontal)
        body.setObjectName("BodySplitter")
        body.setChildrenCollapsible(False)
        body.setHandleWidth(1)

        side = QWidget()
        side.setObjectName("Sidebar")
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(20, 20, 20, 18)
        side_layout.setSpacing(13)

        side_layout.addWidget(self.section_label("DOCUMENT"))
        doc_card = self.card()
        doc_layout = QVBoxLayout(doc_card)
        doc_layout.setContentsMargins(14, 14, 14, 14)
        doc_layout.setSpacing(9)
        self.open_btn = QPushButton("＋   Open image or PDF")
        self.open_btn.setObjectName("PrimaryButton")
        self.open_btn.setMinimumHeight(43)
        self.open_btn.clicked.connect(self.open_file)
        doc_layout.addWidget(self.open_btn)
        self.file_label = QLabel("No document loaded")
        self.file_label.setObjectName("FileName")
        self.file_label.setWordWrap(True)
        self.file_hint = QLabel("Drop a PDF or image here to get started")
        self.file_hint.setObjectName("Muted")
        doc_layout.addWidget(self.file_label)
        doc_layout.addWidget(self.file_hint)
        side_layout.addWidget(doc_card)

        side_layout.addWidget(self.section_label("DETECT & REVIEW"))
        scan_card = self.card()
        scan_layout = QVBoxLayout(scan_card)
        scan_layout.setContentsMargins(14, 14, 14, 14)
        scan_layout.setSpacing(9)
        scan_title = QLabel("Find sensitive information")
        scan_title.setObjectName("CardTitle")
        scan_copy = QLabel("Local OCR + PII recognition. Suggestions must be checked before export.")
        scan_copy.setObjectName("CardCopy")
        scan_copy.setWordWrap(True)
        scan_layout.addWidget(scan_title)
        scan_layout.addWidget(scan_copy)
        scan_buttons = QHBoxLayout()
        scan_buttons.setSpacing(7)
        self.detect_btn = QPushButton("This page")
        self.detect_btn.setObjectName("SecondaryButton")
        self.detect_btn.clicked.connect(self.detect_current)
        self.scan_all_btn = QPushButton("All pages")
        self.scan_all_btn.setObjectName("PrimaryButton")
        self.scan_all_btn.clicked.connect(self.detect_all_pages)
        scan_buttons.addWidget(self.detect_btn)
        scan_buttons.addWidget(self.scan_all_btn)
        scan_layout.addLayout(scan_buttons)
        self.detection_info = QLabel("No scan yet")
        self.detection_info.setObjectName("ScanResult")
        self.detection_info.setWordWrap(True)
        scan_layout.addWidget(self.detection_info)
        self.apply_btn = QPushButton("Apply all suggestions on this page")
        self.apply_btn.setObjectName("ApplyButton")
        self.apply_btn.clicked.connect(self.apply_suggestions)
        scan_layout.addWidget(self.apply_btn)
        side_layout.addWidget(scan_card)

        side_layout.addWidget(self.section_label("MANUAL REDACTION"))
        redact_card = self.card()
        redact_layout = QVBoxLayout(redact_card)
        redact_layout.setContentsMargins(14, 14, 14, 14)
        redact_layout.setSpacing(9)
        self.manual_btn = QPushButton("✎   Draw on document")
        self.manual_btn.setObjectName("DrawButton")
        self.manual_btn.setCheckable(True)
        self.manual_btn.setMinimumHeight(40)
        self.manual_btn.toggled.connect(self._manual_toggled)
        redact_layout.addWidget(self.manual_btn)
        self.manual_hint = QLabel("Drag across the page to cover any area. Release to apply.")
        self.manual_hint.setObjectName("CardCopy")
        self.manual_hint.setWordWrap(True)
        redact_layout.addWidget(self.manual_hint)
        mode_row = QHBoxLayout()
        mode_row.setSpacing(12)
        self.black_radio = QRadioButton("Solid black")
        self.pixel_radio = QRadioButton("Pixelate")
        self.black_radio.setChecked(True)
        self.black_radio.toggled.connect(lambda checked: self._set_mode("black") if checked else None)
        self.pixel_radio.toggled.connect(lambda checked: self._set_mode("pixelate") if checked else None)
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.black_radio)
        self.mode_group.addButton(self.pixel_radio)
        mode_row.addWidget(self.black_radio)
        mode_row.addWidget(self.pixel_radio)
        mode_row.addStretch()
        redact_layout.addLayout(mode_row)
        utility_row = QHBoxLayout()
        utility_row.setSpacing(7)
        self.undo_btn = QPushButton("Undo")
        self.undo_btn.setObjectName("SecondaryButton")
        self.undo_btn.clicked.connect(self.undo_redaction)
        self.clear_btn = QPushButton("Clear page")
        self.clear_btn.setObjectName("DangerButton")
        self.clear_btn.clicked.connect(self.clear_redactions)
        utility_row.addWidget(self.undo_btn)
        utility_row.addWidget(self.clear_btn)
        redact_layout.addLayout(utility_row)
        side_layout.addWidget(redact_card)

        side_layout.addWidget(self.section_label("PURPOSE WATERMARK"))
        watermark_card = self.card()
        wm_layout = QVBoxLayout(watermark_card)
        wm_layout.setContentsMargins(14, 14, 14, 14)
        wm_layout.setSpacing(8)
        self.purpose_edit = QLineEdit()
        self.purpose_edit.setPlaceholderText("e.g. FOR RENTAL KYC ONLY")
        self.date_edit = QLineEdit(date.today().strftime("%d %b %Y"))
        self.date_edit.setPlaceholderText("Date (optional)")
        wm_layout.addWidget(self.purpose_edit)
        wm_layout.addWidget(self.date_edit)
        side_layout.addWidget(watermark_card)

        side_layout.addStretch(1)
        self.export_btn = QPushButton("Export flattened document   →")
        self.export_btn.setObjectName("ExportButton")
        self.export_btn.setMinimumHeight(47)
        self.export_btn.clicked.connect(self.export_document)
        side_layout.addWidget(self.export_btn)
        security_note = QLabel("Export is rebuilt from page pixels. Original PDF text layers and source metadata are not carried over. Use solid black for sensitive data.")
        security_note.setObjectName("SecurityNote")
        security_note.setWordWrap(True)
        side_layout.addWidget(security_note)

        center = QWidget()
        center.setObjectName("CanvasArea")
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(22, 18, 22, 18)
        center_layout.setSpacing(13)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        page_box = QVBoxLayout()
        page_box.setSpacing(1)
        self.page_title = QLabel("Document preview")
        self.page_title.setObjectName("PageTitle")
        self.page_subtitle = QLabel("Open a document or drag it into this window")
        self.page_subtitle.setObjectName("Muted")
        page_box.addWidget(self.page_title)
        page_box.addWidget(self.page_subtitle)
        toolbar.addLayout(page_box)
        toolbar.addStretch()
        self.zoom_out_btn = QToolButton()
        self.zoom_out_btn.setText("−")
        self.zoom_out_btn.setToolTip("Zoom out")
        self.zoom_out_btn.clicked.connect(self._zoom_out)
        self.fit_btn = QPushButton("Fit page")
        self.fit_btn.setObjectName("ToolbarButton")
        self.fit_btn.clicked.connect(self._fit_page)
        self.zoom_in_btn = QToolButton()
        self.zoom_in_btn.setText("＋")
        self.zoom_in_btn.setToolTip("Zoom in")
        self.zoom_in_btn.clicked.connect(self._zoom_in)
        self.prev_btn = QToolButton()
        self.prev_btn.setText("‹")
        self.prev_btn.setToolTip("Previous page")
        self.prev_btn.clicked.connect(lambda: self.change_page(-1))
        self.page_count = QLabel("0 / 0")
        self.page_count.setObjectName("PageCount")
        self.next_btn = QToolButton()
        self.next_btn.setText("›")
        self.next_btn.setToolTip("Next page")
        self.next_btn.clicked.connect(lambda: self.change_page(1))
        for widget in (self.zoom_out_btn, self.fit_btn, self.zoom_in_btn, self.prev_btn, self.page_count, self.next_btn):
            toolbar.addWidget(widget)
        center_layout.addLayout(toolbar)

        preview_frame = QFrame()
        preview_frame.setObjectName("PreviewFrame")
        preview_layout = QVBoxLayout(preview_frame)
        preview_layout.setContentsMargins(10, 10, 10, 10)
        self.preview = PreviewView()
        self.preview.region_added.connect(self.add_manual_region)
        preview_layout.addWidget(self.preview)
        center_layout.addWidget(preview_frame, 1)

        footer = QHBoxLayout()
        self.status_label = QLabel("Ready · Files stay on this device")
        self.status_label.setObjectName("Muted")
        footer.addWidget(self.status_label)
        footer.addStretch()
        self.scan_summary = QLabel("PDF · PNG · JPG · WEBP")
        self.scan_summary.setObjectName("Muted")
        footer.addWidget(self.scan_summary)
        center_layout.addLayout(footer)

        side_scroll = QScrollArea()
        side_scroll.setWidgetResizable(True)
        side_scroll.setFrameShape(QFrame.Shape.NoFrame)
        side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        side_scroll.setWidget(side)
        side_scroll.setMinimumWidth(330)
        side_scroll.setMaximumWidth(390)
        side_scroll.setObjectName("SidebarScroll")
        body.addWidget(side_scroll)
        body.addWidget(center)
        body.setSizes([360, 1090])
        root_layout.addWidget(body, 1)
        self.setCentralWidget(root)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Offline processing · Nothing is uploaded")

        open_action = QAction("Open…", self)
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self.open_file)
        self.addAction(open_action)
        export_action = QAction("Export…", self)
        export_action.setShortcut("Ctrl+Shift+S")
        export_action.triggered.connect(self.export_document)
        self.addAction(export_action)
        undo_action = QAction("Undo redaction", self)
        undo_action.setShortcut("Ctrl+Z")
        undo_action.triggered.connect(self.undo_redaction)
        self.addAction(undo_action)

    def _apply_style(self):
        self.setStyleSheet("""
            * { font-family: 'Ubuntu', 'Noto Sans', sans-serif; font-size: 13px; }
            QMainWindow, #Root { background: #f3f4f7; }
            #Header { background: #20232d; border-bottom: 1px solid #171a22; }
            #LogoMark { color: #a99cff; font-size: 32px; font-weight: 700; }
            #Brand { color: #ffffff; font-size: 19px; font-weight: 750; }
            #HeaderSub { color: #a9adba; font-size: 9px; font-weight: 700; letter-spacing: 1.4px; }
            #LocalBadge { color: #b8f1d1; background: #274738; border: 1px solid #35664e; border-radius: 12px; padding: 7px 12px; font-size: 10px; font-weight: 800; }
            #Sidebar { background: #fafafd; }
            #SidebarScroll { background: #fafafd; border-right: 1px solid #e5e7ed; }
            QScrollArea#SidebarScroll > QWidget > QWidget { background: #fafafd; }
            #CanvasArea { background: #f0f1f5; }
            #Section { color: #848998; font-size: 10px; font-weight: 850; letter-spacing: 1.25px; }
            QFrame#Card { background: #ffffff; border: 1px solid #e5e6ed; border-radius: 12px; }
            #FileName { color: #252837; font-size: 13px; font-weight: 700; }
            #CardTitle { color: #272a38; font-size: 14px; font-weight: 750; }
            #CardCopy, #Muted { color: #858999; font-size: 11px; }
            #ScanResult { color: #53596a; background: #f4f5f9; border: 1px solid #e9eaf0; border-radius: 8px; padding: 8px 9px; font-size: 11px; }
            QPushButton { border: 1px solid #e1e3ea; border-radius: 8px; padding: 9px 10px; font-weight: 650; }
            QPushButton:disabled, QToolButton:disabled { color: #aeb1bc; background: #f0f1f4; border-color: #e8e9ee; }
            #PrimaryButton { background: #6453d8; color: #ffffff; border: 1px solid #6453d8; }
            #PrimaryButton:hover { background: #5545c5; border-color: #5545c5; }
            #SecondaryButton, #ToolbarButton { background: #f5f5fa; color: #3d4150; border-color: #e5e6ed; }
            #SecondaryButton:hover, #ToolbarButton:hover { background: #eeedf9; border-color: #d4d0f1; }
            #ApplyButton { background: #ece9ff; color: #5143b5; border: 1px solid #dcd7ff; }
            #ApplyButton:hover { background: #e2ddff; }
            #DrawButton { background: #eef1ff; color: #455bb8; border: 1px solid #d8defb; text-align: center; }
            #DrawButton:checked { background: #4f62cc; color: white; border-color: #4f62cc; }
            #DangerButton { background: #fff4f3; color: #b64f49; border-color: #f3d9d6; }
            #DangerButton:hover { background: #ffe9e7; }
            #ExportButton { background: #16845b; color: #ffffff; border: 1px solid #16845b; font-size: 14px; font-weight: 750; }
            #ExportButton:hover { background: #11734e; }
            QLineEdit { background: #fbfbfd; color: #292c39; border: 1px solid #e1e3eb; border-radius: 7px; padding: 9px 10px; selection-background-color: #6453d8; }
            QLineEdit:focus { border: 1px solid #8d80e9; background: #ffffff; }
            QRadioButton { color: #575c6b; spacing: 5px; font-size: 11px; }
            QRadioButton::indicator { width: 15px; height: 15px; }
            QToolButton { background: #ffffff; color: #4a4e5d; border: 1px solid #e2e4eb; border-radius: 7px; min-width: 30px; min-height: 29px; font-size: 20px; }
            QToolButton:hover { background: #f1efff; border-color: #d6d0fa; }
            #PageTitle { color: #282b39; font-size: 16px; font-weight: 750; }
            #PageCount { color: #6d7180; font-size: 11px; padding: 0 5px; }
            #PreviewFrame { background: #e3e6ec; border: 1px solid #d8dce5; border-radius: 12px; }
            QStatusBar { background: #fafafd; color: #777c8b; border-top: 1px solid #e4e6ed; }
            #SecurityNote { color: #777c8b; font-size: 10px; }
        """)

    def section_label(self, text: str):
        label = QLabel(text)
        label.setObjectName("Section")
        return label

    def card(self):
        frame = QFrame()
        frame.setObjectName("Card")
        frame.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        return frame

    def _manual_toggled(self, checked: bool):
        self.preview.set_manual_mode(checked)
        self.manual_btn.setText("✓   Drawing mode active" if checked else "✎   Draw on document")
        self.manual_hint.setText("Drag across the page, then release to apply a redaction." if checked else "Drag across the page to cover any area. Release to apply.")
        if checked:
            self.status_label.setText("Drawing mode · Drag a rectangle over the content to hide")

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            self.load_file(urls[0].toLocalFile())

    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open document", str(Path.home()),
            "Supported documents (*.pdf *.png *.jpg *.jpeg *.webp *.bmp);;All files (*)",
        )
        if path:
            self.load_file(path)

    def load_file(self, path: str):
        try:
            self.status_label.setText("Loading document…")
            QApplication.processEvents()
            self.document = Document(path)
            self.current_page = 0
            self.file_label.setText(Path(path).name)
            self.file_label.setToolTip(path)
            self.file_hint.setText(f"{len(self.document.pages)} page(s) · Original file is unchanged")
            self.status_label.setText("Document loaded · Nothing has been changed or uploaded")
            self.detection_info.setText(f"{detection_stack_status()} · review every suggestion before export")
            self.page_subtitle.setText(f"{Path(path).name} · {len(self.document.pages)} page(s)")
            self.manual_btn.setChecked(False)
            self._set_enabled(True)
            self.refresh_page()
        except Exception as exc:
            self.document = None
            self._set_enabled(False)
            QMessageBox.critical(self, "Could not open document", str(exc))
            self.status_label.setText("Could not load the selected document")

    def _set_enabled(self, enabled: bool):
        for widget in [
            self.detect_btn, self.scan_all_btn, self.apply_btn, self.manual_btn,
            self.undo_btn, self.clear_btn, self.export_btn, self.prev_btn,
            self.next_btn, self.zoom_in_btn, self.zoom_out_btn, self.fit_btn,
        ]:
            widget.setEnabled(enabled)

    def refresh_page(self):
        if not self.document:
            return
        page = self.document.pages[self.current_page]
        self.preview.show_page(page.image, page.detections, page.redactions)
        self.page_title.setText(self.document.path.name)
        self.page_count.setText(f"{self.current_page + 1} / {len(self.document.pages)}")
        self.prev_btn.setEnabled(self.current_page > 0)
        self.next_btn.setEnabled(self.current_page < len(self.document.pages) - 1)
        self.scan_summary.setText(f"{page.image.width:,} × {page.image.height:,} px  ·  {len(page.redactions)} redaction(s)")
        self.undo_btn.setEnabled(bool(page.redactions))
        self.clear_btn.setEnabled(bool(page.redactions))
        self.apply_btn.setEnabled(bool(page.detections))
        self.detection_info.setText(
            f"{len(page.detections)} suggestion(s) on this page · {len(page.redactions)} applied redaction(s)"
            if page.detections or page.redactions else "No suggestions yet · scan this page or the full document"
        )

    def change_page(self, delta: int):
        if not self.document:
            return
        self.current_page = max(0, min(len(self.document.pages) - 1, self.current_page + delta))
        self.manual_btn.setChecked(False)
        self.refresh_page()
        page = self.document.pages[self.current_page]
        self.status_label.setText(f"Page {self.current_page + 1} of {len(self.document.pages)} · {len(page.detections)} suggestion(s)")

    def detect_current(self):
        if not self.document:
            return
        self.detect_btn.setEnabled(False)
        self.status_label.setText("Scanning this page locally · OCR + PII recognition…")
        QApplication.processEvents()
        try:
            found = self.document.detect(self.current_page)
            self.refresh_page()
            labels = sorted({region.label for region in found})
            self.detection_info.setText(
                f"{len(found)} suggestion(s) · " + (", ".join(labels) if labels else "No obvious matches")
            )
            self.status_label.setText("Page scan complete · inspect every highlighted region")
        except pytesseract.TesseractNotFoundError:
            QMessageBox.warning(self, "OCR engine not found", "Install Tesseract with:\n\nsudo apt install tesseract-ocr\n\nThen restart Local Redactor.")
            self.status_label.setText("OCR unavailable · manual redaction is still available")
        except Exception as exc:
            QMessageBox.warning(self, "Scan failed", str(exc))
            self.status_label.setText("Scan failed · manual redaction is still available")
        finally:
            self.detect_btn.setEnabled(True)

    def detect_all_pages(self):
        if not self.document:
            return
        original_page = self.current_page
        total_pages = len(self.document.pages)
        total_found = 0
        self.detect_btn.setEnabled(False)
        self.scan_all_btn.setEnabled(False)
        try:
            for index in range(total_pages):
                self.status_label.setText(f"Scanning page {index + 1} of {total_pages} locally…")
                self.statusBar().showMessage(f"OCR + PII scan · page {index + 1} of {total_pages}")
                QApplication.processEvents()
                total_found += len(self.document.detect(index))
            self.current_page = original_page
            self.refresh_page()
            self.detection_info.setText(f"{total_found} suggestion(s) across {total_pages} page(s) · inspect each page before applying")
            self.status_label.setText(f"Document scan complete · {total_found} suggestion(s) across {total_pages} page(s)")
            self.statusBar().showMessage("Scan complete · review every page before export")
        except pytesseract.TesseractNotFoundError:
            QMessageBox.warning(self, "OCR engine not found", "Install Tesseract with:\n\nsudo apt install tesseract-ocr")
            self.status_label.setText("OCR unavailable · manual redaction is still available")
        except Exception as exc:
            QMessageBox.warning(self, "Scan failed", str(exc))
            self.status_label.setText("Scan failed · some earlier pages may already have suggestions")
        finally:
            self.current_page = original_page
            self.refresh_page()
            self.detect_btn.setEnabled(True)
            self.scan_all_btn.setEnabled(True)

    def apply_suggestions(self):
        if not self.document:
            return
        page = self.document.pages[self.current_page]
        if not page.detections:
            QMessageBox.information(self, "No suggestions", "Scan this page first. There are no detected regions to apply.")
            return
        count = len(page.detections)
        for region in page.detections:
            page.redactions.append(Redaction(region.x, region.y, region.w, region.h, self.redaction_mode))
        page.detections = []
        self.manual_btn.setChecked(False)
        self.refresh_page()
        self.status_label.setText(f"Applied {count} redaction(s) · inspect the page and add any missed areas")

    def add_manual_region(self, box):
        if not self.document:
            return
        x, y, w, h = box
        page = self.document.pages[self.current_page]
        # Clamp to source image bounds; this also protects exports from bad drag coords.
        x0 = max(0.0, min(float(page.image.width), float(x)))
        y0 = max(0.0, min(float(page.image.height), float(y)))
        x1 = max(x0, min(float(page.image.width), float(x + w)))
        y1 = max(y0, min(float(page.image.height), float(y + h)))
        if x1 - x0 < 3 or y1 - y0 < 3:
            self.status_label.setText("Redaction area too small · drag a larger rectangle")
            return
        page.redactions.append(Redaction(x0, y0, x1 - x0, y1 - y0, self.redaction_mode))
        self.manual_btn.setChecked(False)
        self.refresh_page()
        self.status_label.setText("Manual redaction applied · the covered pixels are hidden in the preview")

    def undo_redaction(self):
        if not self.document:
            return
        page = self.document.pages[self.current_page]
        if page.redactions:
            page.redactions.pop()
            self.refresh_page()
            self.status_label.setText("Last redaction removed")

    def clear_redactions(self):
        if not self.document:
            return
        page = self.document.pages[self.current_page]
        if page.redactions and QMessageBox.question(
            self, "Clear page redactions", "Remove all redactions from this page?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        page.redactions.clear()
        self.refresh_page()
        self.status_label.setText("Redactions cleared for this page")

    def _set_mode(self, mode):
        self.redaction_mode = mode

    def _zoom_in(self):
        self.preview.zoom_in()

    def _zoom_out(self):
        self.preview.zoom_out()

    def _fit_page(self):
        self.preview.fit_image()

    def export_document(self):
        if not self.document:
            return
        purpose = self.purpose_edit.text().strip()
        date_text = self.date_edit.text().strip()
        if not purpose:
            choice = QMessageBox.question(
                self, "No purpose watermark", "You have not entered a purpose watermark. Export without a watermark?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if choice != QMessageBox.StandardButton.Yes:
                self.purpose_edit.setFocus()
                return
        if self.document.kind == "pdf":
            default = self.document.path.with_name(self.document.path.stem + "_redacted.pdf")
            output, _ = QFileDialog.getSaveFileName(self, "Export flattened PDF", str(default), "PDF document (*.pdf)")
            if not output:
                return
            if not output.lower().endswith(".pdf"):
                output += ".pdf"
        else:
            default = self.document.path.with_name(self.document.path.stem + "_redacted.png")
            output, selected = QFileDialog.getSaveFileName(
                self, "Export redacted image", str(default),
                "PNG image (*.png);;JPEG image (*.jpg *.jpeg);;WebP image (*.webp)",
            )
            if not output:
                return
            if not Path(output).suffix:
                output += ".jpg" if "JPEG" in selected else ".webp" if "WebP" in selected else ".png"
        try:
            self.status_label.setText("Creating flattened export…")
            QApplication.processEvents()
            if self.document.kind == "pdf":
                export_pdf(self.document.pages, output, purpose, date_text)
            else:
                export_image(self.document.pages[0], output, purpose, date_text)
            QMessageBox.information(
                self, "Export complete",
                f"Saved locally:\n{output}\n\nThe export contains flattened page pixels. Source metadata was not copied.",
            )
            self.status_label.setText(f"Exported · {Path(output).name}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            self.status_label.setText("Export failed")


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("LocalTools")
    app.setStyle("Fusion")
    window = LocalRedactor()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
