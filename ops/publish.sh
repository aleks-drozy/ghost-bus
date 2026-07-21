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

# The unit runs as ubuntu (User=ubuntu - see the unit file), not root, but a
# credential.helper configured in /etc/gitconfig (system scope) or even
# ubuntu's own ~/.gitconfig would still be consulted by git's post-auth
# credential_approve() step even though GIT_ASKPASS supplied the password -
# GIT_ASKPASS only answers the prompt, it does not stop git handing a
# successful credential to every configured helper's `store` action
# afterward. GIT_CONFIG_NOSYSTEM removes /etc/gitconfig from consideration
# entirely; `-c credential.helper=` (an empty value resets the helper list)
# is applied directly to the two network-touching git invocations below as
# well, so nothing configured anywhere can persist the token to disk
# regardless of scope.
export GIT_CONFIG_NOSYSTEM=1

REPO_DIR="${GHOSTBUS_REPO_DIR:-/opt/ghost-bus}"
# Nested under REPO_DIR (matching .venv/, already gitignored below it) rather
# than a sibling path: this keeps the default derived from REPO_DIR, which is
# what makes it possible to test the real default path resolution at all.
# ops/../.gitignore carries `data-repo/` for the same reason .venv/ is
# ignored - it is not part of the code checkout's own tracked tree.
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

# 1. Tripwire for accidental edits to the deployed code tree, not a boundary
#    against an attacker: it cannot see .gitignore'd paths (.venv/ - the very
#    interpreter this script executes - is itself ignored), and a determined
#    attacker with write access here could edit tracked files, commit
#    locally, and pass this check trivially. It exists to catch the ordinary
#    case - someone hand-edited a file in place on the VM - not to prove the
#    checkout is trustworthy.
#    Captured on its own line rather than inline in the `[ -n "$(...)" ]`
#    test: `set -e` is suppressed for commands inside a condition, and the
#    command-substitution's own exit status is discarded there regardless -
#    so a FAILING `git status` (git's "dubious ownership" check should no
#    longer trigger now that the unit runs as ubuntu matching this checkout's
#    owner - see the unit file - but a stale index.lock or git missing from
#    ubuntu's PATH still could) previously yielded empty output and let the
#    guard pass open. As a plain assignment on its own line, `set -e` sees
#    and acts on that failure.
DIRTY_STATUS="$(git status --porcelain)"
if [ -n "${DIRTY_STATUS}" ]; then
  echo "publish: code checkout is dirty - refusing to publish" >&2
  echo "${DIRTY_STATUS}" >&2
  exit 1
fi

cd "${DATA_REPO}"

# 2. Establish a known base before anything is written or staged. Without
#    this, `git push HEAD:main` at the end publishes HEAD and every ancestor
#    not already on the remote - including anything ever committed locally
#    in this checkout (a person debugging, or an attacker with write access
#    here) that nothing here previously examined. Resetting to the remote's
#    actual tip also recovers a diverged or stale checkout that would
#    otherwise wedge the push. If `main` does not exist yet (the very first
#    publish, before this repository has any history), there is nothing to
#    reset to or diverge from.
#    This hard reset is safe ONLY because publish.dataset rewrites the
#    dataset in full every run (data/daily, data/uptime, and manifest.json
#    are each regenerated from the database from scratch, not patched
#    incrementally) - if that ever changed to an incremental writer that
#    assumed the previous run's output was still on disk, this reset would
#    silently discard days between runs before the builder ever saw them.
HAVE_REMOTE_BASE=1
if ! git -c credential.helper= fetch "${DATA_REMOTE}" main; then
  HAVE_REMOTE_BASE=0
  echo "publish: no existing main on the data remote yet - treating this as the initial publish" >&2
fi
if [ "${HAVE_REMOTE_BASE}" = "1" ]; then
  git reset --hard FETCH_HEAD
fi

cd "${REPO_DIR}"

# 3. Build the dataset into the data repository's working tree. This enforces
#    the publish gate, the 14-day baseline gate, and complete-service-days-only.
#    A gate failure exits nonzero here and `set -e` stops the run before git is
#    touched at all: nothing is committed, nothing is pushed, and the
#    previously published data stays up.
#    `env -u` strips the publish token from this process's environment before
#    it runs: publish.dataset is deliberately kept git-free and AST-pinned
#    against spawning a process, but nothing stops it reading os.environ, and
#    it writes into the directory that gets published - a single `os.environ`
#    dump into the manifest would publish the token. Only the askpass helper
#    needs it, and only during the push below.
env -u GHOSTBUS_PUBLISH_TOKEN "${PY}" -m publish.dataset --db "${DB_PATH}" --data-dir "${DATA_REPO}/data"

cd "${DATA_REPO}"

# 4. Stage only the dataset paths that exist OR are still tracked.
#    "Dataset-only" is a promise this script keeps itself, not an assumption
#    about what else lives in this checkout - so we name every path
#    explicitly rather than staging the whole tree. data/daily does not
#    exist until the 14-day baseline gate is met (spec D6 keeps uptime +
#    manifest exempt so they publish from day one) - `git add` is fatal on
#    an unmatched pathspec, so a path absent from disk AND the index is
#    skipped. But a path that publish/dataset.py has just rmtree'd because
#    coverage fell back below baseline (dataset.py:393-397's WITHDRAWAL rule
#    - published route data must come down, not sit next to a page saying we
#    publish nothing about any route) is absent from disk while still
#    tracked in the index - `git ls-files` still finds it, and `git add` on a
#    missing-but-tracked path stages the deletion. Checking existence alone
#    would skip that path, the deletion would never be staged, and the
#    withdrawn CSVs would stay live on the public repo forever (each run's
#    fetch+reset would restore them from the remote, dataset.py would delete
#    them again, and the guard would skip them again).
STAGE_PATHS=()
for p in data/daily data/uptime data/manifest.json; do
  if [ -e "${p}" ] || [ -n "$(git ls-files -- "${p}")" ]; then
    STAGE_PATHS+=("${p}")
  fi
done
if [ "${#STAGE_PATHS[@]}" -eq 0 ]; then
  echo "publish: no dataset paths present yet, nothing to stage" >&2
  exit 0
fi
git add -- "${STAGE_PATHS[@]}"

if git diff --cached --quiet; then
  echo "publish: dataset unchanged, nothing to push"
  exit 0
fi

# 4b. Belt and braces: the paths above are the entire contract for this
#     repository. `git add` on a directory stages everything under it, so a
#     stray file dropped inside data/daily or data/uptime - a bug in
#     publish.dataset, or an intrusion - would otherwise ride along silently.
#     Refuse and unstage rather than publish anything outside the contract.
#     STAGED is captured on its own line for the same `set -e` reason as the
#     dirty-checkout guard above: `|| true` is needed so grep's exit-1-on-
#     no-match doesn't abort a normal, fully-matching run, but inline on the
#     same line it would also swallow a failure of the `git diff` itself.
STAGED="$(git diff --cached --name-only)"
UNEXPECTED="$(printf '%s\n' "${STAGED}" | grep -Ev '^data/(daily|uptime)/[^/]+\.csv$|^data/manifest\.json$' || true)"
if [ -n "${UNEXPECTED}" ]; then
  echo "publish: staged files outside the dataset contract - refusing to publish" >&2
  printf '%s\n' "${UNEXPECTED}" >&2
  git reset -- . >/dev/null
  exit 1
fi

git -c user.name='ghost-bus publisher' \
    -c user.email='publisher@ghost-bus.invalid' \
    commit -m "data: publish $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# 5. However this checkout got here, exactly one new commit - the one just
#    made above - may ever reach the remote. Without this, a bug anywhere
#    above that left extra history in place (or a future change to this
#    script) would still push whatever HEAD happens to be.
if [ "${HAVE_REMOTE_BASE}" = "1" ]; then
  NEW_COMMITS="$(git rev-list --count FETCH_HEAD..HEAD)"
else
  NEW_COMMITS="$(git rev-list --count HEAD)"
fi
if [ "${NEW_COMMITS}" != "1" ]; then
  echo "publish: expected exactly 1 new commit ahead of the remote, got ${NEW_COMMITS} - refusing to publish" >&2
  exit 1
fi

git -c credential.helper= push "${DATA_REMOTE}" HEAD:main

echo "publish: pushed $(git rev-parse --short HEAD)"
