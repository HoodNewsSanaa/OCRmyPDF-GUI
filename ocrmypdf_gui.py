#!/usr/bin/env python3
"""A simple desktop GUI for OCRmyPDF.

Screens:
  1. Drop / select files (PDFs or images, drag & drop supported)
  2. Options (all commonly used OCRmyPDF settings)
  3. Progress (overall %, per-file stage, elapsed time and ETA, log)

OCRmyPDF runs in a separate process (``python -m ocrmypdf``) with a small
plugin (gui_progress_plugin.py) that reports progress back as JSON lines.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QProcess, QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtGui import QDesktopServices, QFont, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "OCRmyPDF GUI"
HERE = Path(__file__).resolve().parent
PLUGIN_PATH = HERE / "gui_progress_plugin.py"
ICON_SVG = HERE / "icon.svg"
ICON_PNG = HERE / "icon.png"
MARKER = "@@OCRGUI "

PDF_EXTS = {".pdf"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".jp2", ".webp"}
ACCEPTED_EXTS = PDF_EXTS | IMAGE_EXTS

EXIT_CODE_MESSAGES = {
    1: "Invalid arguments",
    2: "Input file is not a valid PDF or image",
    3: "A required dependency is missing (e.g. tesseract, ghostscript, unpaper)",
    4: "Output PDF is invalid",
    5: "File access error (permissions, or output already exists)",
    6: "Page already contains text - choose 'Skip pages with text', 'Redo OCR' or 'Force OCR'",
    7: "A child process (tesseract/ghostscript) failed",
    8: "PDF is encrypted / password-protected",
    9: "Invalid configuration",
    10: "PDF/A conversion failed (a regular PDF was produced)",
    15: "Unexpected error",
    130: "Cancelled",
}

# (label, value) pairs for combo boxes
MODES = [
    ("Normal - error if a page already has text", "default"),
    ("Skip pages that already have text", "skip"),
    ("Redo OCR - replace existing OCR text layer", "redo"),
    ("Force OCR - rasterize everything, keep links", "force"),
    ("Force OCR - rasterize everything, drop links", "force-ocr-no-links"),
    ("Strip OCR text layer only (no OCR)", "strip"),
]
OUTPUT_TYPES = [
    ("PDF/A (archival, recommended)", "pdfa"),
    ("PDF/A-1b", "pdfa-1"),
    ("PDF/A-2b", "pdfa-2"),
    ("PDF/A-3b", "pdfa-3"),
    ("Standard PDF", "pdf"),
    ("Auto (PDF/A if possible)", "auto"),
    ("None (sidecar text only)", "none"),
]
OPTIMIZE_LEVELS = [
    ("0 - No optimization", 0),
    ("1 - Lossless (safe)", 1),
    ("2 - Lossy, some compression", 2),
    ("3 - Lossy, aggressive", 3),
]
RENDERERS = [("Auto", "auto"), ("hOCR", "hocr"), ("Sandwich", "sandwich"), ("fpdf2", "fpdf2")]
RASTERIZERS = [("Auto", "auto"), ("Ghostscript", "ghostscript"), ("pypdfium", "pypdfium")]
OCR_ENGINES = [("Auto", "auto"), ("Tesseract", "tesseract"), ("None (no OCR)", "none")]
PSM_MODES = [
    ("Default", None),
    ("0 - Orientation & script detection only", 0),
    ("1 - Automatic with OSD", 1),
    ("3 - Fully automatic (Tesseract default)", 3),
    ("4 - Single column of variable sizes", 4),
    ("6 - Single uniform block of text", 6),
    ("11 - Sparse text", 11),
    ("12 - Sparse text with OSD", 12),
]
OEM_MODES = [
    ("Default", None),
    ("0 - Legacy engine", 0),
    ("1 - LSTM neural net", 1),
    ("2 - Legacy + LSTM", 2),
    ("3 - Default available", 3),
]
COLOR_STRATEGIES = [
    ("Default", None),
    ("LeaveColorUnchanged", "LeaveColorUnchanged"),
    ("RGB", "RGB"),
    ("CMYK", "CMYK"),
    ("Gray", "Gray"),
    ("UseDeviceIndependentColor", "UseDeviceIndependentColor"),
]

# --rotate-pages-threshold presets: how sure Tesseract must be before a page is rotated.
# 14 is OCRmyPDF's own default; lower rotates more pages but risks wrong flips.
ROTATION_LEVELS = [
    ("Conservative", 14.0),
    ("Moderate", 8.0),
    ("Aggressive", 4.0),
    ("Very aggressive", 2.0),
]
ROTATION_DEFAULT = 14.0

# Where each OCRmyPDF stage sits on a single file's 0..100% scale.
# OCR is by far the slowest step, so it gets the largest share.
STAGE_BANDS = {
    "Scanning contents": (0, 5),
    "OCR": (5, 82),
    "Image processing": (5, 82),
    "hOCR": (5, 82),
    "Grafting hOCR to PDF": (82, 86),
    "PDF/A conversion": (82, 92),
    "Linearizing": (92, 94),
    "JBIG2": (94, 98),
    "Recompressing JPEGs": (94, 98),
    "Deflating JPEGs": (94, 98),
    "PNGs": (94, 98),
    "Checking output": (98, 100),
}


# --------------------------------------------------------------------------- utils


def human_time(seconds: float | None) -> str:
    if seconds is None or seconds != seconds or seconds < 0:
        return "--:--"
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


TESSDATA_URL = "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/{}.traineddata"

LANGUAGE_NAMES = {
    "afr": "Afrikaans", "amh": "Amharic", "ara": "Arabic", "asm": "Assamese", "aze": "Azerbaijani",
    "aze_cyrl": "Azerbaijani (Cyrillic)", "bel": "Belarusian", "ben": "Bengali", "bod": "Tibetan",
    "bos": "Bosnian", "bre": "Breton", "bul": "Bulgarian", "cat": "Catalan", "ceb": "Cebuano",
    "ces": "Czech", "chi_sim": "Chinese (Simplified)", "chi_sim_vert": "Chinese (Simplified, vertical)",
    "chi_tra": "Chinese (Traditional)", "chi_tra_vert": "Chinese (Traditional, vertical)",
    "chr": "Cherokee", "cos": "Corsican", "cym": "Welsh", "dan": "Danish", "deu": "German",
    "div": "Dhivehi", "dzo": "Dzongkha", "ell": "Greek", "eng": "English", "enm": "Middle English",
    "epo": "Esperanto", "est": "Estonian", "eus": "Basque", "fao": "Faroese", "fas": "Persian",
    "fil": "Filipino", "fin": "Finnish", "fra": "French", "frk": "German (Fraktur)",
    "frm": "Middle French", "fry": "Western Frisian", "gla": "Scottish Gaelic", "gle": "Irish",
    "glg": "Galician", "grc": "Ancient Greek", "guj": "Gujarati", "hat": "Haitian Creole",
    "heb": "Hebrew", "hin": "Hindi", "hrv": "Croatian", "hun": "Hungarian", "hye": "Armenian",
    "iku": "Inuktitut", "ind": "Indonesian", "isl": "Icelandic", "ita": "Italian",
    "ita_old": "Italian (old)", "jav": "Javanese", "jpn": "Japanese", "jpn_vert": "Japanese (vertical)",
    "kan": "Kannada", "kat": "Georgian", "kat_old": "Georgian (old)", "kaz": "Kazakh", "khm": "Khmer",
    "kir": "Kyrgyz", "kmr": "Kurmanji Kurdish", "kor": "Korean", "kor_vert": "Korean (vertical)",
    "lao": "Lao", "lat": "Latin", "lav": "Latvian", "lit": "Lithuanian", "ltz": "Luxembourgish",
    "mal": "Malayalam", "mar": "Marathi", "mkd": "Macedonian", "mlt": "Maltese", "mon": "Mongolian",
    "mri": "Maori", "msa": "Malay", "mya": "Burmese", "nep": "Nepali", "nld": "Dutch",
    "nor": "Norwegian", "oci": "Occitan", "ori": "Odia", "pan": "Punjabi", "pol": "Polish",
    "por": "Portuguese", "pus": "Pashto", "que": "Quechua", "ron": "Romanian", "rus": "Russian",
    "san": "Sanskrit", "sin": "Sinhala", "slk": "Slovak", "slv": "Slovenian", "snd": "Sindhi",
    "spa": "Spanish", "spa_old": "Spanish (old)", "sqi": "Albanian", "srp": "Serbian",
    "srp_latn": "Serbian (Latin)", "sun": "Sundanese", "swa": "Swahili", "swe": "Swedish",
    "syr": "Syriac", "tam": "Tamil", "tat": "Tatar", "tel": "Telugu", "tgk": "Tajik", "tha": "Thai",
    "tir": "Tigrinya", "ton": "Tongan", "tur": "Turkish", "uig": "Uyghur", "ukr": "Ukrainian",
    "urd": "Urdu", "uzb": "Uzbek", "uzb_cyrl": "Uzbek (Cyrillic)", "vie": "Vietnamese",
    "yid": "Yiddish", "yor": "Yoruba",
}


def lang_label(code: str) -> str:
    name = LANGUAGE_NAMES.get(code)
    return f"{name} ({code})" if name else code


def user_tessdata_dir() -> Path | None:
    """Writable tessdata folder, only used when running as a bundle (e.g. Flatpak).

    The launcher sets OCRMYPDF_GUI_BUNDLED_TESSDATA to the read-only folder with the
    languages shipped in the bundle. Tesseract only reads one folder, so we keep a
    writable one in the user's data dir, link the bundled languages into it and
    download extra languages there.
    """
    if not os.environ.get("OCRMYPDF_GUI_BUNDLED_TESSDATA"):
        return None
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return base / "tessdata"


def setup_tessdata() -> None:
    bundled = os.environ.get("OCRMYPDF_GUI_BUNDLED_TESSDATA")
    target = user_tessdata_dir()
    if not bundled or target is None:
        return
    target.mkdir(parents=True, exist_ok=True)
    # Link everything: the language files and also the configs/ folder (hocr, txt, …)
    # that tesseract needs to produce its output.
    for f in Path(bundled).iterdir():
        link = target / f.name
        if not link.exists():
            if link.is_symlink():  # dangling link from an older bundle
                link.unlink()
            link.symlink_to(f)
    # Inherited by tesseract and by the ocrmypdf child processes.
    os.environ["TESSDATA_PREFIX"] = str(target)


def tesseract_languages() -> list[str]:
    exe = shutil.which("tesseract")
    if not exe:
        return []
    try:
        out = subprocess.run(
            [exe, "--list-langs"], capture_output=True, text=True, timeout=15
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    langs = [ln.strip() for ln in out.splitlines()[1:] if ln.strip()]
    return sorted(lang for lang in langs if lang != "osd")


def page_count(path: Path) -> int:
    """Best-effort page count, used to weight files for the overall progress."""
    if path.suffix.lower() != ".pdf":
        return 1
    try:
        import pikepdf

        with pikepdf.open(path) as pdf:
            return max(1, len(pdf.pages))
    except Exception:
        return 1


def app_icon() -> QIcon:
    icon = QIcon()
    if ICON_SVG.exists():
        icon.addFile(str(ICON_SVG))
    if ICON_PNG.exists():
        icon.addFile(str(ICON_PNG))
    return icon


def combo(items, current=None) -> QComboBox:
    cb = QComboBox()
    for label, value in items:
        cb.addItem(label, value)
    if current is not None:
        idx = cb.findData(current)
        if idx >= 0:
            cb.setCurrentIndex(idx)
    return cb


class LanguageDownloadDialog(QDialog):
    """Lets the user pick extra Tesseract languages and downloads them."""

    def __init__(self, parent, installed: set[str], target: Path):
        super().__init__(parent)
        self.setWindowTitle("Get more languages")
        self.resize(420, 520)
        self.target = target
        self.downloaded: list[str] = []
        self.nam = QNetworkAccessManager(self)
        self.queue: list[str] = []
        self.reply: QNetworkReply | None = None

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Tick the languages to download (about 1–10 MB each):"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search…")
        self.search.textChanged.connect(self._filter)
        lay.addWidget(self.search)
        self.list = QListWidget()
        for code, name in sorted(LANGUAGE_NAMES.items(), key=lambda kv: kv[1]):
            if code in installed:
                continue
            item = QListWidgetItem(f"{name} ({code})")
            item.setData(Qt.UserRole, code)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.list.addItem(item)
        lay.addWidget(self.list, 1)
        self.status = QLabel("")
        self.bar = QProgressBar()
        self.bar.hide()
        lay.addWidget(self.status)
        lay.addWidget(self.bar)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.download_btn = self.buttons.addButton("Download", QDialogButtonBox.AcceptRole)
        self.download_btn.clicked.connect(self._start)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)

    def _filter(self, text):
        text = text.lower()
        for i in range(self.list.count()):
            item = self.list.item(i)
            item.setHidden(text not in item.text().lower())

    def _start(self):
        self.queue = [
            self.list.item(i).data(Qt.UserRole)
            for i in range(self.list.count())
            if self.list.item(i).checkState() == Qt.Checked
        ]
        if not self.queue:
            self.accept()
            return
        self.download_btn.setEnabled(False)
        self.list.setEnabled(False)
        self.search.setEnabled(False)
        self.bar.show()
        self._next()

    def _next(self):
        if not self.queue:
            self.accept()
            return
        code = self.queue.pop(0)
        self.status.setText(f"Downloading {lang_label(code)}…")
        self.bar.setValue(0)
        req = QNetworkRequest(QUrl(TESSDATA_URL.format(code)))
        req.setAttribute(QNetworkRequest.RedirectPolicyAttribute,
                         QNetworkRequest.NoLessSafeRedirectPolicy)
        self.reply = self.nam.get(req)
        self.reply.downloadProgress.connect(
            lambda got, total: self.bar.setValue(int(got * 100 / total)) if total > 0 else None)
        self.reply.finished.connect(lambda code=code: self._done(code))

    def _done(self, code):
        reply, self.reply = self.reply, None
        reply.deleteLater()
        if reply.error() != QNetworkReply.NoError:
            QMessageBox.warning(self, "Download failed",
                                f"Could not download {lang_label(code)}:\n{reply.errorString()}")
        else:
            data = bytes(reply.readAll())
            tmp = self.target / f".{code}.traineddata.part"
            tmp.write_bytes(data)
            tmp.replace(self.target / f"{code}.traineddata")
            self.downloaded.append(code)
        self._next()

    def reject(self):
        if self.reply:
            self.queue.clear()
            self.reply.abort()
        super().reject()


# --------------------------------------------------------------------- drop page


class DropZone(QFrame):
    clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("dropZone")
        self.setMinimumHeight(260)
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignCenter)
        icon = QLabel()
        icon.setPixmap(app_icon().pixmap(96, 96))
        icon.setAlignment(Qt.AlignCenter)
        icon.setStyleSheet("border: none;")
        title = QLabel("Drop PDF or image files here")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 18px; font-weight: 600; border: none;")
        sub = QLabel("or click to browse")
        sub.setAlignment(Qt.AlignCenter)
        sub.setStyleSheet("color: palette(mid); border: none;")
        lay.addWidget(icon)
        lay.addWidget(title)
        lay.addWidget(sub)
        self.set_hover(False)

    def set_hover(self, hover: bool):
        color = "#3d8bfd" if hover else "#9aa0a6"
        bg = "rgba(61,139,253,0.08)" if hover else "transparent"
        self.setStyleSheet(
            f"#dropZone {{ border: 3px dashed {color}; border-radius: 14px; background: {bg}; }}"
        )

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()


class SelectPage(QWidget):
    def __init__(self, main: "MainWindow"):
        super().__init__()
        self.main = main
        lay = QVBoxLayout(self)
        lay.setContentsMargins(30, 30, 30, 30)
        header = QLabel("OCRmyPDF")
        header.setStyleSheet("font-size: 26px; font-weight: 700;")
        desc = QLabel("Add a searchable text layer to scanned PDFs and images.")
        desc.setStyleSheet("color: palette(mid);")
        lay.addWidget(header)
        lay.addWidget(desc)
        lay.addSpacing(16)
        self.zone = DropZone()
        self.zone.clicked.connect(main.browse_files)
        lay.addWidget(self.zone, 1)
        if not shutil.which("tesseract"):
            warn = QLabel(
                "⚠ Tesseract was not found on your PATH. OCRmyPDF needs Tesseract "
                "and Ghostscript installed to work."
            )
            warn.setWordWrap(True)
            warn.setStyleSheet("color: #c0392b;")
            lay.addWidget(warn)


# ------------------------------------------------------------------ options page


class OptionsPage(QWidget):
    def __init__(self, main: "MainWindow"):
        super().__init__()
        self.main = main
        self.settings = main.settings
        root = QVBoxLayout(self)

        # ---- file list
        files_box = QGroupBox("Files")
        fl = QVBoxLayout(files_box)
        self.file_list = QListWidget()
        self.file_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.file_list.setMaximumHeight(130)
        fl.addWidget(self.file_list)
        row = QHBoxLayout()
        add_btn = QPushButton("Add files…")
        add_btn.clicked.connect(main.browse_files)
        rm_btn = QPushButton("Remove selected")
        rm_btn.clicked.connect(self.remove_selected)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(main.clear_files)
        row.addWidget(add_btn)
        row.addWidget(rm_btn)
        row.addWidget(clear_btn)
        row.addStretch()
        fl.addLayout(row)
        root.addWidget(files_box)

        # ---- option tabs
        tabs = QTabWidget()
        tabs.addTab(self._build_basic_tab(), "Basic")
        tabs.addTab(self._build_image_tab(), "Image processing")
        tabs.addTab(self._build_output_tab(), "Output && optimization")
        tabs.addTab(self._build_metadata_tab(), "Metadata")
        tabs.addTab(self._build_advanced_tab(), "Advanced")
        root.addWidget(tabs, 1)

        # ---- command preview
        self.cmd_preview = QLineEdit()
        self.cmd_preview.setReadOnly(True)
        self.cmd_preview.setFont(QFont("monospace", 9))
        prev_row = QHBoxLayout()
        prev_row.addWidget(QLabel("Command:"))
        prev_row.addWidget(self.cmd_preview, 1)
        root.addLayout(prev_row)

        # ---- buttons
        btns = QHBoxLayout()
        reset_btn = QPushButton("Reset to defaults")
        reset_btn.clicked.connect(self.reset_defaults)
        btns.addWidget(reset_btn)
        btns.addStretch()
        self.start_btn = QPushButton("Start OCR ▶")
        self.start_btn.setDefault(True)
        self.start_btn.setMinimumHeight(36)
        self.start_btn.setStyleSheet("font-weight: 600; padding: 4px 22px;")
        self.start_btn.clicked.connect(main.start)
        btns.addWidget(self.start_btn)
        root.addLayout(btns)

        self._connect_preview(self)
        self.update_preview()

    # ----- tabs
    def _build_basic_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        s = self.settings

        # Languages
        self.lang_list = QListWidget()
        self.lang_list.setMaximumHeight(120)
        saved = set((s.value("languages", "eng") or "eng").split("+"))
        self.populate_languages(saved)
        if user_tessdata_dir() is not None:
            more = "Click \u201cGet more languages\u201d to add others."
        else:
            more = "Install more with your package manager (e.g. tesseract-ocr-deu)."
        lang_help = QLabel("Tick every language that appears in the document. " + more)
        lang_help.setWordWrap(True)
        lang_help.setStyleSheet("color: palette(mid); font-size: 11px;")
        lang_box = QVBoxLayout()
        lang_box.addWidget(self.lang_list)
        lang_box.addWidget(lang_help)
        if user_tessdata_dir() is not None:
            get_more = QPushButton("Get more languages…")
            get_more.clicked.connect(self.download_languages)
            lang_row = QHBoxLayout()
            lang_row.addWidget(get_more)
            lang_row.addStretch()
            lang_box.addLayout(lang_row)
        form.addRow("Languages:", lang_box)

        self.mode = combo(MODES, s.value("mode", "skip"))
        self.mode.setToolTip("What to do with pages that already contain text.")
        form.addRow("Existing text:", self.mode)

        self.output_type = combo(OUTPUT_TYPES, s.value("output_type", "pdfa"))
        form.addRow("Output type:", self.output_type)

        # Output location
        out_row = QHBoxLayout()
        self.out_same = QCheckBox("Same folder as input")
        self.out_same.setChecked(s.value("out_same", True, type=bool))
        self.out_dir = QLineEdit(s.value("out_dir", ""))
        self.out_dir.setPlaceholderText("Output folder")
        out_browse = QPushButton("…")
        out_browse.setFixedWidth(32)
        out_browse.clicked.connect(self._pick_out_dir)
        self.out_same.toggled.connect(lambda on: (self.out_dir.setEnabled(not on), out_browse.setEnabled(not on)))
        self.out_dir.setEnabled(not self.out_same.isChecked())
        out_browse.setEnabled(not self.out_same.isChecked())
        out_row.addWidget(self.out_same)
        out_row.addWidget(self.out_dir, 1)
        out_row.addWidget(out_browse)
        form.addRow("Save to:", out_row)

        self.suffix = QLineEdit(s.value("suffix", "_ocr"))
        self.suffix.setToolTip("Appended to each file name, e.g. scan.pdf → scan_ocr.pdf. Leave empty to keep the name (requires a different output folder).")
        form.addRow("File name suffix:", self.suffix)

        self.overwrite = QCheckBox("Overwrite existing output files")
        self.overwrite.setChecked(s.value("overwrite", False, type=bool))
        form.addRow("", self.overwrite)

        self.sidecar = QCheckBox("Also save recognized text as a .txt file (sidecar)")
        self.sidecar.setChecked(s.value("sidecar", False, type=bool))
        form.addRow("", self.sidecar)

        self.pages = QLineEdit(s.value("pages", ""))
        self.pages.setPlaceholderText("All pages (e.g. 1,3,5-9)")
        form.addRow("Pages to OCR:", self.pages)
        return w

    def _build_image_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        s = self.settings
        has_unpaper = bool(shutil.which("unpaper"))

        self.rotate_pages = QCheckBox("Auto-rotate pages to the correct orientation")
        self.rotate_pages.setChecked(s.value("rotate_pages", True, type=bool))
        form.addRow(self.rotate_pages)

        # Rotation aggressiveness: labelled presets plus a custom confidence value
        self.rotate_level = QComboBox()
        for label, value in ROTATION_LEVELS:
            self.rotate_level.addItem(f"{label} ({value:g})", value)
        self.rotate_level.addItem("Custom…", None)
        self.rotate_threshold = QDoubleSpinBox()
        self.rotate_threshold.setRange(0.1, 100)
        self.rotate_threshold.setDecimals(1)
        self.rotate_threshold.setSingleStep(0.5)
        saved = float(s.value("rotate_threshold", ROTATION_DEFAULT) or ROTATION_DEFAULT)
        self.rotate_threshold.setValue(saved)  # 0 from older versions means "default"
        self._sync_rotate_level()
        self.rotate_level.currentIndexChanged.connect(self._rotate_level_changed)
        self.rotate_threshold.valueChanged.connect(self._sync_rotate_level)
        tip = ("How sure Tesseract must be that a page is sideways or upside down before it "
               "is rotated. Lower values fix more pages but may flip some wrongly.")
        self.rotate_level.setToolTip(tip)
        self.rotate_threshold.setToolTip(tip)
        rot_row = QHBoxLayout()
        rot_row.addWidget(self.rotate_level)
        rot_row.addWidget(QLabel("Custom:"))
        rot_row.addWidget(self.rotate_threshold)
        rot_row.addStretch()
        form.addRow("   Rotation aggressiveness:", rot_row)
        self.rotate_pages.toggled.connect(self.rotate_level.setEnabled)
        self.rotate_pages.toggled.connect(self.rotate_threshold.setEnabled)
        self.rotate_level.setEnabled(self.rotate_pages.isChecked())
        self.rotate_threshold.setEnabled(self.rotate_pages.isChecked())

        self.deskew = QCheckBox("Deskew (straighten crooked scans)")
        self.deskew.setChecked(s.value("deskew", True, type=bool))
        form.addRow(self.deskew)

        self.clean = QCheckBox("Clean pages before OCR (remove scan noise, uses unpaper)")
        self.clean.setChecked(s.value("clean", False, type=bool) and has_unpaper)
        self.clean_final = QCheckBox("Keep the cleaned image in the output (--clean-final)")
        self.clean_final.setChecked(s.value("clean_final", False, type=bool) and has_unpaper)
        if not has_unpaper:
            for cb in (self.clean, self.clean_final):
                cb.setEnabled(False)
                cb.setToolTip("unpaper is not installed")
        form.addRow(self.clean)
        form.addRow(self.clean_final)

        self.remove_background = QCheckBox("Remove background (experimental)")
        self.remove_background.setChecked(s.value("remove_background", False, type=bool))
        form.addRow(self.remove_background)

        self.remove_vectors = QCheckBox("Ignore vector objects when OCRing (helps on drawings/tables)")
        self.remove_vectors.setChecked(s.value("remove_vectors", False, type=bool))
        form.addRow(self.remove_vectors)

        self.oversample = QSpinBox()
        self.oversample.setRange(0, 1200)
        self.oversample.setSingleStep(50)
        self.oversample.setSpecialValueText("Off")
        self.oversample.setSuffix(" DPI")
        self.oversample.setValue(int(s.value("oversample", 0)))
        self.oversample.setToolTip("Upsample low-resolution images to at least this DPI before OCR (e.g. 300).")
        form.addRow("Oversample to:", self.oversample)

        self.image_dpi = QSpinBox()
        self.image_dpi.setRange(0, 2400)
        self.image_dpi.setSpecialValueText("Auto")
        self.image_dpi.setSuffix(" DPI")
        self.image_dpi.setValue(int(s.value("image_dpi", 0)))
        self.image_dpi.setToolTip("DPI to assume for image inputs that don't specify their resolution.")
        form.addRow("Image input DPI:", self.image_dpi)
        return w

    def _build_output_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        s = self.settings

        self.optimize = combo(OPTIMIZE_LEVELS, int(s.value("optimize", 1)))
        form.addRow("Optimization:", self.optimize)

        self.jpeg_quality = QSpinBox()
        self.jpeg_quality.setRange(0, 100)
        self.jpeg_quality.setSpecialValueText("Default")
        self.jpeg_quality.setValue(int(s.value("jpeg_quality", 0)))
        form.addRow("JPEG quality (lossy):", self.jpeg_quality)

        self.png_quality = QSpinBox()
        self.png_quality.setRange(0, 100)
        self.png_quality.setSpecialValueText("Default")
        self.png_quality.setValue(int(s.value("png_quality", 0)))
        form.addRow("PNG quality (lossy):", self.png_quality)

        self.color_strategy = combo(COLOR_STRATEGIES, s.value("color_strategy", None))
        form.addRow("PDF/A color conversion:", self.color_strategy)

        self.fast_web_view = QDoubleSpinBox()
        self.fast_web_view.setRange(0, 100000)
        self.fast_web_view.setSuffix(" MB")
        self.fast_web_view.setSpecialValueText("Default")
        self.fast_web_view.setValue(float(s.value("fast_web_view", 0)))
        self.fast_web_view.setToolTip("Linearize (fast web view) files larger than this size.")
        form.addRow("Fast web view above:", self.fast_web_view)

        self.pdf_renderer = combo(RENDERERS, s.value("pdf_renderer", "auto"))
        form.addRow("Text layer renderer:", self.pdf_renderer)
        return w

    def _build_metadata_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        note = QLabel("Leave blank to keep the document's existing values. Applied to every file.")
        note.setStyleSheet("color: palette(mid);")
        form.addRow(note)
        self.title = QLineEdit()
        self.author = QLineEdit()
        self.subject = QLineEdit()
        self.keywords = QLineEdit()
        form.addRow("Title:", self.title)
        form.addRow("Author:", self.author)
        form.addRow("Subject:", self.subject)
        form.addRow("Keywords:", self.keywords)
        return w

    def _build_advanced_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        s = self.settings

        self.jobs = QSpinBox()
        self.jobs.setRange(0, os.cpu_count() or 64)
        self.jobs.setSpecialValueText(f"Auto ({os.cpu_count()} CPUs)")
        self.jobs.setValue(int(s.value("jobs", 0)))
        form.addRow("Parallel jobs:", self.jobs)

        self.ocr_engine = combo(OCR_ENGINES, s.value("ocr_engine", "auto"))
        form.addRow("OCR engine:", self.ocr_engine)

        self.psm = combo(PSM_MODES, s.value("psm", None))
        form.addRow("Tesseract page segmentation:", self.psm)

        self.oem = combo(OEM_MODES, s.value("oem", None))
        form.addRow("Tesseract engine mode:", self.oem)

        self.tess_timeout = QDoubleSpinBox()
        self.tess_timeout.setRange(0, 3600)
        self.tess_timeout.setSuffix(" s")
        self.tess_timeout.setSpecialValueText("Default (180 s)")
        self.tess_timeout.setValue(float(s.value("tess_timeout", 0)))
        form.addRow("Tesseract timeout per page:", self.tess_timeout)

        self.skip_big = QDoubleSpinBox()
        self.skip_big.setRange(0, 10000)
        self.skip_big.setSuffix(" MPixels")
        self.skip_big.setSpecialValueText("Off")
        self.skip_big.setValue(float(s.value("skip_big", 0)))
        form.addRow("Skip OCR on images larger than:", self.skip_big)

        self.rasterizer = combo(RASTERIZERS, s.value("rasterizer", "auto"))
        form.addRow("Rasterizer:", self.rasterizer)

        self.invalidate_sigs = QCheckBox("Allow invalidating digital signatures")
        self.invalidate_sigs.setChecked(s.value("invalidate_sigs", False, type=bool))
        form.addRow(self.invalidate_sigs)

        self.tagged_ignore = QCheckBox("Process Tagged PDFs anyway (don't stop on them)")
        self.tagged_ignore.setChecked(s.value("tagged_ignore", False, type=bool))
        form.addRow(self.tagged_ignore)

        self.soft_render = QCheckBox("Continue on soft rendering errors")
        self.soft_render.setChecked(s.value("soft_render", False, type=bool))
        form.addRow(self.soft_render)

        self.extra_args = QLineEdit(s.value("extra_args", ""))
        self.extra_args.setPlaceholderText("Any other ocrmypdf arguments, e.g. --unpaper-args \"--layout double\"")
        form.addRow("Extra arguments:", self.extra_args)
        return w

    def _rotate_level_changed(self, index):
        value = self.rotate_level.itemData(index)
        if value is not None:  # a preset; "Custom…" keeps the typed value
            self.rotate_threshold.blockSignals(True)
            self.rotate_threshold.setValue(value)
            self.rotate_threshold.blockSignals(False)
            self.update_preview()

    def _sync_rotate_level(self, *_):
        """Select the preset matching the custom value, or "Custom…"."""
        value = self.rotate_threshold.value()
        index = next((i for i, (_, v) in enumerate(ROTATION_LEVELS) if abs(v - value) < 1e-6),
                     len(ROTATION_LEVELS))
        self.rotate_level.blockSignals(True)
        self.rotate_level.setCurrentIndex(index)
        self.rotate_level.blockSignals(False)

    def populate_languages(self, checked: set[str]):
        self.lang_list.blockSignals(True)
        self.lang_list.clear()
        langs = tesseract_languages() or ["eng"]
        # checked languages first, then alphabetical by display name
        for lang in sorted(langs, key=lambda c: (c not in checked, lang_label(c).lower())):
            item = QListWidgetItem(lang_label(lang))
            item.setData(Qt.UserRole, lang)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if lang in checked else Qt.Unchecked)
            self.lang_list.addItem(item)
        self.lang_list.blockSignals(False)

    def download_languages(self):
        target = user_tessdata_dir()
        installed = set(tesseract_languages())
        dlg = LanguageDownloadDialog(self, installed, target)
        dlg.exec()
        if dlg.downloaded:
            self.populate_languages(set(self.selected_languages()) | set(dlg.downloaded))
            self.update_preview()

    # ----- helpers
    def _connect_preview(self, widget):
        for child in widget.findChildren(QWidget):
            if isinstance(child, QCheckBox):
                child.toggled.connect(self.update_preview)
            elif isinstance(child, QComboBox):
                child.currentIndexChanged.connect(self.update_preview)
            elif isinstance(child, (QSpinBox, QDoubleSpinBox)):
                child.valueChanged.connect(self.update_preview)
            elif isinstance(child, QLineEdit) and child is not getattr(self, "cmd_preview", None):
                child.textChanged.connect(self.update_preview)
        self.lang_list.itemChanged.connect(self.update_preview)

    def _pick_out_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Choose output folder", self.out_dir.text())
        if d:
            self.out_dir.setText(d)

    def remove_selected(self):
        for item in self.file_list.selectedItems():
            self.main.files.remove(item.data(Qt.UserRole))
        self.main.refresh_files()

    def set_files(self, files: list[Path]):
        self.file_list.clear()
        for f in files:
            item = QListWidgetItem(f"{f.name}    —    {f.parent}")
            item.setData(Qt.UserRole, f)
            item.setToolTip(str(f))
            self.file_list.addItem(item)
        self.start_btn.setEnabled(bool(files))
        self.update_preview()

    def selected_languages(self) -> list[str]:
        return [
            self.lang_list.item(i).data(Qt.UserRole)
            for i in range(self.lang_list.count())
            if self.lang_list.item(i).checkState() == Qt.Checked
        ]

    def build_args(self) -> list[str]:
        """Translate the widgets into ocrmypdf command-line arguments."""
        a: list[str] = []
        langs = self.selected_languages()
        if langs:
            a += ["-l", "+".join(langs)]
        mode = self.mode.currentData()
        if mode != "default":
            a += ["--mode", mode]
        a += ["--output-type", self.output_type.currentData()]
        if self.pages.text().strip():
            a += ["--pages", self.pages.text().strip()]
        if self.rotate_pages.isChecked():
            a.append("--rotate-pages")
            a += ["--rotate-pages-threshold", f"{self.rotate_threshold.value():g}"]
        if self.deskew.isChecked():
            a.append("--deskew")
        if self.clean.isChecked():
            a.append("--clean")
        if self.clean_final.isChecked():
            a.append("--clean-final")
        if self.remove_background.isChecked():
            a.append("--remove-background")
        if self.remove_vectors.isChecked():
            a.append("--remove-vectors")
        if self.oversample.value():
            a += ["--oversample", str(self.oversample.value())]
        if self.image_dpi.value():
            a += ["--image-dpi", str(self.image_dpi.value())]
        a += ["--optimize", str(self.optimize.currentData())]
        if self.jpeg_quality.value():
            a += ["--jpeg-quality", str(self.jpeg_quality.value())]
        if self.png_quality.value():
            a += ["--png-quality", str(self.png_quality.value())]
        if self.color_strategy.currentData():
            a += ["--color-conversion-strategy", self.color_strategy.currentData()]
        if self.fast_web_view.value():
            a += ["--fast-web-view", f"{self.fast_web_view.value():g}"]
        if self.pdf_renderer.currentData() != "auto":
            a += ["--pdf-renderer", self.pdf_renderer.currentData()]
        for flag, edit in (("--title", self.title), ("--author", self.author),
                           ("--subject", self.subject), ("--keywords", self.keywords)):
            if edit.text().strip():
                a += [flag, edit.text().strip()]
        if self.jobs.value():
            a += ["--jobs", str(self.jobs.value())]
        if self.ocr_engine.currentData() != "auto":
            a += ["--ocr-engine", self.ocr_engine.currentData()]
        if self.psm.currentData() is not None:
            a += ["--tesseract-pagesegmode", str(self.psm.currentData())]
        if self.oem.currentData() is not None:
            a += ["--tesseract-oem", str(self.oem.currentData())]
        if self.tess_timeout.value():
            a += ["--tesseract-timeout", f"{self.tess_timeout.value():g}"]
        if self.skip_big.value():
            a += ["--skip-big", f"{self.skip_big.value():g}"]
        if self.rasterizer.currentData() != "auto":
            a += ["--rasterizer", self.rasterizer.currentData()]
        if self.invalidate_sigs.isChecked():
            a.append("--invalidate-digital-signatures")
        if self.tagged_ignore.isChecked():
            a += ["--tagged-pdf-mode", "ignore"]
        if self.soft_render.isChecked():
            a.append("--continue-on-soft-render-error")
        extra = self.extra_args.text().strip()
        if extra:
            try:
                a += shlex.split(extra)
            except ValueError:
                pass  # unbalanced quotes - validated on start
        return a

    def output_path_for(self, src: Path) -> Path:
        out_dir = src.parent if self.out_same.isChecked() else Path(self.out_dir.text().strip() or src.parent)
        return out_dir / f"{src.stem}{self.suffix.text()}.pdf"

    def update_preview(self, *_):
        if not hasattr(self, "cmd_preview"):
            return
        args = ["ocrmypdf", *self.build_args()]
        if self.main.files:
            src = self.main.files[0]
            if self.sidecar.isChecked():
                args += ["--sidecar", str(self.output_path_for(src).with_suffix(".txt"))]
            args += [str(src), str(self.output_path_for(src))]
        else:
            args += ["input.pdf", "output.pdf"]
        self.cmd_preview.setText(shlex.join(args))
        self.cmd_preview.setCursorPosition(0)

    def validate(self) -> str | None:
        if not self.main.files:
            return "Add at least one file."
        if not self.selected_languages() and self.ocr_engine.currentData() != "none":
            return "Select at least one OCR language."
        if not self.out_same.isChecked():
            d = self.out_dir.text().strip()
            if not d:
                return "Choose an output folder."
            if not Path(d).is_dir():
                return f"Output folder does not exist:\n{d}"
        try:
            shlex.split(self.extra_args.text())
        except ValueError as e:
            return f"Extra arguments could not be parsed: {e}"
        for f in self.main.files:
            if self.output_path_for(f).resolve() == f.resolve():
                return (f"Output would overwrite the input file:\n{f}\n\n"
                        "Set a file name suffix or choose a different output folder.")
        return None

    def save_settings(self):
        s = self.settings
        s.setValue("languages", "+".join(self.selected_languages()))
        s.setValue("mode", self.mode.currentData())
        s.setValue("output_type", self.output_type.currentData())
        s.setValue("out_same", self.out_same.isChecked())
        s.setValue("out_dir", self.out_dir.text())
        s.setValue("suffix", self.suffix.text())
        s.setValue("overwrite", self.overwrite.isChecked())
        s.setValue("sidecar", self.sidecar.isChecked())
        s.setValue("pages", self.pages.text())
        s.setValue("rotate_pages", self.rotate_pages.isChecked())
        s.setValue("rotate_threshold", self.rotate_threshold.value())
        s.setValue("deskew", self.deskew.isChecked())
        s.setValue("clean", self.clean.isChecked())
        s.setValue("clean_final", self.clean_final.isChecked())
        s.setValue("remove_background", self.remove_background.isChecked())
        s.setValue("remove_vectors", self.remove_vectors.isChecked())
        s.setValue("oversample", self.oversample.value())
        s.setValue("image_dpi", self.image_dpi.value())
        s.setValue("optimize", self.optimize.currentData())
        s.setValue("jpeg_quality", self.jpeg_quality.value())
        s.setValue("png_quality", self.png_quality.value())
        s.setValue("color_strategy", self.color_strategy.currentData())
        s.setValue("fast_web_view", self.fast_web_view.value())
        s.setValue("pdf_renderer", self.pdf_renderer.currentData())
        s.setValue("jobs", self.jobs.value())
        s.setValue("ocr_engine", self.ocr_engine.currentData())
        s.setValue("psm", self.psm.currentData())
        s.setValue("oem", self.oem.currentData())
        s.setValue("tess_timeout", self.tess_timeout.value())
        s.setValue("skip_big", self.skip_big.value())
        s.setValue("rasterizer", self.rasterizer.currentData())
        s.setValue("invalidate_sigs", self.invalidate_sigs.isChecked())
        s.setValue("tagged_ignore", self.tagged_ignore.isChecked())
        s.setValue("soft_render", self.soft_render.isChecked())
        s.setValue("extra_args", self.extra_args.text())

    def reset_defaults(self):
        if QMessageBox.question(self, APP_NAME, "Reset all options to their defaults?") != QMessageBox.Yes:
            return
        self.settings.clear()
        self.main.rebuild_options_page()


# ----------------------------------------------------------------- progress page


@dataclass
class Job:
    src: Path
    dst: Path
    pages: int
    sidecar: Path | None = None
    status: str = "pending"  # pending | running | done | failed | skipped
    message: str = ""
    progress: float = 0.0  # 0..1 within this file
    started: float = 0.0
    log: list[str] = field(default_factory=list)


class ProgressPage(QWidget):
    def __init__(self, main: "MainWindow"):
        super().__init__()
        self.main = main
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)

        self.title = QLabel("Processing…")
        self.title.setStyleSheet("font-size: 20px; font-weight: 700;")
        lay.addWidget(self.title)

        self.percent = QLabel("0%")
        self.percent.setAlignment(Qt.AlignCenter)
        self.percent.setStyleSheet("font-size: 44px; font-weight: 700;")
        lay.addWidget(self.percent)

        self.overall = QProgressBar()
        self.overall.setRange(0, 1000)
        self.overall.setTextVisible(False)
        self.overall.setMinimumHeight(22)
        lay.addWidget(self.overall)

        times = QHBoxLayout()
        self.elapsed_lbl = QLabel("Elapsed: 00:00")
        self.eta_lbl = QLabel("ETA: calculating…")
        self.eta_lbl.setStyleSheet("font-weight: 600;")
        times.addWidget(self.elapsed_lbl)
        times.addStretch()
        times.addWidget(self.eta_lbl)
        lay.addLayout(times)
        lay.addSpacing(10)

        self.file_lbl = QLabel("")
        self.file_lbl.setStyleSheet("font-weight: 600;")
        self.stage_lbl = QLabel("")
        # White text on a dark backing so the page count is readable on any theme
        self.stage_lbl.setStyleSheet(
            "color: white; background: rgba(0, 0, 0, 150); border-radius: 4px; padding: 2px 8px;"
        )
        self.stage_lbl.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        self.file_bar = QProgressBar()
        self.file_bar.setRange(0, 1000)
        self.file_bar.setFormat("")
        lay.addWidget(self.file_lbl)
        lay.addWidget(self.stage_lbl)
        lay.addWidget(self.file_bar)

        self.job_list = QListWidget()
        self.job_list.setMaximumHeight(140)
        lay.addWidget(self.job_list)

        self.log_toggle = QPushButton("Show log ▸")
        self.log_toggle.setFlat(True)
        self.log_toggle.setStyleSheet("text-align: left;")
        self.log_toggle.clicked.connect(self.toggle_log)
        lay.addWidget(self.log_toggle)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("monospace", 9))
        self.log.setMaximumBlockCount(5000)
        self.log.hide()
        lay.addWidget(self.log, 1)
        lay.addStretch()

        btns = QHBoxLayout()
        self.open_btn = QPushButton("Open output folder")
        self.open_btn.clicked.connect(main.open_output_folder)
        self.new_btn = QPushButton("Process more files")
        self.new_btn.clicked.connect(main.go_home)
        self.back_btn = QPushButton("Back to options")
        self.back_btn.clicked.connect(main.go_options)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(main.cancel)
        btns.addWidget(self.open_btn)
        btns.addWidget(self.back_btn)
        btns.addWidget(self.new_btn)
        btns.addStretch()
        btns.addWidget(self.cancel_btn)
        lay.addLayout(btns)
        self.set_running(True)

    def toggle_log(self):
        vis = not self.log.isVisible()
        self.log.setVisible(vis)
        self.log_toggle.setText("Hide log ▾" if vis else "Show log ▸")

    def set_running(self, running: bool):
        self.cancel_btn.setVisible(running)
        self.open_btn.setVisible(not running)
        self.new_btn.setVisible(not running)
        self.back_btn.setVisible(not running)

    def set_jobs(self, jobs: list[Job]):
        self.file_bar.show()
        self.job_list.clear()
        for job in jobs:
            self.job_list.addItem(QListWidgetItem(""))
        self.refresh_jobs(jobs)
        self.log.clear()

    def refresh_jobs(self, jobs: list[Job]):
        icons = {"pending": "⏳", "running": "▶", "done": "✅", "failed": "❌", "skipped": "⏭", "cancelled": "⏹"}
        for i, job in enumerate(jobs):
            text = f"{icons.get(job.status, '')}  {job.src.name}"
            if job.status == "running":
                text += f"  ({job.progress * 100:.0f}%)"
            if job.message:
                text += f"  —  {job.message}"
            self.job_list.item(i).setText(text)


# ------------------------------------------------------------------ main window


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(820, 720)
        self.setAcceptDrops(True)
        self.settings = QSettings("ocrmypdf-gui", "ocrmypdf-gui")
        self.files: list[Path] = []

        self.jobs: list[Job] = []
        self.current: int = -1
        self.proc: QProcess | None = None
        self.cancelled = False
        self.run_started = 0.0
        self.total_weight = 1
        self.stage = ""
        self.stdout_buf = b""
        self.eta_smooth: float | None = None

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.select_page = SelectPage(self)
        self.options_page = OptionsPage(self)
        self.progress_page = ProgressPage(self)
        self.stack.addWidget(self.select_page)
        self.stack.addWidget(self.options_page)
        self.stack.addWidget(self.progress_page)

        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.update_overall)

    # ---- file handling
    def is_running(self) -> bool:
        return self.proc is not None

    def add_files(self, paths):
        added = 0
        for p in paths:
            p = Path(p)
            candidates = (
                sorted(x for x in p.rglob("*") if x.suffix.lower() in ACCEPTED_EXTS)
                if p.is_dir() else [p]
            )
            for c in candidates:
                if c.is_file() and c.suffix.lower() in ACCEPTED_EXTS and c not in self.files:
                    self.files.append(c.resolve())
                    added += 1
        if added:
            self.refresh_files()
            self.stack.setCurrentWidget(self.options_page)
        elif paths:
            QMessageBox.warning(self, APP_NAME, "No supported files found (PDF or image files).")

    def refresh_files(self):
        self.options_page.set_files(self.files)
        if not self.files:
            self.stack.setCurrentWidget(self.select_page)

    def clear_files(self):
        self.files.clear()
        self.refresh_files()

    def browse_files(self):
        exts = " ".join(f"*{e}" for e in sorted(ACCEPTED_EXTS))
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select files to OCR", self.settings.value("last_dir", str(Path.home())),
            f"PDF and images ({exts} {exts.upper()});;All files (*)",
        )
        if files:
            self.settings.setValue("last_dir", str(Path(files[0]).parent))
            self.add_files(files)

    def rebuild_options_page(self):
        idx = self.stack.indexOf(self.options_page)
        self.stack.removeWidget(self.options_page)
        self.options_page.deleteLater()
        self.options_page = OptionsPage(self)
        self.stack.insertWidget(idx, self.options_page)
        self.options_page.set_files(self.files)
        self.stack.setCurrentWidget(self.options_page)

    # ---- drag & drop (anywhere in the window)
    def dragEnterEvent(self, event):
        if not self.is_running() and event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.select_page.zone.set_hover(True)

    def dragLeaveEvent(self, event):
        self.select_page.zone.set_hover(False)

    def dropEvent(self, event):
        self.select_page.zone.set_hover(False)
        if self.is_running():
            return
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        self.add_files(paths)

    # ---- navigation
    def go_home(self):
        self.files.clear()
        self.refresh_files()
        self.stack.setCurrentWidget(self.select_page)

    def go_options(self):
        self.refresh_files()
        self.stack.setCurrentWidget(self.options_page)

    def open_output_folder(self):
        folder = None
        for job in self.jobs:
            if job.status == "done":
                folder = job.dst.parent
                break
        if folder is None and self.jobs:
            folder = self.jobs[0].dst.parent
        if folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # ---- running
    def start(self):
        opts = self.options_page
        err = opts.validate()
        if err:
            QMessageBox.warning(self, APP_NAME, err)
            return
        opts.save_settings()

        self.jobs = []
        existing = []
        for f in self.files:
            dst = opts.output_path_for(f)
            job = Job(src=f, dst=dst, pages=page_count(f))
            if opts.sidecar.isChecked():
                job.sidecar = dst.with_suffix(".txt")
            if dst.exists() and not opts.overwrite.isChecked():
                existing.append(dst)
            self.jobs.append(job)
        if existing:
            names = "\n".join(str(p) for p in existing[:10])
            more = f"\n…and {len(existing) - 10} more" if len(existing) > 10 else ""
            ans = QMessageBox.question(
                self, APP_NAME,
                f"These output files already exist:\n{names}{more}\n\nOverwrite them?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            )
            if ans == QMessageBox.Cancel:
                return
            if ans == QMessageBox.No:
                for job in self.jobs:
                    if job.dst in existing:
                        job.status, job.message = "skipped", "output exists"

        self.base_args = opts.build_args()
        self.total_weight = sum(j.pages for j in self.jobs if j.status != "skipped") or 1
        self.cancelled = False
        self.current = -1
        self.eta_smooth = None
        self.run_started = time.monotonic()

        pp = self.progress_page
        pp.set_jobs(self.jobs)
        pp.title.setText(f"Processing {len(self.jobs)} file{'s' if len(self.jobs) != 1 else ''}…")
        pp.set_running(True)
        pp.eta_lbl.setText("ETA: calculating…")
        self.stack.setCurrentWidget(pp)
        self.timer.start()
        self.next_job()

    def next_job(self):
        self.current += 1
        while self.current < len(self.jobs) and self.jobs[self.current].status == "skipped":
            self.current += 1
        if self.current >= len(self.jobs) or self.cancelled:
            self.finish()
            return
        job = self.jobs[self.current]
        job.status = "running"
        job.started = time.monotonic()
        job.progress = 0.0
        self.stage = "Starting"
        self.stdout_buf = b""

        args = ["-m", "ocrmypdf", "--plugin", str(PLUGIN_PATH), *self.base_args]
        if job.sidecar:
            args += ["--sidecar", str(job.sidecar)]
        args += [str(job.src), str(job.dst)]

        pp = self.progress_page
        pp.file_lbl.setText(f"File {self.current + 1} of {len(self.jobs)}: {job.src.name}")
        pp.stage_lbl.setText("Starting…")
        pp.stage_lbl.show()
        pp.file_bar.setValue(0)
        pp.log.appendPlainText(f"\n$ ocrmypdf {shlex.join(args[4:])}")
        pp.refresh_jobs(self.jobs)

        self.proc = QProcess(self)
        env = self.proc.processEnvironment()
        if env.isEmpty():
            from PySide6.QtCore import QProcessEnvironment
            env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("PYTHONIOENCODING", "utf-8")
        self.proc.setProcessEnvironment(env)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self.on_output)
        self.proc.finished.connect(self.on_finished)
        self.proc.errorOccurred.connect(self.on_error)
        self.proc.start(sys.executable, args)

    def on_output(self):
        if not self.proc:
            return
        self.stdout_buf += bytes(self.proc.readAllStandardOutput())
        *lines, self.stdout_buf = self.stdout_buf.split(b"\n")
        for raw in lines:
            line = raw.decode("utf-8", "replace").rstrip("\r")
            if line.startswith(MARKER):
                try:
                    self.on_progress_event(json.loads(line[len(MARKER):]))
                except json.JSONDecodeError:
                    pass
            elif line.strip():
                self.progress_page.log.appendPlainText(line)
                self.jobs[self.current].log.append(line)

    def on_progress_event(self, ev: dict):
        job = self.jobs[self.current]
        desc = ev.get("desc") or ""
        lo, hi = STAGE_BANDS.get(desc, (None, None))
        total = ev.get("total") or 0
        if ev.get("event") == "start":
            self.stage = desc
            frac = 0.0
        elif ev.get("event") == "update":
            frac = min(1.0, (ev.get("current") or 0) / total) if total else 0.0
        else:  # end
            frac = 1.0
        if lo is not None:
            job.progress = max(job.progress, (lo + frac * (hi - lo)) / 100)
        unit = ev.get("unit") or ("page" if desc in ("OCR", "Scanning contents", "PDF/A conversion") else "")
        detail = desc
        if ev.get("event") == "update" and total:
            cur = ev.get("current") or 0
            detail += f" — {cur:g}/{total:g} {unit}s" if unit and unit != "%" else f" — {cur / total * 100:.0f}%"
        self.progress_page.stage_lbl.setText(detail)
        self.update_overall()

    def overall_fraction(self) -> float:
        done = 0.0
        for job in self.jobs:
            if job.status in ("done", "failed"):
                done += job.pages
            elif job.status == "running":
                done += job.pages * job.progress
        return min(1.0, done / self.total_weight)

    def update_overall(self):
        if not self.jobs:
            return
        pp = self.progress_page
        frac = self.overall_fraction()
        elapsed = time.monotonic() - self.run_started
        pp.overall.setValue(int(frac * 1000))
        pp.percent.setText(f"{frac * 100:.0f}%")
        pp.elapsed_lbl.setText(f"Elapsed: {human_time(elapsed)}")
        if 0 <= self.current < len(self.jobs):
            job = self.jobs[self.current]
            pp.file_bar.setValue(int(job.progress * 1000))
            pp.refresh_jobs(self.jobs)
        if self.is_running():
            # ETA from overall throughput, smoothed so it doesn't jump around.
            if frac > 0.02 and elapsed > 2:
                raw = elapsed * (1 - frac) / frac
                self.eta_smooth = raw if self.eta_smooth is None else 0.7 * self.eta_smooth + 0.3 * raw
                # count down between progress updates
                pp.eta_lbl.setText(f"ETA: {human_time(self.eta_smooth)} remaining")
            else:
                pp.eta_lbl.setText("ETA: calculating…")
        self.setWindowTitle(f"{frac * 100:.0f}% — {APP_NAME}" if self.is_running() else APP_NAME)

    def on_error(self, err):
        if err == QProcess.FailedToStart and self.proc:
            self.progress_page.log.appendPlainText(f"Failed to start: {self.proc.errorString()}")
            self.on_finished(-1, QProcess.CrashExit)

    def on_finished(self, code, status):
        if self.proc is None:
            return
        self.on_output()
        if self.stdout_buf:
            self.progress_page.log.appendPlainText(self.stdout_buf.decode("utf-8", "replace"))
        self.proc.deleteLater()
        self.proc = None
        job = self.jobs[self.current]
        elapsed = human_time(time.monotonic() - job.started)
        if self.cancelled:
            job.status, job.message = "cancelled", "cancelled"
            job.progress = 0
        elif code == 0 and status == QProcess.NormalExit:
            job.status, job.message, job.progress = "done", f"done in {elapsed}", 1.0
        elif code == 10:
            job.status, job.message, job.progress = "done", "done (PDF/A conversion failed, saved as regular PDF)", 1.0
        else:
            job.status = "failed"
            job.message = EXIT_CODE_MESSAGES.get(code, f"failed (exit code {code})")
            if any("No space left on device" in line for line in job.log[-50:]):
                import tempfile
                job.message = (
                    "Ran out of disk space for temporary files in "
                    f"{tempfile.gettempdir()} - free up space, or OCR fewer pages at a time"
                )
            job.progress = 1.0  # counts as finished for overall progress
            if not self.progress_page.log.isVisible():
                self.progress_page.toggle_log()
        self.progress_page.refresh_jobs(self.jobs)
        self.update_overall()
        self.next_job()

    def cancel(self):
        if not self.proc:
            return
        if QMessageBox.question(self, APP_NAME, "Cancel processing?") != QMessageBox.Yes:
            return
        self.cancelled = True
        for job in self.jobs:
            if job.status == "pending":
                job.status = "cancelled"
        proc = self.proc
        proc.terminate()
        QTimer.singleShot(4000, lambda: proc.kill() if self.proc is proc else None)
        self.progress_page.stage_lbl.setText("Cancelling…")

    def finish(self):
        self.timer.stop()
        self.update_overall()
        pp = self.progress_page
        pp.set_running(False)
        total = human_time(time.monotonic() - self.run_started)
        ok = sum(j.status == "done" for j in self.jobs)
        failed = sum(j.status == "failed" for j in self.jobs)
        if self.cancelled:
            pp.title.setText("Cancelled")
        elif failed:
            pp.title.setText(f"Finished with errors: {ok} succeeded, {failed} failed")
        else:
            pp.title.setText(f"Done! {ok} file{'s' if ok != 1 else ''} processed")
            pp.percent.setText("100%")
            pp.overall.setValue(1000)
        pp.eta_lbl.setText(f"Total time: {total}")
        pp.stage_lbl.hide()
        pp.file_lbl.setText("")
        if not self.cancelled:
            pp.file_bar.hide()
        pp.refresh_jobs(self.jobs)
        self.setWindowTitle(APP_NAME)
        QApplication.alert(self)

    def closeEvent(self, event):
        if self.proc:
            if QMessageBox.question(self, APP_NAME, "OCR is still running. Stop it and quit?") != QMessageBox.Yes:
                event.ignore()
                return
            self.cancelled = True
            self.proc.kill()
            self.proc.waitForFinished(3000)
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName(os.environ.get("FLATPAK_ID", "ocrmypdf-gui"))
    setup_tessdata()
    app.setWindowIcon(app_icon())
    win = MainWindow()
    win.show()
    if len(sys.argv) > 1:
        win.add_files(sys.argv[1:])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
