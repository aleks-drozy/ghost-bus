#!/usr/bin/env bash
# git calls this for the password prompt. It writes the credential to stdout
# for git and nowhere else: no logging, no tracing, no trailing newline.
set -euo pipefail
set +x
printf %s "${GHOSTBUS_PUBLISH_TOKEN}"
