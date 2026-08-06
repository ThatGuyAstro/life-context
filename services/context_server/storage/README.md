# Context storage

This directory is an isolated SQLite storage layer. It owns no HTTP routes or
server lifecycle and can be used by route/worker adapters from outside this
directory.

## Defaults

- Migrations are forward-only and recorded in `schema_migrations` (current
  version: 3).
- Sources, captures, observations, jobs, artifacts, and artifact-reference
  tables use foreign keys and idempotency keys where an ingestion retry needs
  a stable identity.
- FTS5 indexes are maintained by SQLite triggers for observations, captures,
  sources, and artifact metadata. `rebuild_fts_indexes()` repairs indexes after
  an interrupted import or an older database upgrade.
- Artifact bytes are immutable SHA-256 content-addressed files at
  `<artifact-root>/<first-two>/<next-two>/<digest>`. Writes stage, fsync, and
  atomically rename the file before registering metadata.
- Retention is `raw = 24 hours`, `keyframe = 30 days`, and `derived =
indefinite`. `purge_expired()` removes expired observations and unreferenced
  artifacts; referenced artifacts are reported and retained.

## sqlite-vec assumption

sqlite-vec is optional. On startup the package tries the official Python
`sqlite_vec` package and its `loadable_path()` only when extension loading is
available. A caller may provide an explicit platform extension path instead.
The package does not install or download the extension. Every outcome is
persisted in `storage_features` as `available`, `unavailable`, or `disabled`,
with a human-readable reason, and vector-index creation raises
`VectorFeatureUnavailable` while it is unavailable. The vector virtual-table
syntax assumes a compatible sqlite-vec `vec0` build when a caller explicitly
creates an index.
