"""Check and export the explicitly allowed public source, without Git history."""

from __future__ import annotations

import argparse
import fnmatch
import gzip
import html
import io
import json
import re
import subprocess
import tarfile
import tomllib
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_PARTS = {
    ".git",
    ".venv",
    ".claude",
    ".codex",
    ".agents",
    ".aws",
    ".ssh",
    "_private",
    "artifacts",
    "output",
    "tmp",
    "exports",
    "backups",
    "__pycache__",
}
PRIVATE_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".pdf",
    ".docx",
    ".eml",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".csv",
    ".log",
    ".bak",
    ".backup",
}
DATABASE_FILE = re.compile(r"\.(?:db|sqlite3?)(?:-(?:wal|shm|journal))?$", re.IGNORECASE)
SECRET = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|sk-[A-Za-z0-9_-]{20,}|apify_api_[A-Za-z0-9_-]{10,}"
    r"|AIza[0-9A-Za-z_-]{30,}|AKIA[A-Z0-9]{16}"
    r"|xox[baprs]-[A-Za-z0-9-]{20,}|ya29\.[A-Za-z0-9_-]{20,}"
    r"|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"
)
ASSIGNMENT = re.compile(
    r"(?i)[\"']?(?:api[_-]?key|access[_-]?token|password|client[_-]?secret)[\"']?"
    r"\s*[=:]\s*[\"']([^\"'\n]{8,})[\"']"
)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}")
PERSONAL_PATH = re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+/", re.IGNORECASE)
SYSTEM_EMAILS = {"mailer-daemon@googlemail.com", "mailer-daemon@google.com"}
SYNTHETIC_MAILBOXES = {
    "a",
    "anyone",
    "bad",
    "bot",
    "candidate",
    "cited",
    "desk",
    "dsn-1",
    "ghost",
    "gmail-generated-id",
    "helper",
    "info",
    "logo",
    "mailer-daemon",
    "no-reply",
    "original-1",
    "other",
    "out-1",
    "parent",
    "pass",
    "press",
    "provider-rfc822-message-id",
    "recipient",
    "recipient.other",
    "recipient.two",
    "reply-1",
    "someone",
    "uncited",
    "unverified",
    "x",
    "you",
}
SYNTHETIC_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "example.test",
    "example.invalid",
    "acme.example",
    "acme.test",
    "agency.test",
    "ashbyhq.test",
    "b.test",
    "blocked.test",
    "careeros.local",
    "dead.test",
    "employer.example",
    "eu.blocked.test",
    "gmail.test",
    "gone.test",
    "googlemail.test",
    "navy.test",
    "notblocked.test",
    "2x.test",
}
# This exact invented address exercises the connector's privacy-address exclusion.
SYNTHETIC_PRIVACY_ADDRESS = "0000+example@users.noreply.github.com"


def normalized_text(value: str) -> str:
    """Inspect common encodings as well as literal text; this is not a proof of anonymity."""
    return unicodedata.normalize("NFKC", html.unescape(unquote(unquote(value)))).casefold()


def is_example_email(address: str) -> bool:
    address = address.casefold()
    mailbox, domain = address.rsplit("@", 1)
    return (
        (mailbox in SYNTHETIC_MAILBOXES and domain in SYNTHETIC_DOMAINS)
        or address == SYNTHETIC_PRIVACY_ADDRESS
        or address in SYSTEM_EMAILS
    )


def reject_source_symlinks(root: Path, path: Path) -> None:
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"Symlink in public source: {current.relative_to(root)}")


def source_snapshot(root: Path, files: list[Path]) -> Mapping[Path, bytes]:
    contents = {}
    for path in files:
        reject_source_symlinks(root, path)
        contents[path] = path.read_bytes()
    return MappingProxyType(contents)


def source_files(root: Path) -> list[Path]:
    manifest = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    target = manifest["tool"]["hatch"]["build"]["targets"]["sdist"]
    files: set[Path] = set()
    for entry in target["include"]:
        relative = Path(entry.removeprefix("/"))
        if relative.is_absolute() or ".." in relative.parts or any(c in entry for c in "*?["):
            raise ValueError("Public manifest entries must be literal paths inside the project")
        start = root / relative
        reject_source_symlinks(root, start)
        if not start.exists():
            raise ValueError(f"Missing public manifest path: {relative}")
        paths = [start, *start.rglob("*")] if start.is_dir() and not start.is_symlink() else [start]
        for path in paths:
            name = path.relative_to(root)
            if any(part in PRIVATE_PARTS for part in name.parts):
                if "__pycache__" in name.parts:
                    continue
                raise ValueError(f"Private path in public manifest: {name}")
            if any(
                fnmatch.fnmatch(name.as_posix(), pattern) for pattern in target.get("exclude", [])
            ):
                continue
            if path.is_symlink():
                raise ValueError(f"Symlink in public source: {name}")
            if path.is_file():
                if (
                    name.name in {"AGENTS.md", ".netrc", ".coverage"}
                    or (name.name.casefold().startswith(".env") and name.name != ".env.example")
                    or path.suffix.casefold() in PRIVATE_SUFFIXES
                    or DATABASE_FILE.search(name.name)
                ):
                    raise ValueError(f"Private file in public source: {name}")
                files.add(path)
    return sorted(files)


def inventory_findings(root: Path, files: list[Path]) -> list[str]:
    if not (root / ".git").exists():
        return []  # A source export deliberately has no Git metadata.
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    allowed = {path.relative_to(root).as_posix() for path in files}
    return [
        f"{name}: unexpected public path; review its inclusion or keep it private"
        for name in sorted(set(result.stdout.decode().split("\0")) - allowed - {""})
        if (root / name).exists() or (root / name).is_symlink()
    ]


def scan_files(
    root: Path,
    files: list[Path],
    *,
    snapshot: Mapping[Path, bytes] | None = None,
    private_values: tuple[str, ...] = (),
) -> list[str]:
    findings = []
    private_values = tuple(normalized_text(value) for value in private_values if value.strip())
    for path in files:
        relative = path.relative_to(root)
        if any(value in normalized_text(relative.as_posix()) for value in private_values):
            findings.append("[redacted path]: private reference value in filename")
            location = "[redacted path]"
        else:
            location = relative.as_posix()
        try:
            content = (
                snapshot[path].decode("utf-8")
                if snapshot is not None
                else path.read_text(encoding="utf-8")
            )
        except UnicodeDecodeError:
            findings.append(f"{location}: non-text content needs explicit public review")
            continue
        for number, raw_line in enumerate(content.splitlines(), 1):
            line = normalized_text(raw_line)
            kinds = []
            if SECRET.search(raw_line) or SECRET.search(line):
                kinds.append("credential pattern")
            assignments = [match[1] for match in ASSIGNMENT.finditer(line)]
            if any(
                not value.casefold().startswith(
                    ("test", "dummy", "fake", "example", "placeholder", "$")
                )
                for value in assignments
            ):
                kinds.append("literal secret assignment")
            if PERSONAL_PATH.search(raw_line) or PERSONAL_PATH.search(line):
                kinds.append("personal filesystem path")
            if any(not is_example_email(match[0]) for match in EMAIL.finditer(line)):
                kinds.append("non-example email address")
            if any(value in line for value in private_values):
                kinds.append("private reference value")
            if kinds:
                findings.append(f"{location}:{number}: {', '.join(kinds)}")
    return findings


def export_source(
    root: Path,
    files: list[Path],
    output: Path,
    *,
    snapshot: Mapping[Path, bytes] | None = None,
    private_values: tuple[str, ...] = (),
) -> None:
    output = output.resolve()
    if output.exists():
        raise ValueError("Output already exists; choose a new file to preserve the previous export")
    public_directories = {path.parent for path in files if path.parent != root}
    if output in files or any(output.is_relative_to(path) for path in public_directories):
        raise ValueError("The export must be outside public source directories")
    if snapshot is None:
        snapshot = source_snapshot(root, files)
    findings = scan_files(root, files, snapshot=snapshot, private_values=private_values)
    if findings:
        raise ValueError("Unsafe public source: " + "; ".join(findings))
    output.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with output.open("xb") as handle:
            created = True
            with gzip.GzipFile(fileobj=handle, filename="", mode="wb", mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w") as archive:
                    for path in files:
                        data = snapshot[path]
                        info = tarfile.TarInfo(f"careeros/{path.relative_to(root).as_posix()}")
                        info.size = len(data)
                        info.mode = 0o644
                        info.mtime = 0
                        archive.addfile(info, io.BytesIO(data))
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check without writing an export")
    parser.add_argument("--output", type=Path, help="Write a source archive without Git history")
    parser.add_argument(
        "--private-values",
        type=Path,
        help="Optional private JSON list of identifiers to reject; never included in the export",
    )
    args = parser.parse_args()
    if args.check and args.output:
        parser.error("Choose --check or --output")
    try:
        files = source_files(ROOT)
        snapshot = source_snapshot(ROOT, files)
        private_values: tuple[str, ...] = ()
        if args.private_values:
            if args.private_values.resolve() in {path.resolve() for path in files}:
                raise ValueError("The private comparison file must be outside the public manifest")
            values = json.loads(args.private_values.read_text(encoding="utf-8"))
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise ValueError("Private comparison file must contain a JSON list of strings")
            private_values = tuple(values)
        findings = inventory_findings(ROOT, files) + scan_files(
            ROOT, files, snapshot=snapshot, private_values=private_values
        )
        if findings:
            print("Public release check failed (matched values are redacted):")
            for finding in findings:
                print(f"- {finding}")
            return 1
        if args.output:
            export_source(
                ROOT, files, args.output, snapshot=snapshot, private_values=private_values
            )
        print(
            f"Source pattern check passed: {len(files)} files. This does not establish anonymity; "
            "review names, prose, metadata, history, and built packages separately."
        )
        return 0
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"Public release check failed: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
