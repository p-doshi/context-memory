# 0001: Storage layout outside synced folders

Date: 2026-10-24
Status: accepted

## Context

The design document started in a OneDrive folder. SQLite (WAL mode) and LanceDB
write many small files and rely on file locks; sync clients lock and upload files
mid-write, which can produce conflicted copies or corrupt databases. A Python
virtual environment adds tens of thousands of files that sync for no benefit.

## Decision

On the development laptop everything lives under `C:\JARVIS`:

| Path | Contents | In git |
|---|---|---|
| `C:\JARVIS\context-memory` | this repository, plus `.venv` | yes (not `.venv`) |
| `C:\JARVIS\data` | SQLite metadata, LanceDB index (`CMH_DATA_DIR`) | no |
| `C:\JARVIS\models` | Hugging Face model cache (`CMH_MODEL_DIR`) | no |

The code default for `CMH_DATA_DIR` is `%LOCALAPPDATA%\context-memory\data` (or
`~/.local/share/context-memory/data`), so a fresh clone works on any machine
without configuration. Model weights are pinned by Hugging Face commit through
`CMH_EMBEDDING_MODEL_REVISION`.

## Consequences

- The data directory is not backed up by OneDrive. Backups are a roadmap item
  (Week 6); until then, a copy of `C:\JARVIS\data` is the backup.
- Only one physical disk exists (512 GB NVMe). TB-scale research datasets cannot
  live on this machine and must stay on external or remote storage, accessed
  through the dataset catalog.
