# nuitka-project: --assume-yes-for-downloads
# nuitka-project: --disable-plugin=tk-inter
# nuitka-project: --enable-plugins=no-qt
# nuitka-project: --nofollow-import-to="tkinter"
# nuitka-project: --nofollow-import-to=PIL.AvifImagePlugin
# nuitka-project: --nofollow-import-to=aiohttp
# nuitka-project: --nofollow-import-to=markdown_it
# nuitka-project: --nofollow-import-to=pydoc
# nuitka-project: --nofollow-import-to=pygments
# nuitka-project: --nofollow-import-to=tarfile
# nuitka-project: --nofollow-import-to=zipfile
# nuitka-project: --onefile-cache-mode=cached
# nuitka-project: --onefile-child-grace-time=1
# nuitka-project: --python-flag=no_asserts
# nuitka-project: --python-flag=no_docstrings
# nuitka-project: --python-flag=no_site
# nuitka-project: --python-flag=safe_path
# nuitka-project: --python-flag=static_hashes
# nuitka-project: --warn-unusual-code

# nuitka-project-if: {OS} in ("MACOS"):
#    nuitka-project: --macos-app-console-mode=force
#    nuitka-project: --macos-signed-app-name=com.NSPC911.tuirim

# nuitka-project-if: {OS} in ("Windows"):
#    nuitka-project: --windows-console-mode=force
#    nuitka-project: --output-filename=tuirim.exe
# nuitka-project-else:
#    nuitka-project: --output-filename=tuirim


import argparse
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("-o", "--output", type=Path, help="default: VIDEO.trimmed.mp4")
    args = parser.parse_args()
    try:
        from tuirim.main_app import App

        App(args.video, args.output or args.video.with_suffix(".trimmed.mp4")).run()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
