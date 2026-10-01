"""Crash-safe JSON checkpoints for the unattended publishing pipeline."""

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


def atomic_write_json(path, data, *, sort_keys=False):
    """Replace a checkpoint only after a complete new JSON file is on disk.

    Serialization, disk errors and interrupted writes leave the previous file
    intact. Callers still own the production lock and error/retry policy.
    """
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=sort_keys) + "\n"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
