"""Workspace-confined paths for visualization state and future artifacts."""

from pathlib import Path


def confined_path(root: Path, path: Path) -> Path:
    root = root.resolve()
    candidate = path if path.is_absolute() else root / path
    resolved = candidate.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError("visualization_path_outside_workspace")
    # Reject aliases even when they currently point inside the workspace.
    current = candidate.absolute()
    while current != root:
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise ValueError("visualization_path_alias")
        if current.parent == current:
            raise ValueError("visualization_path_outside_workspace")
        current = current.parent
    return resolved
