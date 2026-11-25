#!/usr/bin/env python3
"""Convenience entry point: ``python console.py ...``."""

from release_console import ReleaseConsole, main

__all__ = ["ReleaseConsole", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
