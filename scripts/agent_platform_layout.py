"""Resolve the supported source layouts of the pinned agent-platform checkout."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class AgentPlatformLayoutError(ValueError):
    """Raised when a checkout has no unique supported source layout."""


@dataclass(frozen=True)
class AgentPlatformLayout:
    """The unique source package root selected for an agent-platform checkout."""

    repo_root: Path
    package_root: Path
    relative_root: Path

    @property
    def import_root(self) -> Path:
        """Return the path that must be inserted for ``import mandate``."""
        return self.package_root.parent

    def path_for(self, relative_path: Path) -> Path:
        """Resolve a path relative to the ``mandate`` package."""
        return self.package_root / relative_path

    def label_for(self, relative_path: Path) -> str:
        """Return the checkout-relative path used in diagnostics."""
        return (self.relative_root / relative_path).as_posix()

    def path_for_module(self, module_name: str) -> Path:
        """Resolve a dotted ``mandate.*`` module to its source file."""
        parts = module_name.split(".")
        if len(parts) < 2 or parts[0] != "mandate":
            raise AgentPlatformLayoutError(
                f"agent record module is outside mandate package: {module_name}"
            )
        return self.path_for(Path(*parts[1:]).with_suffix(".py"))


def resolve_agent_platform_layout(
    repo_root: Path,
    *,
    required_paths: tuple[Path, ...] = (),
) -> AgentPlatformLayout:
    """Select exactly one old or ``src/mandate`` package layout.

    A checkout containing both package roots is rejected even when one is
    incomplete, since silently choosing one could validate a different source
    tree than the pinned revision.
    """
    root = repo_root.resolve()
    candidates = (
        (root / "mandate", Path("mandate")),
        (root / "src" / "mandate", Path("src/mandate")),
    )
    present = [
        (package_root, relative_root)
        for package_root, relative_root in candidates
        if package_root.is_dir()
    ]
    if len(present) != 1:
        if not present:
            raise AgentPlatformLayoutError(
                "agent-platform checkout has no supported mandate source layout"
            )
        raise AgentPlatformLayoutError(
            "agent-platform checkout has ambiguous mandate source layouts"
        )
    package_root, relative_root = present[0]
    missing = next(
        (
            relative_path
            for relative_path in required_paths
            if not (package_root / relative_path).is_file()
        ),
        None,
    )
    if missing is not None:
        raise AgentPlatformLayoutError(
            f"agent-platform checkout is missing {relative_root / missing}"
        )
    return AgentPlatformLayout(root, package_root, relative_root)
