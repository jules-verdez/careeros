# Preparing a release for private review

The owner reviews the exact source, history, package contents, privacy findings and checksums
and explicitly approves each release before publication.

## Source export

```bash
uv run python scripts/public_release.py --check
uv run python scripts/public_release.py --output dist/careeros-source.tar.gz
```

An optional `--private-values /private/path/identifiers.json` argument accepts a private JSON
list of identifiers. Keep that comparison file outside every public manifest. Its contents
are never copied into the export. Findings describe locations and categories without echoing
matched values. This check is supplemental: it does not replace a complete private-data audit.

The manifest in `pyproject.toml` includes only product source, migrations, generic prompts,
documentation, fictional examples, tests, and build configuration. The exporter freezes source
bytes and writes that exact snapshot with neutral archive ownership and timestamps. Missing
manifest entries, unexpected repository files, source symlinks, databases, contact exports,
credential containers, and selected credential patterns fail the check.

Email examples require reviewed synthetic mailbox names and domains. Changing a domain to
`.test`, or using a GitHub noreply address, does not make a real username anonymous. Common
URL and HTML encodings are checked too. Names, account handles, message excerpts, CV facts,
personal rules, dated incidents, screenshots, and employer histories still need manual review.

## History and packages

Create a separate repository from the reviewed source when operational history contains
private data. Do not copy its `.git` folder, merge its branches, or reuse old release bundles.
Review every branch, tag, commit identity, file version, and available unreferenced object.
Choose a neutral author identity explicitly. A deletion commit does not remove older copies.

Build the wheel and source distribution from the same reviewed snapshot. Enumerate every
archive member, inspect ownership and package metadata, compare packaged source bytes with
the reviewed source, and test installation outside the operational workspace. Keep an exact
SHA-256 manifest for the review. The export tool itself never uploads or publishes anything.

## Private operator state

Keep databases, credentials, CVs, correspondence, contact lists, live target lists, generated
reports, backups, and personal `AGENTS.md` rules outside release inputs. Preserve those records
privately; removing them from a release does not require destroying the operator's working data.
Use only fictional fixtures in tests. The CLI loads `.env` only from the current directory;
connector imports do not search parent directories for credentials.

## License

The source and both distributions include the CareerOS Single-Seat Commercial License.
The package declares the custom identifier `LicenseRef-CareerOS-Single-Seat-Commercial`.
Verify that every distributed copy contains the same full license text.
