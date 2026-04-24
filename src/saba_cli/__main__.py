"""Enables `python -m saba_cli ...` invocation."""

from saba_cli.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
