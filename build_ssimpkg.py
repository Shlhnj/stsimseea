"""
Bundle src/ into a SyncroSim package file: dist/stsimseea-<x-y-z>.ssimpkg

Same layout as ApexRMS's SyncroSim 3 packages (e.g. stsim-4-5-4.ssimpkg):
a zip with the package files at the root plus a .metadata file.

    python build_ssimpkg.py

On Windows you can also let SyncroSim build it (copy src to a folder named
stsimseea first):
    "C:\\Program Files\\SyncroSim Studio\\SyncroSim.PackageManager.exe" --create=stsimseea
"""
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
DIST = os.path.join(HERE, "dist")
SKIP_DIRS = {"__pycache__", ".pytest_cache"}
SKIP_EXT = {".pyc"}


def main():
    xml = open(os.path.join(SRC, "package.xml"), encoding="utf-8-sig").read()
    pkg = re.search(r'<package\s[^>]*name="([^"]+)"', xml).group(1)
    ver = re.search(r'<package\s[^>]*version="([^"]+)"', xml).group(1)
    ssim = re.search(r'minSyncroSimVersion="([^"]+)"', xml).group(1)
    metadata = ('\ufeff<?xml version="1.0" encoding="utf-8"?>\r\n'
                f'<package builtForSyncroSimVersion="{ssim}" />').encode("utf-8")

    os.makedirs(DIST, exist_ok=True)
    out = os.path.join(DIST, f"{pkg}-{ver.replace('.', '-')}.ssimpkg")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(".metadata", metadata)
        for root, dirs, files in os.walk(SRC):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for f in sorted(files):
                if os.path.splitext(f)[1] in SKIP_EXT:
                    continue
                path = os.path.join(root, f)
                z.write(path, os.path.relpath(path, SRC).replace(os.sep, "/"))
    with zipfile.ZipFile(out) as z:
        print(out)
        for n in z.namelist():
            print("  ", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
