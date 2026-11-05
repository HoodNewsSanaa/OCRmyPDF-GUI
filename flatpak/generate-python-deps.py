#!/usr/bin/env python3
"""Generate python-deps.json (a flatpak-builder module) with pinned wheels.

Re-run this to update the Python packages, or if the runtime's Python version changes:
    python3 generate-python-deps.py --python 3.13
"""
import argparse, json, subprocess, sys, tempfile
from pathlib import Path

# PySide6 comes from the io.qt.PySide.BaseApp base app, so only ocrmypdf is needed here.
PACKAGES = ["ocrmypdf"]
# glibc in the runtime (freedesktop 25.08) is 2.41, so accept anything up to that.
ARCHES = {
    arch: [f"manylinux_2_{v}_{arch}" for v in range(41, 16, -1)] + [f"manylinux2014_{arch}"]
    for arch in ("x86_64", "aarch64")
}

def resolve(arch, plats, pyver):
    with tempfile.TemporaryDirectory() as d:
        report = Path(d) / "r.json"
        cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--dry-run", "--ignore-installed",
               "--only-binary=:all:", "--python-version", pyver, "--implementation", "cp",
               "--target", d + "/t", "--report", str(report)]
        for p in plats:
            cmd += ["--platform", p]
        subprocess.run(cmd + PACKAGES, check=True)
        out = {}
        for item in json.loads(report.read_text())["install"]:
            info = item["download_info"]
            out[item["metadata"]["name"].lower()] = (info["url"], info["archive_info"]["hashes"]["sha256"])
        return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default="3.13")
    ap.add_argument("-o", "--output", default=str(Path(__file__).with_name("python-deps.json")))
    a = ap.parse_args()
    per_arch = {arch: resolve(arch, plats, a.python) for arch, plats in ARCHES.items()}
    sources, names = [], set()
    seen = {}
    for arch, pkgs in per_arch.items():
        for name, (url, sha) in pkgs.items():
            names.add(name)
            seen.setdefault((url, sha), set()).add(arch)
    for (url, sha), arches in sorted(seen.items()):
        src = {"type": "file", "url": url, "sha256": sha}
        if len(arches) < len(ARCHES):
            src["only-arches"] = sorted(arches)
        sources.append(src)
    module = {
        "name": "python-deps",
        "buildsystem": "simple",
        "build-commands": [
            "pip3 install --verbose --exists-action=i --no-index --find-links=\"file://${PWD}\" "
            "--prefix=${FLATPAK_DEST} --no-build-isolation " + " ".join(PACKAGES)
        ],
        "sources": sources,
    }
    Path(a.output).write_text(json.dumps(module, indent=2) + "\n")
    print(f"wrote {a.output}: {len(names)} packages, {len(sources)} files")

if __name__ == "__main__":
    main()
