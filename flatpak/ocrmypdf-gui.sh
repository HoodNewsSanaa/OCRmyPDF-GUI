#!/bin/sh
# Flatpak launcher: tells the app where the bundled Tesseract languages live.
export OCRMYPDF_GUI_BUNDLED_TESSDATA=/app/share/tessdata
# OCRmyPDF keeps several full-resolution images per page until a file is done,
# which can be many GB for long documents. /tmp is often a small RAM-backed
# tmpfs (the default on Arch and Fedora), so keep temporary files on disk instead.
export TMPDIR="${XDG_CACHE_HOME:-$HOME/.cache}/tmp"
mkdir -p "$TMPDIR"
exec python3 /app/share/ocrmypdf-gui/ocrmypdf_gui.py "$@"
