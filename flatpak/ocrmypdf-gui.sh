#!/bin/sh
# Flatpak launcher: tells the app where the bundled Tesseract languages live.
export OCRMYPDF_GUI_BUNDLED_TESSDATA=/app/share/tessdata
exec python3 /app/share/ocrmypdf-gui/ocrmypdf_gui.py "$@"
