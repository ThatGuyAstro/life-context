"""Run the Windows collector as ``python -m services.collector``."""

from .runtime import main


if __name__ == "__main__":
    raise SystemExit(main())
