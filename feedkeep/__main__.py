"""Enable ``python -m feedkeep``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
