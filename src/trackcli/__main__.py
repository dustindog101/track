"""``python -m trackcli`` -- mirrors the installed `track` entry point."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
