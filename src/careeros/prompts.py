"""Versioned prompt loader."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from careeros.services.normalization import sha256_text


@dataclass(frozen=True)
class VersionedPrompt:
    name: str
    content: str
    sha256: str


class PromptLibrary:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def load(self, name: str) -> VersionedPrompt:
        path = (self._root / name).resolve()
        if path.parent != self._root:
            raise ValueError("prompt name must not escape the prompt directory")
        if not path.is_file():
            raise FileNotFoundError(f"prompt not found: {path}")
        content = path.read_text(encoding="utf-8")
        return VersionedPrompt(name=name, content=content, sha256=sha256_text(content))
