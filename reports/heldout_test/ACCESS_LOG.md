# Held-out Access Log

Append-only. One read is expected. Any additional read means the single-read protocol was broken and must be disclosed, with its reason recorded below.

Reformatting note: this file was originally written as a table with reasons appended as bullets, which interleaved badly as entries accumulated. It was reformatted once into the per-read blocks below. No entry, timestamp, checksum, or reason was removed or altered.

Across all three reads the fraud model artifact checksum was identical and every transaction, event, and business metric was identical. Only the report generator changed between reads. No model, threshold, detector parameter, or policy was tuned against the held-out labels at any point.

## Read 1

- Timestamp (UTC): `2026-08-23T14:14:07.230214+00:00`
- Commit: `unavailable`
- Working tree: `unavailable`
- Fraud model SHA-256 (truncated as originally recorded): `bdfa0755413b58ec`
- Results SHA-256 (truncated as originally recorded): `55e2cea1db11416f`
- Reason: initial sealed evaluation.

## Read 2

- Timestamp (UTC): `2026-08-23T14:16:45.659320+00:00`
- Commit: `unavailable`
- Working tree: `unavailable`
- Fraud model SHA-256 (truncated as originally recorded): `bdfa0755413b58ec`
- Results SHA-256 (truncated as originally recorded): `87329a8297888bcd`
- Reason: report-generator defect fix. The policy-comparison section fed the learned candidate a zero-filled placeholder context instead of the trained context features. No model, threshold, detector parameter, or policy was changed. All transaction, event, and business metrics were byte-identical to read 1.

## Read 3

- Timestamp (UTC): `2026-08-23T14:19:48.496282+00:00`
- Commit: `unavailable`
- Working tree: `unavailable`
- Fraud model SHA-256 (truncated as originally recorded): `bdfa0755413b58ec`
- Results SHA-256 (truncated as originally recorded): `3798bc89f035a6dd`
- Reason: integrity wording correction in the generator: replaced an inaccurate single-read note with an explicit per-read disclosure. No model, threshold, detector parameter, or policy changed.

## Post-read renders

`report.md` was subsequently regenerated with `--render-only`, which reads only `results.json` and never touches sealed labels. Renders are not reads and add no entry here.
