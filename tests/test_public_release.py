"""The public export excludes private state and redacts unsafe-source findings."""

from __future__ import annotations

import importlib.util
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "public_release", Path(__file__).resolve().parents[1] / "scripts" / "public_release.py"
)
assert spec is not None and spec.loader is not None
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.hatch.build.targets.sdist]\ninclude = ["/docs"]\n', encoding="utf-8"
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("# Public guide\n", encoding="utf-8")
    return tmp_path


def test_export_preserves_only_allowed_source_and_has_reproducible_metadata(source: Path) -> None:
    (source / "_private").mkdir()
    (source / "_private" / "contacts.json").write_text('{"private": true}')
    (source / ".env").write_text("PRIVATE_VALUE=local-only")
    files = release.source_files(source)
    first, second = source / "first.tar.gz", source / "second.tar.gz"
    release.export_source(source, files, first)
    release.export_source(source, files, second)
    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first) as archive:
        assert archive.getnames() == ["careeros/docs/guide.md"]
        member = archive.getmembers()[0]
        assert (member.uid, member.gid, member.uname, member.gname, member.mtime) == (
            0,
            0,
            "",
            "",
            0,
        )
        document = archive.extractfile(member)
        assert document is not None
        assert document.read() == b"# Public guide\n"


def test_scan_reports_locations_without_disclosing_secret_or_contact_values(source: Path) -> None:
    secret = "gh" + "p_" + "A" * 36
    contact = "actual.person@" + "mail-provider.com"
    (source / "docs" / "guide.md").write_text(f"{secret}\n{contact}\n")
    findings = release.scan_files(source, release.source_files(source))
    assert len(findings) == 2
    assert "docs/guide.md:1: credential pattern" in findings
    assert "docs/guide.md:2: non-example email address" in findings
    assert secret not in "\n".join(findings)
    assert contact not in "\n".join(findings)


def test_symlinks_cannot_pull_private_data_into_a_public_directory(source: Path) -> None:
    private = source / ".env"
    private.write_text("local-only")
    (source / "docs" / "credentials.md").symlink_to(private)
    with pytest.raises(ValueError, match="Symlink"):
        release.source_files(source)


def test_symlinked_parent_directory_is_rejected(source: Path) -> None:
    outside = source / "_private" / "original" / "careeros"
    outside.mkdir(parents=True)
    (outside / "personal.md").write_text("private records")
    (source / "src").symlink_to(outside.parent, target_is_directory=True)
    (source / "pyproject.toml").write_text(
        '[tool.hatch.build.targets.sdist]\ninclude = ["/src/careeros"]\n'
    )
    with pytest.raises(ValueError, match="Symlink"):
        release.source_files(source)


@pytest.mark.parametrize(
    "name",
    [
        "career.db-wal",
        "career.sqlite3-shm",
        "career.sqlite-journal",
        "contacts.csv",
        "runtime.log",
        "credentials.p12",
        "credentials.pfx",
        "backup.bak",
        "LOCAL.DB-WAL",
    ],
)
def test_private_file_types_fail_even_when_their_content_looks_public(
    source: Path, name: str
) -> None:
    (source / "docs" / name).write_text("No recognizable email or credential pattern.")
    with pytest.raises(ValueError, match="Private file"):
        release.source_files(source)


def test_export_uses_exactly_the_snapshot_that_was_scanned(source: Path) -> None:
    files = release.source_files(source)
    snapshot = release.source_snapshot(source, files)
    assert release.scan_files(source, files, snapshot=snapshot) == []
    (source / "docs" / "guide.md").write_text("gh" + "p_" + "A" * 36)
    output = source / "reviewed.tar.gz"
    release.export_source(source, files, output, snapshot=snapshot)
    with tarfile.open(output) as archive:
        guide = archive.extractfile("careeros/docs/guide.md")
        assert guide is not None
        assert guide.read() == b"# Public guide\n"


def test_direct_export_refuses_unsafe_content_without_creating_an_archive(source: Path) -> None:
    files = release.source_files(source)
    (source / "docs" / "guide.md").write_text("gh" + "p_" + "A" * 36)
    output = source / "unsafe.tar.gz"
    with pytest.raises(ValueError, match="Unsafe public source"):
        release.export_source(source, files, output)
    assert not output.exists()


def test_export_failure_removes_its_partial_archive(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("Storage failure")

    monkeypatch.setattr(release.tarfile.TarFile, "addfile", fail)
    output = source / "partial.tar.gz"
    with pytest.raises(OSError, match="Storage failure"):
        release.export_source(source, release.source_files(source), output)
    assert not output.exists()


def test_tracked_or_unignored_files_outside_the_manifest_fail_review(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (source / ".git").mkdir()
    (source / "unreviewed.txt").write_text("private draft")
    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=b"docs/guide.md\0unreviewed.txt\0deleted.txt\0"
        ),
    )
    assert release.inventory_findings(source, release.source_files(source)) == [
        "unreviewed.txt: unexpected public path; review its inclusion or keep it private"
    ]


def test_export_cannot_replace_source_or_a_previous_export(source: Path) -> None:
    files = release.source_files(source)
    existing = source / "previous.tar.gz"
    existing.write_bytes(b"previous export")
    with pytest.raises(ValueError, match="already exists"):
        release.export_source(source, files, existing)
    assert existing.read_bytes() == b"previous export"
    with pytest.raises(ValueError, match="outside public source"):
        release.export_source(source, files, source / "docs" / "release.tar.gz")


@pytest.mark.parametrize("entry", ["/.env", "/_private", "/../outside"])
def test_private_manifest_entries_are_rejected(source: Path, entry: str) -> None:
    (source / "pyproject.toml").write_text(
        f'[tool.hatch.build.targets.sdist]\ninclude = ["{entry}"]\n'
    )
    (source / ".env").write_text("private")
    (source / "_private").mkdir()
    with pytest.raises(ValueError, match=r"Private|inside the project"):
        release.source_files(source)


@pytest.mark.parametrize("domain", ["example.test", "example.com", "users.noreply.github.com"])
def test_fake_domains_and_privacy_addresses_do_not_anonymize_usernames(
    source: Path, domain: str
) -> None:
    address = "identifiable.person" + "@" + domain
    (source / "docs" / "guide.md").write_text(address)
    findings = release.scan_files(source, release.source_files(source))
    assert findings == ["docs/guide.md:1: non-example email address"]
    assert address not in findings[0]


def test_encoded_identifiers_and_filenames_are_checked_without_echoing_values(source: Path) -> None:
    private_name = "private-operator"
    guide = source / "docs" / (private_name + ".md")
    guide.write_text("private%2Doperator")
    findings = release.scan_files(source, [guide], private_values=(private_name,))
    assert "[redacted path]: private reference value in filename" in findings
    assert any("private reference value" in finding for finding in findings)
    # The content finding uses an ordinary source location; a private filename must be redacted.
    assert private_name not in "\n".join(findings)


def test_export_rechecks_private_reference_values(source: Path) -> None:
    (source / "docs" / "guide.md").write_text("operator-identity")
    output = source / "rejected.tar.gz"
    with pytest.raises(ValueError, match="private reference value"):
        release.export_source(
            source, release.source_files(source), output, private_values=("operator-identity",)
        )
    assert not output.exists()


def test_missing_manifest_entry_is_not_silently_omitted(source: Path) -> None:
    (source / "pyproject.toml").write_text(
        '[tool.hatch.build.targets.sdist]\ninclude = ["/missing"]\n'
    )
    with pytest.raises(ValueError, match="Missing public manifest path"):
        release.source_files(source)
