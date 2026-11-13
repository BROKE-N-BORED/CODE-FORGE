name: recipe-database
description: Building-block recipe — give the owner's project a real local database (SQLite by default, Postgres when the project already runs it) with schema, migrations, and safe query helpers. Use when the owner asks for a database, storage, saving data, or records in a project being built.

# Recipe: Database — local-first data store (C10 building block)

A named, tested scaffold. Default is **SQLite** — one file, no
server, no account, in the standard library of every stack Forge
targets. Postgres only when the project already runs it or the
owner names it; never provision a cloud database. The recipe's
job is that asking for "a database" produces the same safe shape
every time instead of whatever the model fancies that day.

## The scaffold

1. **Location** — the database file lives in the project's data
   folder (`data/app.db`), created on first run, named in one
   config constant. Never scattered, never in the repo root by
   accident, and added to the project's ignore rules if it holds
   real user data.
2. **Schema as code** — a `SCHEMA` block (or `schema.sql`) with
   `CREATE TABLE IF NOT EXISTS` statements; every table gets an
   integer primary key; foreign keys declared, and
   `PRAGMA foreign_keys = ON` set on every connection.
3. **Migrations, not edits** — schema changes after first ship
   are numbered migration functions (`migrate_002_add_email`)
   run in order at startup, recorded in a `schema_version`
   table. Never edit a shipped CREATE TABLE and hope.
4. **Queries** — parameterized, always (`?` placeholders).
   String-built SQL with user data in it is a defect, full stop.
   A small helper (`db_query`, `db_execute`) wraps connections
   so no call site opens raw connections ad hoc.
5. **Backups** — one function, `backup_db()`, copying the file
   with a dated name into `data/backups/`. Mention it in the
   project's README notes when the recipe lands.

## Verify (not done until these pass)

- Fresh start creates the file and tables; second start changes
  nothing (idempotent).
- Insert a row, restart, read it back — data survives.
- A value containing quotes / SQL text is stored as data, not
  executed (parameterization check).
- `backup_db()` produces a copy that opens and reads.

## Never

No string-concatenated SQL with outside data. No database file
committed with real user data in it. No silent schema drift —
if the code and the schema disagree, the migration is missing.
