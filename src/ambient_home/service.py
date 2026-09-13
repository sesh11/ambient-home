"""Install ambient-home and the Reachy daemon as macOS LaunchAgents."""

import os
import sys
import shutil
import string
import argparse
import platform
import subprocess
from pathlib import Path
from dataclasses import dataclass
from importlib.resources import files


DAEMON_LABEL = "ai.ambient-home.daemon"
APP_LABEL = "ai.ambient-home.app"
LABELS = (DAEMON_LABEL, APP_LABEL)

_UV_CANDIDATES = (
    Path("~/.local/bin/uv"),
    Path("/opt/homebrew/bin/uv"),
    Path("/usr/local/bin/uv"),
)


@dataclass(frozen=True)
class ServicePaths:
    """Per-machine values substituted into the LaunchAgent templates."""

    repo_dir: Path
    uv_bin: Path
    home: Path
    log_dir: Path
    agents_dir: Path

    @property
    def path(self) -> str:
        """PATH exported to the agents so ``uv`` and its tools resolve."""
        entries = [
            str(self.uv_bin.parent),
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ]
        unique: list[str] = []
        for entry in entries:
            if entry not in unique:
                unique.append(entry)
        return ":".join(unique)

    def plist_path(self, label: str) -> Path:
        """Return where the agent plist for ``label`` is installed."""
        return self.agents_dir / f"{label}.plist"

    def substitutions(self) -> dict[str, str]:
        """Return the template variables."""
        return {
            "repo_dir": str(self.repo_dir),
            "uv_bin": str(self.uv_bin),
            "home": str(self.home),
            "log_dir": str(self.log_dir),
            "path": self.path,
        }


def find_uv() -> Path:
    """Locate the ``uv`` binary, preferring the one on PATH."""
    found = shutil.which("uv")
    if found:
        return Path(found).resolve()
    for candidate in _UV_CANDIDATES:
        expanded = candidate.expanduser()
        if expanded.is_file():
            return expanded.resolve()
    raise FileNotFoundError("uv not found on PATH; install uv or pass --uv-bin")


def _resolve_uv(uv_bin: Path | None, require_uv: bool) -> Path:
    if uv_bin is not None:
        return uv_bin.expanduser().resolve()
    try:
        return find_uv()
    except FileNotFoundError:
        if require_uv:
            raise
        return Path("uv")


def resolve_paths(
    repo_dir: Path | None = None,
    uv_bin: Path | None = None,
    home: Path | None = None,
    require_uv: bool = True,
) -> ServicePaths:
    """Resolve machine-specific paths for the LaunchAgents."""
    resolved_repo = (repo_dir or Path.cwd()).resolve()
    if not (resolved_repo / "pyproject.toml").is_file():
        raise FileNotFoundError(f"{resolved_repo} does not contain pyproject.toml; run from the ambient-home checkout")
    if not (resolved_repo / "scripts" / "run-daemon.sh").is_file():
        raise FileNotFoundError(f"{resolved_repo}/scripts/run-daemon.sh is missing")
    resolved_home = (home or Path.home()).resolve()
    return ServicePaths(
        repo_dir=resolved_repo,
        uv_bin=_resolve_uv(uv_bin, require_uv),
        home=resolved_home,
        log_dir=resolved_home / "Library" / "Logs" / "ambient-home",
        agents_dir=resolved_home / "Library" / "LaunchAgents",
    )


def render_plist(label: str, paths: ServicePaths) -> str:
    """Render the LaunchAgent plist for ``label`` from the bundled template."""
    template = files("ambient_home").joinpath("launchd", f"{label}.plist").read_text(encoding="utf-8")
    return string.Template(template).substitute(paths.substitutions())


def launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a launchctl subcommand."""
    return subprocess.run(["launchctl", *args], check=check, capture_output=True, text=True)


def domain() -> str:
    """Return the launchd GUI domain for the current user."""
    return f"gui/{os.getuid()}"


def install(paths: ServicePaths, *, dry_run: bool = False) -> list[Path]:
    """Write the agent plists and bootstrap them into launchd."""
    written: list[Path] = []
    if not dry_run:
        paths.log_dir.mkdir(parents=True, exist_ok=True)
        paths.agents_dir.mkdir(parents=True, exist_ok=True)
    for label in LABELS:
        content = render_plist(label, paths)
        target = paths.plist_path(label)
        if dry_run:
            print(f"# {target}\n{content}")
        else:
            target.write_text(content, encoding="utf-8")
        written.append(target)
    if dry_run:
        return written
    for label in LABELS:
        launchctl("bootout", f"{domain()}/{label}", check=False)
        result = launchctl("bootstrap", domain(), str(paths.plist_path(label)), check=False)
        if result.returncode != 0:
            raise RuntimeError(f"launchctl bootstrap {label} failed: {result.stderr.strip() or result.stdout.strip()}")
        print(f"installed {label} -> {paths.plist_path(label)}")
    print(f"logs: {paths.log_dir}/daemon.log, {paths.log_dir}/app.log")
    return written


def uninstall(paths: ServicePaths, *, dry_run: bool = False) -> list[Path]:
    """Remove the agents from launchd and delete their plists."""
    removed: list[Path] = []
    for label in reversed(LABELS):
        target = paths.plist_path(label)
        if dry_run:
            print(f"would bootout {domain()}/{label} and remove {target}")
            removed.append(target)
            continue
        launchctl("bootout", f"{domain()}/{label}", check=False)
        if target.exists():
            target.unlink()
            removed.append(target)
        print(f"removed {label}")
    return removed


def _parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--repo-dir", type=Path, default=None, help="ambient-home checkout (default: cwd)")
    parser.add_argument("--uv-bin", type=Path, default=None, help="path to uv (default: from PATH)")
    parser.add_argument("--dry-run", action="store_true", help="print what would be done without touching launchd")
    return parser


def _require_macos(dry_run: bool) -> None:
    if platform.system() != "Darwin" and not dry_run:
        raise SystemExit("LaunchAgents are macOS-only; use --dry-run elsewhere to preview the plists")


def install_main() -> int:
    """Entry point for ``ambient-install-service``."""
    args = _parser("Install ambient-home and reachy-mini-daemon as macOS LaunchAgents.").parse_args()
    _require_macos(args.dry_run)
    try:
        paths = resolve_paths(args.repo_dir, args.uv_bin)
        install(paths, dry_run=args.dry_run)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def uninstall_main() -> int:
    """Entry point for ``ambient-uninstall-service``."""
    args = _parser("Remove the ambient-home LaunchAgents.").parse_args()
    _require_macos(args.dry_run)
    try:
        paths = resolve_paths(args.repo_dir, args.uv_bin, require_uv=False)
        uninstall(paths, dry_run=args.dry_run)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
