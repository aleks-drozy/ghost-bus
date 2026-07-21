#!/usr/bin/env bash
# git calls this for the password prompt. It writes the credential to stdout
# for git and nowhere else: no logging, no tracing, no trailing newline.
set -euo pipefail
set +x

# Refuse to hand the token to any host but github.com. Git passes the prompt
# text it is asking about as $1 (e.g. "Password for
# 'https://x-access-token@github.com': ", with no path - git scopes
# credentials by host, not by path, so the quoted URL ends right after the
# host unless credential.useHttpPath is set, which nothing here sets). If
# GHOSTBUS_DATA_REMOTE is ever repointed at something else - misconfiguration,
# or an attacker who can only change env vars and not this file - the token
# must stay unusable there.
#
# A bare substring test (`*github.com*`) is not anchored and is defeated by
# any of: a lookalike hostname (github.com.evil.example), a path or query
# string that merely mentions the name (evil.example/?ref=github.com), or
# github.com placed as fake userinfo ahead of the real host
# (github.com@127.0.0.1). Anchoring on the host being the LAST thing before
# the closing quote - with or without a leading username@ - rules out all
# three: none of them has "github.com" immediately followed by the quote.
case "$1" in
  *"@github.com'"*|*"//github.com'"*) ;;
  *) exit 1 ;;
esac

printf %s "${GHOSTBUS_PUBLISH_TOKEN}"
