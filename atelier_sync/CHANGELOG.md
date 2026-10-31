# Changelog

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
