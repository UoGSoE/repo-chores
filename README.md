# repo-chores

A housekeeping script for teams who run several Laravel apps across an on-prem
GitLab and, optionally, GitHub mirrors. It finds your production apps locally,
then reports drift in their repo descriptions and READMEs across both
platforms. It can fix that drift too.

It's a single file (`repo-chores.py`) with no required dependencies, so you can
drop it into a folder, read it end to end, and add your own chores later.

## What it does

For each production app it finds, it talks to the GitLab and GitHub APIs (via
the `gh` and `glab` CLIs) and reports each platform independently, so drift is
easy to see:

- the project path (group/org + name) on each platform
- the repo description, and whether it carries the expected version prefix
- the actual Laravel version, read from `composer.json` on the default branch
- whether the repo still has the stock Laravel README or a real one

It can also rewrite repo descriptions to a consistent version-tag convention
(see below), and list repos that never got a proper README.

### What counts as a "production app"

A local directory qualifies when all of these are true:

1. it's a git repository
2. it has a remote whose host is your on-prem GitLab (`REPO_CHORES_GITLAB_HOST`)
3. it contains an `artisan` file (so it's really a Laravel app)

The on-prem GitLab remote is what marks an app as shipped rather than a
throwaway experiment. Any `github.com` remotes on the same repo are treated as
mirrors and reported alongside it.

## Requirements

- Python 3.11+
- [`gh`](https://cli.github.com/) and [`glab`](https://gitlab.com/gitlab-org/cli),
  both authenticated with read access (and write access if you intend to use
  `update-descriptions --apply`)
- Optional: [`uv`](https://docs.astral.sh/uv/) to run it, and `python-dotenv`
  if you'd like `.env` parsing handled by the library rather than the built-in
  fallback

> **Note on self-managed GitLab:** `glab` defaults to `gitlab.com`. This tool
> always passes `--hostname $REPO_CHORES_GITLAB_HOST` so it talks to your
> instance, so make sure you're authenticated against that host
> (`glab auth status --hostname your.gitlab.host`).

## Configuration

Settings come from environment variables, which can be supplied directly or via
a `.env` file in the directory you run the tool from. Real environment
variables always win over `.env` values.

| Variable | Required | Description |
|---|---|---|
| `REPO_CHORES_GITLAB_HOST` | yes | Your on-prem GitLab hostname, e.g. `gitlab.example.com` (no `https://`) |
| `REPO_CHORES_CODE_DIR` | no | Folder to scan for apps (default: `~/Documents/code`; also settable with `--path`) |

Copy the example file and fill it in:

```bash
cp dotenv.example .env
# then edit .env
```

## Usage

Run with `uv` or plain Python:

```bash
uv run repo-chores.py report
# or
python3 repo-chores.py report
```

### Commands

| Command | What it does | Writes? |
|---|---|---|
| `report` | Per app, per platform: path, description + prefix check, Laravel version, README status | No |
| `readme-check` | Lists repos still using the default/boilerplate README (or none at all) | No |
| `update-descriptions` | Fixes the version-tag prefix on each description. Dry-run by default | Only with `--apply` |

### Common flags

- `--path PATH`: folder to scan (overrides `REPO_CHORES_CODE_DIR`)
- `--only SUBSTRING`: only process apps whose directory name contains this, which is handy for testing
- `--format {text,csv,json}`: output format (default `text`); `csv` and `json` go to stdout for piping into a spreadsheet or `jq`
- `--apply`: (`update-descriptions` only) write the changes; without it you get a dry-run

```bash
# Whole estate, as a spreadsheet
uv run repo-chores.py report --format csv > repos.csv

# See what description changes would be made, without making them
uv run repo-chores.py update-descriptions

# Apply them to a single app first, to check
uv run repo-chores.py update-descriptions --only my-app --apply
```

## The description convention

`update-descriptions` keeps a version tag at the start of each repo description
so the current Laravel major version shows up in the web UI:

- GitLab renders Markdown in project descriptions, so it uses a bold tag: `**[L12.x]**`
- GitHub descriptions are plain text, so it uses `[L12.x]`

The major version is taken from the `laravel/framework` constraint in
`composer.json`. When re-tagging, the tool strips older tag styles and tidies
them up while preserving the rest of the description. This convention is the
default the script encodes. If your team tags things differently, that logic
lives in one place and is easy to adjust.

## Safety

Everything is read-only except `update-descriptions --apply`. That command is a
dry-run unless you pass `--apply`, and even then it's worth scoping the first
run with `--only` so you can check one repo before doing the rest.

## Licence

MIT. See [LICENSE](LICENSE).
