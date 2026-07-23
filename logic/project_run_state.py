"""Persistent project-input fingerprints and simulation run status."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from logic.project_paths import get_project_dir, get_project_results_dir


_IGNORED_INPUT_FILES = {"input_done.flag", "simulation_status.json"}


def project_input_fingerprint(project_name: str) -> str:
    """Hash all durable project inputs, independent of file timestamps."""
    root = Path(get_project_dir(project_name))
    digest = hashlib.sha256()
    if not root.exists():
        return digest.hexdigest()

    for path in sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p: p.as_posix()):
        if path.name in _IGNORED_INPUT_FILES:
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def run_status_path(project_name: str) -> Path:
    return Path(get_project_results_dir(project_name)) / "simulation_status.json"


def read_run_status(project_name: str) -> dict:
    path = run_status_path(project_name)
    try:
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def write_run_status(project_name: str, state: str, **details) -> None:
    path = run_status_path(project_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "project_name": project_name,
        "state": state,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **details,
    }
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    temporary.replace(path)


def results_match_current_inputs(project_name: str, status: dict | None = None) -> bool:
    status = status or read_run_status(project_name)
    completed_fingerprint = status.get("input_fingerprint") if status.get("state") == "completed" else None
    return bool(completed_fingerprint) and completed_fingerprint == project_input_fingerprint(project_name)
