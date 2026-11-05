# Changelog

## 0.4.0

- Adds `salon-logo-v1` on status, registration and sync responses.
- Preserves optional `settings.workspace.logoData` in synchronization, bootstrap and backups. A PNG data URL is limited to 512 × 512 pixels and 512 KiB; explicit `null` restores the built-in logo. Legacy records keep the field absent.
- Validates canonical base64, PNG signature/header, chunk bounds/CRC and terminal IEND. External URLs, SVG, malformed images and animated PNG are rejected.
- Prevents older-device settings from silently removing a shared logo or reset marker; explicit conflicts/rejections retain the previous agenda atomically. Ordinary legacy edits remain supported.
- Keeps existing SQLite schema, device identities, tokens, receipts and business records without migration or reset.

## 0.3.0

- Adds the `client-deletion-v1` capability alongside `suppliers-v1` on status, registration and sync responses.
- Preserves an optional strictly boolean `clients[].deleted` field. A deleted identity remains archived with an empty phone and contact notes, retaining its ID and name for appointment and correction references.
- Rejects silent revival, new bookings and reassignment to a deleted identity. Existing bookings for the same identity remain editable under the existing accounting/audit rules.
- Keeps legacy clients without the field unchanged, permits physical deletion only after all current/audit references are absent, and accepts valid bootstrap backups containing deleted identities and historical bookings.
- Uses the existing schema-1 records, CAS operations and SQLite database. No reset, cascade deletion, pairing change or receipt rewrite is required.

## 0.2.0

- Adds a shared supplier catalog and supplier name snapshots on expenses, with the `suppliers-v1` capability.

## 0.1.0

- Adds persistent per-record CAS synchronization, offline operation receipts, audit preservation and configurable automatic SQLite backups.
