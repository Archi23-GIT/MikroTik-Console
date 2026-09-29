# Backups and versioning

The current application version is recorded in `VERSION`. Before changing application code, runtime configuration, or behavior in a later task, create an immutable snapshot under `backup/` using this pattern:

```text
backup/v<VERSION>_<YYYY-MM-DD>/
```

Copy all application scripts (including `mikrotik-console.py` and `mikrotik-config-builder.py` when present), `README.md`, `devices.example.json`, `devices.json`, `requirements.txt` (when present), and `VERSION` into that snapshot before editing. Never overwrite an existing snapshot; if the same version and date already exist, add a numeric suffix. Then update `VERSION` for the resulting application: increment the patch number for fixes and the minor number for new features (for example, `1.0` → `1.0.1` or `1.1`).

The snapshot contains the active device configuration so this local project can be restored as it was. It may reveal private network addresses and usernames; keep the backup directory private.

The initial snapshots are `backup/v1.0_2026-09-29/` and `backup/v1.0_2026-09-29_2/`; subsequent snapshots are created before each change.
