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
    uv run repo-chores.py report --format html > report.html   # visual report
    uv run repo-chores.py readme-check
    uv run repo-chores.py update-descriptions            # dry-run
    uv run repo-chores.py update-descriptions --apply     # actually writes
    uv run repo-chores.py report --only some-app          # filter to one app
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import subprocess
import sys
import urllib.parse
from datetime import datetime
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


# --- HTML output (customise the look here) ------------------------------------
# `--format html` renders a single, self-contained page: the <style> block below
# is inlined into it, so there are NO external assets or CDN calls. It opens
# straight from disk (`... --format html > report.html && open report.html`) and
# works offline — handy when the VPN is up for the GitLab host but little else.
#
# Re-theme it in one of two ways:
#   * edit DEFAULT_HTML_STYLE right here, or
#   * leave the script pristine and pass your own stylesheet: `--css mytheme.css`.
#
# The default palette is the University of Glasgow house style
# (https://design.gla.ac.uk): University blue #011451, dark blue #005398,
# error #D4351C, success #8BC34A, highlight #FFDD00. Noto Sans is the UofG
# typeface — we list it first and fall back to the system sans stack rather than
# fetching it from a font CDN (which would break the "self-contained" promise).

DEFAULT_HTML_STYLE = """
:root {
  --ug-blue: #011451; --ug-dark-blue: #005398;
  --error: #D4351C; --success: #8BC34A; --highlight: #FFDD00;
  --ink: #323232; --grey-1: #f5f5f5; --grey-2: #e6e6e6;
  --warn-bg: #fff8d6; --warn-ink: #6b5500;
  --bad-bg: #fbe4e0; --bad-ink: #8f1c0c;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--grey-1); color: var(--ink); line-height: 1.5;
  font-family: "Noto Sans", system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
main { max-width: 1100px; margin: 0 auto; padding: 24px; }
h1 { color: var(--ug-blue); font-size: 1.6rem; margin: 0 0 4px; }
.lead { color: #555; margin: 0 0 24px; }
a { color: var(--ug-dark-blue); }

.summary { display: flex; flex-wrap: wrap; gap: 12px; margin: 0 0 16px; }
.stat { background: #fff; border: 1px solid var(--grey-2); border-radius: 6px; padding: 8px 14px; font-size: .9rem; }
.stat b { color: var(--ug-blue); font-size: 1.1rem; }
.stat.warn { border-color: var(--highlight); background: var(--warn-bg); }
.stat.warn b { color: var(--warn-ink); }
.stat.bad { border-color: var(--error); background: var(--bad-bg); }
.stat.bad b { color: var(--bad-ink); }
.stat.ok b { color: #3c6e00; }

.problem-list { margin: 0 0 24px; font-size: .9rem; }
.problem-list a { margin-right: 12px; white-space: nowrap; }

.app { background: #fff; border: 1px solid var(--grey-2); border-radius: 8px; margin: 16px 0; overflow: hidden; }
.app.flagged { border-left: 6px solid var(--highlight); }
.app.drift { border-left: 6px solid var(--error); }
.app > header { background: var(--ug-blue); color: #fff; padding: 12px 16px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.app > header h2 { font-size: 1.05rem; margin: 0; font-weight: 600; }

.badge { font-size: .72rem; font-weight: 700; text-transform: uppercase; letter-spacing: .03em; padding: 2px 8px; border-radius: 999px; }
.badge.warn { background: var(--highlight); color: #3a2f00; }
.badge.bad { background: var(--error); color: #fff; }
.badge.ok { background: var(--success); color: #1f3300; }

table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 8px 16px; vertical-align: top; border-top: 1px solid var(--grey-2); }
thead th { border-top: none; font-size: .72rem; text-transform: uppercase; letter-spacing: .03em; color: #666; background: var(--grey-1); }
tbody tr:hover { background: #fafbff; }
.platform { font-weight: 600; color: var(--ug-dark-blue); white-space: nowrap; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .85rem; }
.muted { color: #999; }
.desc { white-space: pre-wrap; word-break: break-word; max-width: 40ch; font-size: .85rem; }

td.flag-warn { background: var(--warn-bg); color: var(--warn-ink); font-weight: 600; }
td.flag-bad { background: var(--bad-bg); color: var(--bad-ink); font-weight: 600; }
tr.unreachable td { background: var(--bad-bg); color: var(--bad-ink); }

footer.meta { margin-top: 32px; color: #999; font-size: .8rem; border-top: 1px solid var(--grey-2); padding-top: 12px; }
"""

# The page shell. Only the named {placeholders} are substituted; str.format does
# not re-scan the substituted values, so the CSS braces in {style} and any stray
# braces in descriptions are safe to insert as-is.
HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
{style}
</style>
</head>
<body>
<main>
<h1>{heading}</h1>
<p class="lead">{lead}</p>
{summary}
{body}
<footer class="meta">{footer}</footer>
</main>
</body>
</html>
"""


def load_html_style(args: argparse.Namespace) -> str:
    """CSS for --format html: a user file via --css, else the built-in UofG theme."""
    css_path = getattr(args, "css", None)
    if css_path:
        path = Path(css_path).expanduser()
        if not path.is_file():
            sys.exit(f"--css stylesheet not found: {path}")
        return path.read_text(encoding="utf-8")
    return DEFAULT_HTML_STYLE


def _slug(name: str) -> str:
    """A safe #anchor id from an app directory name."""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-") or "app"


def _remote_label(platform: str) -> str:
    return {"gitlab": "GitLab", "github": "GitHub"}.get(platform, html.escape(platform or "?"))


def _stat(value: object, label: str, *, tone: str = "") -> str:
    """A summary chip. `value` must be an int or already-escaped string."""
    cls = f"stat {tone}".strip()
    return f"<span class='{cls}'><b>{value}</b> {html.escape(label)}</span>"


def render_html_page(*, title: str, heading: str, lead: str,
                     summary: str, body: str, style: str) -> str:
    """Wrap pre-built summary/body HTML in the page shell.

    title/heading/lead are escaped here; summary/body are trusted HTML that the
    callers below assemble with html.escape() on every dynamic value, and style
    is the CSS (the built-in default or a user's --css file).
    """
    footer = f"repo-chores · generated {datetime.now():%Y-%m-%d %H:%M}"
    return HTML_PAGE.format(
        title=html.escape(title), heading=html.escape(heading),
        lead=html.escape(lead), summary=summary, body=body,
        style=style, footer=html.escape(footer),
    )


def render_remote_row(r: dict, drift: bool) -> str:
    """One <tr> for a single remote within an app's table."""
    label = _remote_label(r.get("platform", ""))
    path = html.escape(r.get("path", ""))
    if r.get("reachable") == "N":
        err = html.escape(r.get("error") or "unreachable")
        return (f"<tr class='unreachable'><td class='platform'>{label}</td>"
                f"<td class='mono'>{path}</td>"
                f"<td colspan='5'>⚠ unreachable — {err}</td></tr>")

    platform = r.get("platform", "")
    laravel = r.get("laravel", "")
    laravel_cell_cls = " class='flag-bad'" if drift and laravel else ""
    laravel_html = html.escape(laravel) if laravel else "<span class='muted'>unknown</span>"

    prefix_ok = r.get("prefix_ok", "?")
    if prefix_ok == "Y":
        prefix_cell = "<td><span class='badge ok'>ok</span></td>"
    elif prefix_ok == "N":
        expected = expected_prefix(laravel, platform) or ""
        prefix_cell = (f"<td class='flag-warn'>mismatch"
                       f"<br><span class='muted'>expected {html.escape(expected)}</span></td>")
    else:
        prefix_cell = "<td><span class='muted'>n/a</span></td>"

    readme_cell = ("<td>custom</td>" if r.get("custom_readme") == "Y"
                   else "<td class='flag-warn'>boilerplate / none</td>")

    branch = r.get("default_branch", "")
    branch_html = html.escape(branch) if branch else "<span class='muted'>?</span>"
    desc = r.get("description", "")
    desc_html = (f"<div class='desc'>{html.escape(desc)}</div>" if desc
                 else "<span class='muted'>(none)</span>")

    return (
        f"<tr><td class='platform'>{label}</td>"
        f"<td class='mono'>{path}</td>"
        f"<td{laravel_cell_cls}>{laravel_html}</td>"
        f"<td class='mono'>{branch_html}</td>"
        f"{prefix_cell}{readme_cell}"
        f"<td>{desc_html}</td></tr>"
    )


def render_app_card(name: str, app_rows: list[dict]) -> tuple[str, bool, bool]:
    """Return (card_html, has_problem, drift) for one app and its remotes."""
    versions = {r.get("laravel") for r in app_rows
                if r.get("reachable") == "Y" and r.get("laravel")}
    drift = len(versions) > 1
    has_problem = drift or any(
        r.get("prefix_ok") == "N" or r.get("custom_readme") == "N"
        or r.get("reachable") == "N" for r in app_rows
    )
    cls = "app drift" if drift else "app flagged" if has_problem else "app"

    badges = ""
    if drift:
        joined = html.escape(" vs ".join(sorted(v for v in versions if v)))
        badges = f"<span class='badge bad'>version drift: {joined}</span>"
    header = f"<header><h2>{html.escape(name)}</h2>{badges}</header>"

    # Defensive: an app with no recognised remotes (shouldn't normally happen,
    # since discovery requires a GitLab remote in the first place).
    if all("platform" not in r for r in app_rows):
        msg = html.escape(app_rows[0].get("error") or "no GitLab/GitHub remotes recognised")
        card = (f"<section class='{cls}' id='app-{_slug(name)}'>{header}"
                f"<p class='muted' style='padding:12px 16px'>{msg}</p></section>")
        return card, has_problem, drift

    body_rows = "".join(render_remote_row(r, drift) for r in app_rows)
    table = (
        "<table><thead><tr>"
        "<th>Platform</th><th>Path</th><th>Laravel</th><th>Branch</th>"
        "<th>Prefix</th><th>README</th><th>Description</th>"
        "</tr></thead><tbody>" + body_rows + "</tbody></table>"
    )
    return (f"<section class='{cls}' id='app-{_slug(name)}'>{header}{table}</section>",
            has_problem, drift)


def emit_report_html(rows: list[dict], root: object, style: str) -> None:
    """Render the `report` rows as a grouped, self-contained HTML page."""
    order: list[str] = []
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        d = r.get("directory", "")
        if d not in grouped:
            grouped[d] = []
            order.append(d)
        grouped[d].append(r)

    cards: list[str] = []
    flagged: list[str] = []
    for d in order:
        card, has_problem, _ = render_app_card(d, grouped[d])
        cards.append(card)
        if has_problem:
            flagged.append(d)

    n_prefix = sum(1 for r in rows if r.get("prefix_ok") == "N")
    n_readme = sum(1 for r in rows if r.get("custom_readme") == "N")
    n_unreach = sum(1 for r in rows if r.get("reachable") == "N")

    stats = "".join([
        _stat(len(order), "apps"),
        _stat(n_prefix, "prefix mismatches", tone="warn" if n_prefix else "ok"),
        _stat(n_readme, "boilerplate / missing READMEs", tone="warn" if n_readme else "ok"),
        _stat(n_unreach, "unreachable remotes", tone="bad" if n_unreach else "ok"),
    ])
    if flagged:
        links = " ".join(f"<a href='#app-{_slug(a)}'>{html.escape(a)}</a>" for a in flagged)
        problem_line = f"<p class='problem-list'><strong>Needs attention:</strong> {links}</p>"
    else:
        problem_line = "<p class='problem-list'>✓ Everything looks tidy.</p>"

    summary = f"<div class='summary'>{stats}</div>{problem_line}"
    print(render_html_page(
        title="repo-chores — drift report",
        heading="repo-chores — drift report",
        lead=f"{len(order)} production Laravel app(s) under {root}",
        summary=summary, body="".join(cards), style=style,
    ))


def emit_readmecheck_html(flagged: list[dict], style: str) -> None:
    if flagged:
        rows = "".join(
            f"<tr><td class='platform'>{_remote_label(r['platform'])}</td>"
            f"<td class='mono'>{html.escape(r['path'])}</td>"
            f"<td class='mono muted'>{html.escape(r['directory'])}</td></tr>"
            for r in flagged
        )
        body = (
            "<section class='app flagged'>"
            "<header><h2>Boilerplate / missing READMEs</h2>"
            f"<span class='badge warn'>{len(flagged)}</span></header>"
            "<table><thead><tr><th>Platform</th><th>Path</th><th>Directory</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></section>"
        )
        summary = f"<div class='summary'>{_stat(len(flagged), 'repos need a README', tone='warn')}</div>"
    else:
        body = "<section class='app'><p style='padding:16px'>All reachable repos have a custom README. 🎉</p></section>"
        summary = f"<div class='summary'>{_stat(0, 'repos need a README', tone='ok')}</div>"
    print(render_html_page(
        title="repo-chores — README check",
        heading="repo-chores — README check",
        lead="Repos still using the default / boilerplate README (or none at all).",
        summary=summary, body=body, style=style,
    ))


def emit_update_html(changes: list[dict], apply: bool, style: str) -> None:
    mode = "Applied changes" if apply else "Dry-run — no writes"
    if not changes:
        body = "<section class='app'><p style='padding:16px'>Every reachable repo description already carries the right prefix. 🎉</p></section>"
        summary = f"<div class='summary'>{_stat(0, 'changes', tone='ok')}</div>"
    else:
        out_rows = []
        for r in changes:
            result = r.get("result", "")
            res_cls = " class='flag-bad'" if apply and not result.startswith("updated") else ""
            out_rows.append(
                f"<tr><td class='platform'>{_remote_label(r['platform'])}</td>"
                f"<td class='mono'>{html.escape(r['path'])}</td>"
                f"<td class='desc'>{html.escape(r.get('from') or '(none)')}</td>"
                f"<td class='desc'>{html.escape(r.get('to', ''))}</td>"
                f"<td{res_cls}>{html.escape(result)}</td></tr>"
            )
        body = (
            "<section class='app'>"
            f"<header><h2>Description changes</h2><span class='badge warn'>{html.escape(mode)}</span></header>"
            "<table><thead><tr><th>Platform</th><th>Path</th><th>From</th><th>To</th><th>Result</th></tr></thead>"
            f"<tbody>{''.join(out_rows)}</tbody></table></section>"
        )
        summary = (f"<div class='summary'>{_stat(len(changes), 'changes')}"
                   f"<span class='stat'><b>{html.escape(mode)}</b></span></div>")
    print(render_html_page(
        title="repo-chores — descriptions",
        heading="repo-chores — description updates",
        lead=f"{mode}.",
        summary=summary, body=body, style=style,
    ))


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
        elif args.format == "json":
            emit_json(rows)
        else:  # html
            emit_report_html(rows, args.path, load_html_style(args))
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
    if args.format == "html":
        emit_readmecheck_html(flagged, load_html_style(args))
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
    if args.format == "html":
        emit_update_html(changes, args.apply, load_html_style(args))
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
    common.add_argument("--format", choices=["text", "csv", "json", "html"], default="text",
                        help="Output format (default: text). 'html' is a self-contained page.")
    common.add_argument("--css", type=Path, metavar="FILE",
                        help="Custom CSS file for --format html (overrides the built-in UofG theme).")

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
