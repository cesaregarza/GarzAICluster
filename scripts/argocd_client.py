"""Resolve the repository-pinned Argo CD client without eager filesystem lookup."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import NamedTuple


class ArgocdResolution(NamedTuple):
    path: str
    source: str


def _pinned_path(pinned_version: str) -> Path:
    return Path("/root/dev/.tools") / f"argocd-v{pinned_version}" / "argocd"


def _runnable_path(value: str) -> str:
    """Return an absolute runnable path, resolving command names through PATH."""
    if not value:
        raise FileNotFoundError("empty executable path")
    candidate = shutil.which(value) if "/" not in value else value
    if candidate is None:
        raise FileNotFoundError(f"{value!r} is not on PATH")
    path = Path(candidate).expanduser().resolve(strict=True)
    if not path.is_file():
        raise FileNotFoundError(f"executable is not a file: {path}")
    if not os.access(path, os.X_OK):
        raise PermissionError(f"executable is not runnable: {path}")
    return str(path)


def _invalid_selected(source: str, value: str, error: OSError) -> FileNotFoundError:
    return FileNotFoundError(
        f"Argo CD executable from {source} is invalid ({value!r}): {error}; "
        "refusing to fall back"
    )


def resolve_argocd(explicit: str | None, pinned_version: str) -> ArgocdResolution:
    """Select an executable in strict precedence order.

    User-selected values are fail-closed. PATH and the managed pinned candidate
    are opportunistic defaults, so an unusable default can fall through.
    """
    pinned = _pinned_path(pinned_version)
    if explicit is not None:
        try:
            return ArgocdResolution(_runnable_path(explicit), "explicit --argocd-bin")
        except OSError as error:
            raise _invalid_selected("--argocd-bin/--argocd", explicit, error) from error

    environment_value = os.environ.get("ARGOCD_BIN")
    if environment_value is not None:
        try:
            return ArgocdResolution(_runnable_path(environment_value), "ARGOCD_BIN")
        except OSError as error:
            raise _invalid_selected("ARGOCD_BIN", environment_value, error) from error

    try:
        path_value = shutil.which("argocd")
        if path_value is not None:
            try:
                return ArgocdResolution(_runnable_path(path_value), "PATH")
            except OSError:
                pass
    except OSError:
        pass

    try:
        return ArgocdResolution(_runnable_path(str(pinned)), "pinned")
    except OSError as error:
        raise FileNotFoundError(
            "could not find a runnable Argo CD client; checked "
            f"--argocd-bin/--argocd, ARGOCD_BIN, PATH (argocd), and pinned "
            f"{pinned}; expected repository version {pinned_version}"
        ) from error


def emit_preflight(resolution: ArgocdResolution) -> None:
    """Print the safe selected executable and resolution source."""
    print(
        json.dumps(
            {
                "stage": "argocd-preflight",
                "result": "selected",
                "selected_path": resolution.path,
                "source": resolution.source,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
