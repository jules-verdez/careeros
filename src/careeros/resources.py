"""Resolve versioned runtime resources in a checkout or an installed wheel."""

from pathlib import Path


def resource_root() -> Path:
    package = Path(__file__).resolve().parent
    bundled = package / "_resources"
    return bundled if bundled.is_dir() else package.parents[1]
