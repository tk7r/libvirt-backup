#!/usr/bin/env python3
"""build.py - local build: quality checks, then the .deb.

py_compile always; ruff format-check and lint when ruff is present.
The .deb itself is built by build-package.sh (lintian when present)
and lands next to the repository checkout.

Requires: dpkg-dev, debhelper, fakeroot; optionally ruff.
Run from anywhere.
"""

import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent


def quality():
    """Byte-compile the tool; run ruff format-check and lint when present.

    Raises:
        subprocess.CalledProcessError: on a compile or lint failure
    """
    subprocess.run(
        [sys.executable, "-m", "py_compile", "libvirt-backup.py"],
        check=True,
        cwd=REPO,
    )
    if shutil.which("ruff"):
        for mode in ("format --check", "check"):
            subprocess.run(
                ["ruff", *mode.split(), "libvirt-backup.py"],
                check=True,
                cwd=REPO,
            )
    else:
        print("WARNING: ruff not installed - lint skipped", file=sys.stderr)


def main():
    """Run the quality checks and the .deb build.

    Returns:
        int: process exit code
    """
    try:
        quality()
        subprocess.run(["./build-package.sh"], check=True, cwd=REPO)
    except subprocess.CalledProcessError as exc:
        cmd = " ".join(exc.cmd)
        print(
            f"error: command failed (exit {exc.returncode}): {cmd}",
            file=sys.stderr,
        )
        return exc.returncode or 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
