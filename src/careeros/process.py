"""Bounded, non-interactive subprocess execution.

Shared by the readiness probes and by connectors that drive a vendor CLI. Both need to
run one command, capture its output and never block on a prompt, and both need that
behaviour to be replaceable in tests without patching the subprocess module.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(self, args: tuple[str, ...], *, timeout: float = 15.0) -> CommandResult:
        """Run one bounded, non-interactive command."""


class SubprocessCommandRunner:
    def run(self, args: tuple[str, ...], *, timeout: float = 15.0) -> CommandResult:
        try:
            completed = subprocess.run(
                args,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return CommandResult(
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = (
                exc.stdout
                if isinstance(exc.stdout, str)
                else (exc.stdout.decode() if exc.stdout else "")
            )
            stderr = (
                exc.stderr
                if isinstance(exc.stderr, str)
                else (exc.stderr.decode() if exc.stderr else "")
            )
            return CommandResult(
                returncode=124,
                stdout=stdout,
                stderr=stderr or f"Command timed out after {timeout}s",
            )
