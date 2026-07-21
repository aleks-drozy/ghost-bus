#!/usr/bin/env bash
# git calls this for the password prompt. It writes the credential to stdout
# for git and nowhere else: no logging, no tracing, no trailing newline.
set -euo pipefail
set +x

# Refuse to hand the token to any host but github.com. Git passes the prompt
# text it is asking about as $1 (e.g. "Password for
# 'https://x-access-token@github.com': "); if GHOSTBUS_DATA_REMOTE is ever
# repointed at something else - misconfiguration, or an attacker who can only
# change env vars and not this file - the token stays unusable there.
case "${1:-}" in
  *github.com*) ;;
  *) exit 1 ;;
esac

printf %s "${GHOSTBUS_PUBLISH_TOKEN}"
