#!/usr/bin/env bash
# Ghost Bus publisher: build the dataset, then push it to the DATA repository.
#
# Run by ghostbus-publisher.service, which loads /etc/ghostbus.env. The
# credential is never named in this file: git obtains it via ops/git-askpass.sh,
# so it appears neither in argv, nor in `ps` output, nor in the journal.
#
# The dataset lives in its own repository on purpose. A Contents:write token on
# the code repository could rewrite publish/site.py or a template, and CI checks
# that repository out and executes it - arbitrary HTML on the site and arbitrary
# code in CI. Scoped to a repository holding only CSVs and a manifest, the same
# permission cannot reach a line of executable code.
set -euo pipefail
set +x

# A trace variable left in /etc/ghostbus.env would put the credential exchange
# into the journal. Clear them regardless of what the env file carries.
unset GIT_TRACE GIT_TRACE_CURL GIT_TRACE_PACKET GIT_CURL_VERBOSE

REPO_DIR="${GHOSTBUS_REPO_DIR:-/opt/ghost-bus}"
DATA_REPO="${GHOSTBUS_DATA_REPO_DIR:-${REPO_DIR}/data-repo}"
# GHOSTBUS_DATA_REMOTE exists only so tests can point this at a local bare
# repo instead of GitHub; production never sets it, so the real remote below
# is what actually runs.
DATA_REMOTE="${GHOSTBUS_DATA_REMOTE:-https://x-access-token@github.com/aleks-drozy/ghost-bus-data.git}"
DB_PATH="${GHOSTBUS_DB:-${REPO_DIR}/state/ghostbus.db}"
PY="${REPO_DIR}/.venv/bin/python"

export GIT_TERMINAL_PROMPT=0
export GIT_ASKPASS="${REPO_DIR}/ops/git-askpass.sh"

cd "${REPO_DIR}"

# 1. Refuse to run if the code checkout is dirty. The VM is not a development
#    machine; anything modified in place means someone edited the deployed
#    tree, and we will not publish numbers produced by code nobody reviewed.
if [ -n "$(git status --porcelain)" ]; then
  echo "publish: code checkout is dirty - refusing to publish" >&2
  git status --short >&2
  exit 1
fi

# 2. Build the dataset into the data repository's working tree. This enforces
#    the publish gate, the 14-day baseline gate, and complete-service-days-only.
#    A gate failure exits nonzero here and `set -e` stops the run before git is
#    touched at all: nothing is committed, nothing is pushed, and the
#    previously published data stays up.
"${PY}" -m publish.dataset --db "${DB_PATH}" --data-dir "${DATA_REPO}/data"

cd "${DATA_REPO}"

# 3. Stage only the dataset paths. "Dataset-only" is a promise this script
#    keeps itself, not an assumption about what else lives in this checkout -
#    so we name every path explicitly rather than staging the whole tree.
git add -- data/daily data/uptime data/manifest.json

if git diff --cached --quiet; then
  echo "publish: dataset unchanged, nothing to push"
  exit 0
fi

# 3b. Belt and braces: the three paths above are the entire contract for
#     this repository. `git add` on a directory stages everything under it,
#     so a stray file dropped inside data/daily or data/uptime - a bug in
#     publish.dataset, or an intrusion - would otherwise ride along silently.
#     Refuse and unstage rather than publish anything outside the contract.
UNEXPECTED="$(git diff --cached --name-only | grep -Ev '^data/(daily|uptime)/[^/]+\.csv$|^data/manifest\.json$' || true)"
if [ -n "${UNEXPECTED}" ]; then
  echo "publish: staged files outside the dataset contract - refusing to publish" >&2
  printf '%s\n' "${UNEXPECTED}" >&2
  git reset -- . >/dev/null
  exit 1
fi

git -c user.name='ghost-bus publisher' \
    -c user.email='publisher@ghost-bus.invalid' \
    commit -m "data: publish $(date -u +%Y-%m-%dT%H:%M:%SZ)"

git push "${DATA_REMOTE}" HEAD:main

echo "publish: pushed $(git rev-parse --short HEAD)"
