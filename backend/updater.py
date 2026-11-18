"""In-app updates for a copy installed with git (the setup script installs it that way)."""
import subprocess
import sys

from . import config

ROOT = config.ROOT


def _git(*args, timeout=60):
    p = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout,
                       creationflags=config.NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip()[-400:] or f"git {args[0]} failed")
    return p.stdout.strip()


def check() -> dict:
    if not (ROOT / ".git").exists():
        return {"can_update": False, "reason": "This copy wasn't installed with git, so it can't update itself."}
    try:
        _git("fetch", "--quiet", "origin")
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        behind = int(_git("rev-list", "--count", f"HEAD..origin/{branch}"))
        changes = _git("log", "--format=%s", f"HEAD..origin/{branch}").splitlines()[:10] if behind else []
        dirty = bool(_git("status", "--porcelain", "--untracked-files=no"))
    except FileNotFoundError:
        return {"can_update": False, "reason": "Git isn't installed on this PC."}
    except Exception as e:
        return {"can_update": False, "reason": f"Couldn't check for updates: {e}"}
    return {"can_update": True, "behind": behind, "changes": changes, "dirty": dirty, "branch": branch}


def apply() -> dict:
    info = check()
    if not info.get("can_update"):
        raise RuntimeError(info.get("reason"))
    if info["dirty"]:
        raise RuntimeError("Files in the Clipper folder were edited by hand, so the update was stopped to keep "
                           "those edits. Undo them (or ask whoever changed them) and try again.")
    if not info["behind"]:
        return {"updated": False, "message": "Clipper is already up to date."}
    before = _git("rev-parse", "HEAD")
    _git("pull", "--ff-only", timeout=180)
    changed = _git("diff", "--name-only", before, "HEAD").splitlines()
    notes = []
    if "requirements.txt" in changed:
        p = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"], cwd=ROOT,
                           capture_output=True, text=True, timeout=1800, creationflags=config.NO_WINDOW)
        if p.returncode != 0:
            raise RuntimeError("Updated, but installing the new Python packages failed: " + p.stderr[-400:])
        notes.append("installed new Python packages")
    if "package.json" in changed:
        p = subprocess.run("npm install --no-audit --no-fund", cwd=ROOT, shell=True, capture_output=True, text=True,
                           timeout=1800, creationflags=config.NO_WINDOW)
        if p.returncode != 0:
            raise RuntimeError("Updated, but npm install failed: " + p.stderr[-400:])
        notes.append("updated the app shell")
    return {"updated": True, "message": f"Updated ({info['behind']} change{'s' if info['behind'] != 1 else ''})"
            + (f", {', '.join(notes)}" if notes else "") + ". Restart Clipper to use it."}
