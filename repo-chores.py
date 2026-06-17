#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""repo-chores — housekeeping checks across our production Laravel apps.

A repo is treated as a *production* Laravel app when, in the code folder, it:
  1. is a git repo, AND
  2. has a remote whose host is our on-prem GitLab (set REPO_CHORES_GITLAB_HOST), AND
  3. has an `artisan` file (so it really is a Laravel app, not a side-project).

For each such app we look at every recognised remote independently — the GitLab
one and any github.com mirror — so drift between them is easy to spot, and for
each we report:
  - the project path (group/org + name)
  - the repo description (and whether it carries the right **[L<major>.x]** prefix)
  - the actual Laravel version from composer.json on the default branch
  - whether it still has the default boilerplate README (Y = has a custom one)

All reads go through the authenticated `gh` and `glab` CLIs. Everything is
read-only except `update-descriptions --apply`, which writes repo descriptions.

Configuration — via environment variables, or a .env file in this directory
(see dotenv.example):
    REPO_CHORES_GITLAB_HOST   your on-prem GitLab host, e.g. gitlab.example.com  (required)
    REPO_CHORES_CODE_DIR      folder to scan for apps  (optional; default ~/Documents/code)

Usage — run with uv, or plain `python3 repo-chores.py ...`:
    uv run repo-chores.py report
    uv run repo-chores.py readme-check
    uv run repo-chores.py update-descriptions            # dry-run
    uv run repo-chores.py update-descriptions --apply     # actually writes
    uv run repo-chores.py report --only some-app          # filter to one app
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

# --- configuration -----------------------------------------------------------
# Config comes from environment variables, optionally seeded from a .env file in
# the current directory. We use python-dotenv if it's installed, but fall back
# to a tiny built-in reader so the tool needs no third-party dependency at all.

def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
        return
    except ImportError:
        pass
    env_path = Path(".env")
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # Real environment variables always win over .env values.
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_dotenv()

# Your on-prem GitLab host, e.g. "gitlab.example.com". REQUIRED — set it via the
# REPO_CHORES_GITLAB_HOST env var or a .env file (see dotenv.example).
GITLAB_HOST = os.environ.get("REPO_CHORES_GITLAB_HOST", "")
GITHUB_HOST = "github.com"
# Folder scanned for apps; override with REPO_CHORES_CODE_DIR or the --path flag.
DEFAULT_ROOT = Path(os.environ.get("REPO_CHORES_CODE_DIR") or Path.home() / "Documents" / "code")

# A README that still contains this stock sentence is treated as un-customised.
BOILERPLATE_MARKERS = (
    "Laravel is a web application framework with expressive, elegant syntax",
)

# --- prefix convention -------------------------------------------------------
# New convention: a version tag at the very start of the description.
#   - GitLab renders markdown, so we use a *bold* tag: "**[L12.x]**".
#   - GitHub descriptions are plain text (no markdown), so we use "[L12.x]".
# When rewriting a description we first strip the old cruft:
#   - old version tags, bold or not: **[5.8]**, [9.0], **[12.x]**, **[11.x ]**
#     (and the new **[L12.x]** form too, so re-running is idempotent)
#   - the legacy "a new branch is waiting" target notes, e.g. (✨🌱 _10.x_),
#     now obsolete since upgrades are quick (Laravel Shift etc.)
# Genuine prose — including **NOTE: …** warnings — is kept.

# A leading version tag: optional bold, optional "L", a version number.
_VERSION_TAG_RE = re.compile(r"\*{0,2}\[\s*L?\d+(?:\.\d+|\.x|x)?\s*\]\*{0,2}", re.IGNORECASE)
# A legacy "🌱 target version" note, e.g. (✨🌱 _10.x_) or _(✨🌱 11.x)_.
_TARGET_NOTE_RE = re.compile(r"_?\([^)]*[🌱✨][^)]*\)_?")
# Dash glue between the old markers (" - ", "–", "—").
_SEP_RE = re.compile(r"[-–—]+")


# --- data ---------------------------------------------------------------------

@dataclass
class Remote:
    platform: str   # "gitlab" | "github"
    host: str
    path: str       # e.g. "group/project-name"


@dataclass
class RemoteInfo:
    remote: Remote
    reachable: bool = True
    error: str | None = None
    description: str | None = None
    default_branch: str | None = None
    composer_constraint: str | None = None
    laravel: str | None = None            # e.g. "L13.x"
    has_custom_readme: bool | None = None


@dataclass
class App:
    directory: Path
    remotes: list[Remote]


# --- shelling out -------------------------------------------------------------

def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


# --- discovery ----------------------------------------------------------------

def parse_remote_url(url: str) -> tuple[str, str] | None:
    """Return (host, path) from a git remote URL, or None if unparseable.

    Handles scp-style (git@host:owner/repo.git), ssh:// and https:// forms.
    """
    url = url.strip()
    # scp-like: git@host:owner/repo.git
    m = re.match(r"^[\w.+-]+@([^:/]+):(.+?)(?:\.git)?/?$", url)
    if m:
        return m.group(1), m.group(2)
    # ssh:// or https:// (optional user@, optional :port)
    m = re.match(r"^(?:ssh|https?)://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+?)(?:\.git)?/?$", url)
    if m:
        return m.group(1), m.group(2)
    return None


def git_remote_urls(directory: Path) -> list[str]:
    res = run(["git", "-C", str(directory), "remote", "-v"])
    if res.returncode != 0:
        return []
    urls = set()
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            urls.add(parts[1])
    return sorted(urls)


def classify_remotes(urls: list[str]) -> list[Remote]:
    """Keep only our GitLab + GitHub remotes, de-duplicated by (platform, path)."""
    remotes: list[Remote] = []
    seen: set[tuple[str, str]] = set()
    for url in urls:
        parsed = parse_remote_url(url)
        if not parsed:
            continue
        host, path = parsed
        if host == GITLAB_HOST:
            platform = "gitlab"
        elif host == GITHUB_HOST:
            platform = "github"
        else:
            continue
        key = (platform, path)
        if key in seen:
            continue
        seen.add(key)
        remotes.append(Remote(platform, host, path))
    # GitLab first so it reads consistently in the report.
    remotes.sort(key=lambda r: (r.platform != "gitlab", r.path))
    return remotes


def discover_apps(root: Path) -> list[App]:
    apps: list[App] = []
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        if not (child / ".git").exists():
            continue
        remotes = classify_remotes(git_remote_urls(child))
        has_gitlab = any(r.platform == "gitlab" for r in remotes)
        if has_gitlab and (child / "artisan").exists():
            apps.append(App(directory=child, remotes=remotes))
    return apps


# --- platform APIs ------------------------------------------------------------

def gh_json(api_path: str) -> dict | None:
    res = run(["gh", "api", api_path])
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return None


def gh_raw_file(repo_path: str, filepath: str, ref: str) -> str | None:
    """Raw file contents from a GitHub repo, or None if absent/unreadable."""
    res = run([
        "gh", "api",
        f"repos/{repo_path}/contents/{urllib.parse.quote(filepath)}?ref={ref}",
        "-H", "Accept: application/vnd.github.raw",
    ])
    return res.stdout if res.returncode == 0 else None


def glab_json(api_path: str) -> dict | None:
    res = run(["glab", "api", "--hostname", GITLAB_HOST, api_path])
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return None


def glab_raw_file(project_path: str, filepath: str, ref: str) -> str | None:
    enc_project = urllib.parse.quote(project_path, safe="")
    enc_file = urllib.parse.quote(filepath, safe="")
    res = run([
        "glab", "api", "--hostname", GITLAB_HOST,
        f"projects/{enc_project}/repository/files/{enc_file}/raw?ref={ref}",
    ])
    return res.stdout if res.returncode == 0 else None


# --- analysis -----------------------------------------------------------------

def laravel_version(composer_json: str) -> tuple[str | None, str | None]:
    """Return (constraint, "L<major>.x") from composer.json's laravel/framework."""
    try:
        data = json.loads(composer_json)
    except json.JSONDecodeError:
        return None, None
    constraint = (data.get("require") or {}).get("laravel/framework")
    if not constraint:
        return None, None
    majors = [int(tok.split(".")[0]) for tok in re.findall(r"\d+(?:\.\d+)*", constraint)]
    if not majors:
        return constraint, None
    return constraint, f"L{max(majors)}.x"


def is_custom_readme(readme: str | None) -> bool:
    """True if the README looks bespoke; False if missing or stock boilerplate."""
    if not readme or not readme.strip():
        return False
    return not any(marker in readme for marker in BOILERPLATE_MARKERS)


def expected_prefix(laravel: str | None, platform: str) -> str | None:
    """Version prefix for a platform: bold on GitLab, plain on GitHub."""
    if not laravel:
        return None
    return f"**[{laravel}]**" if platform == "gitlab" else f"[{laravel}]"


def strip_legacy_markers(description: str | None) -> str:
    """Peel leading version tags and 🌱 target notes; keep the real prose."""
    text = (description or "").strip()
    changed = True
    while changed and text:
        changed = False
        for pattern in (_VERSION_TAG_RE, _TARGET_NOTE_RE, _SEP_RE):
            m = pattern.match(text)
            if m and m.end() > 0:
                text = text[m.end():].lstrip()
                changed = True
    # Collapse any internal newlines/whitespace runs to single spaces.
    return re.sub(r"\s+", " ", text).strip()


def desired_description(current: str | None, laravel: str | None, platform: str) -> str | None:
    """The description we *want*: platform prefix + the cleaned-up body."""
    prefix = expected_prefix(laravel, platform)
    if not prefix:
        return None
    body = strip_legacy_markers(current)
    return f"{prefix} {body}".strip()


def gather_remote(remote: Remote) -> RemoteInfo:
    """Fetch everything we report for a single remote (read-only)."""
    info = RemoteInfo(remote=remote)
    if remote.platform == "github":
        meta = gh_json(f"repos/{remote.path}")
    else:
        meta = glab_json(f"projects/{urllib.parse.quote(remote.path, safe='')}")

    if meta is None:
        info.reachable = False
        info.error = "API unreachable (offline / VPN? / not authenticated / not found)"
        return info

    info.description = meta.get("description") or None
    info.default_branch = meta.get("default_branch")
    ref = info.default_branch or "master"

    if remote.platform == "github":
        composer = gh_raw_file(remote.path, "composer.json", ref)
        readme = gh_raw_file(remote.path, "README.md", ref)
    else:
        composer = glab_raw_file(remote.path, "composer.json", ref)
        readme = glab_raw_file(remote.path, "README.md", ref)

    if composer:
        info.composer_constraint, info.laravel = laravel_version(composer)
    info.has_custom_readme = is_custom_readme(readme)
    return info


def description_status(info: RemoteInfo) -> str:
    prefix = expected_prefix(info.laravel, info.remote.platform)
    if not prefix:
        return "[Laravel version unknown]"
    if info.description and info.description.startswith(prefix):
        return "[OK]"
    return f"[MISMATCH — expected {prefix}]"


# --- writes (only via update-descriptions --apply) ----------------------------

def set_description(remote: Remote, description: str) -> tuple[bool, str]:
    if remote.platform == "github":
        res = run(["gh", "api", "-X", "PATCH", f"repos/{remote.path}",
                   "-f", f"description={description}"])
    else:
        enc = urllib.parse.quote(remote.path, safe="")
        res = run(["glab", "api", "--hostname", GITLAB_HOST, "-X", "PUT",
                   f"projects/{enc}", "-f", f"description={description}"])
    if res.returncode != 0:
        return False, (res.stderr or res.stdout).strip()[:200]
    return True, ""


# --- output formats -----------------------------------------------------------

REPORT_FIELDS = [
    "directory", "platform", "path", "laravel", "default_branch",
    "prefix_ok", "custom_readme", "description", "reachable", "error",
]
READMECHECK_FIELDS = ["directory", "platform", "path"]
UPDATE_FIELDS = ["directory", "platform", "path", "from", "to", "result"]


def emit_csv(fieldnames: list[str], rows: list[dict]) -> None:
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)


def emit_json(rows: list[dict]) -> None:
    json.dump(rows, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def report_row(app: App, info: RemoteInfo) -> dict:
    """Flatten a gathered remote into one row for csv/json output."""
    if not info.reachable:
        return {
            "directory": app.directory.name, "platform": info.remote.platform,
            "path": info.remote.path, "reachable": "N", "error": info.error or "",
        }
    prefix = expected_prefix(info.laravel, info.remote.platform)
    if not prefix:
        prefix_ok = "?"
    elif info.description and info.description.startswith(prefix):
        prefix_ok = "Y"
    else:
        prefix_ok = "N"
    return {
        "directory": app.directory.name, "platform": info.remote.platform,
        "path": info.remote.path, "laravel": info.laravel or "",
        "default_branch": info.default_branch or "", "prefix_ok": prefix_ok,
        "custom_readme": "Y" if info.has_custom_readme else "N",
        "description": info.description or "", "reachable": "Y", "error": "",
    }


# --- subcommands --------------------------------------------------------------

def cmd_report(apps: list[App], args: argparse.Namespace) -> None:
    gathered = [(app, [gather_remote(r) for r in app.remotes]) for app in apps]

    if args.format != "text":
        rows: list[dict] = []
        for app, infos in gathered:
            if not infos:
                rows.append({"directory": app.directory.name,
                             "error": "no GitLab/GitHub remotes recognised"})
            else:
                rows.extend(report_row(app, info) for info in infos)
        if args.format == "csv":
            emit_csv(REPORT_FIELDS, rows)
        else:
            emit_json(rows)
        return

    print(f"Found {len(apps)} production Laravel app(s) under {args.path}\n")
    for app, infos in gathered:
        print("=" * 72)
        print(app.directory.name)
        print("-" * 72)
        if not infos:
            print("  (no GitLab/GitHub remotes recognised)")
            print()
            continue
        for info in infos:
            label = "GitLab" if info.remote.platform == "gitlab" else "GitHub"
            print(f"  {label}  {info.remote.path}")
            if not info.reachable:
                print(f"          (unreachable: {info.error})")
                continue
            print(f"          laravel : {info.laravel or '?'}   "
                  f"(composer.json @ {info.default_branch or '?'})")
            print(f"          desc    : {info.description or '(none)'}   "
                  f"{description_status(info)}")
            print(f"          readme  : "
                  f"{'Y (custom)' if info.has_custom_readme else 'N (boilerplate/none)'}")
        print()


def cmd_readme_check(apps: list[App], args: argparse.Namespace) -> None:
    flagged: list[dict] = []
    for app in apps:
        for remote in app.remotes:
            info = gather_remote(remote)
            if info.reachable and info.has_custom_readme is False:
                flagged.append({"directory": app.directory.name,
                                "platform": remote.platform, "path": remote.path})

    if args.format == "csv":
        emit_csv(READMECHECK_FIELDS, flagged)
        return
    if args.format == "json":
        emit_json(flagged)
        return

    if not flagged:
        print("All reachable repos have a custom README. 🎉")
        return
    print("Repos still using the default/boilerplate README (or none at all):\n")
    for row in flagged:
        print(f"  {row['platform']:6}  {row['path']}   (dir: {row['directory']})")


def cmd_update_descriptions(apps: list[App], args: argparse.Namespace) -> None:
    changes: list[dict] = []
    for app in apps:
        for remote in app.remotes:
            info = gather_remote(remote)
            if not info.reachable or not info.laravel:
                continue
            prefix = expected_prefix(info.laravel, remote.platform)
            if info.description and info.description.startswith(prefix):
                continue  # already correct
            new = desired_description(info.description, info.laravel, remote.platform)
            if not new:
                continue
            row = {"directory": app.directory.name, "platform": remote.platform,
                   "path": remote.path, "from": info.description or "", "to": new,
                   "result": "dry-run"}
            if args.apply:
                ok, err = set_description(remote, new)
                row["result"] = "updated" if ok else f"failed: {err}"
            changes.append(row)

    if args.format == "csv":
        emit_csv(UPDATE_FIELDS, changes)
        return
    if args.format == "json":
        emit_json(changes)
        return

    if not changes:
        print("Every reachable repo description already carries the right prefix. 🎉")
        return
    mode = "APPLYING CHANGES" if args.apply else "DRY-RUN — no writes (re-run with --apply to write)"
    print(f"{len(changes)} description change(s) — {mode}\n")
    for row in changes:
        print(f"  {row['platform']:6}  {row['path']}")
        print(f"          from: {row['from'] or '(none)'}")
        print(f"          to  : {row['to']}")
        if args.apply:
            mark = "✓" if row["result"] == "updated" else "✗"
            print(f"          {mark} {row['result']}")
        print()


# --- entrypoint ---------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--path", type=Path, default=DEFAULT_ROOT,
                        help=f"Code folder to scan (default: {DEFAULT_ROOT})")
    common.add_argument("--only",
                        help="Only process apps whose directory name contains this substring.")
    common.add_argument("--format", choices=["text", "csv", "json"], default="text",
                        help="Output format (default: text).")

    parser = argparse.ArgumentParser(
        description="Housekeeping chores for our production Laravel apps.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("report", parents=[common],
                   help="Plain-text report of every app's GitLab + GitHub state.")
    sub.add_parser("readme-check", parents=[common],
                   help="List repos still using the default/boilerplate README.")
    up = sub.add_parser("update-descriptions", parents=[common],
                        help="Fix [L..x] description prefixes (dry-run unless --apply).")
    up.add_argument("--apply", action="store_true",
                    help="Actually write the new descriptions (otherwise dry-run).")
    return parser


def main() -> None:
    args = build_parser().parse_args()

    if not GITLAB_HOST:
        sys.exit(
            "REPO_CHORES_GITLAB_HOST is not set.\n"
            "Set your on-prem GitLab host as an environment variable:\n"
            "    export REPO_CHORES_GITLAB_HOST=gitlab.example.com\n"
            "or add it to a .env file in this directory (see dotenv.example):\n"
            "    REPO_CHORES_GITLAB_HOST=gitlab.example.com"
        )

    args.path = args.path.expanduser()
    if not args.path.is_dir():
        sys.exit(f"Code folder not found: {args.path}")

    apps = discover_apps(args.path)
    if args.only:
        apps = [a for a in apps if args.only in a.directory.name]

    handlers = {
        "report": cmd_report,
        "readme-check": cmd_readme_check,
        "update-descriptions": cmd_update_descriptions,
    }
    handlers[args.command](apps, args)


if __name__ == "__main__":
    main()
