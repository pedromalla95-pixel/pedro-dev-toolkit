#!/usr/bin/env bash
# Refresh mirrored skills from the live copies in ~/.claude/skills.
# research/ and AUDIT-* stay out: they hold real project plans and this repo has a GitHub remote.
set -euo pipefail
cd "$(dirname "$0")"
rsync -a --exclude='*.bak' --exclude='__pycache__' --exclude='invocations.log' --exclude='node_modules' \
  --exclude='research/' --exclude='AUDIT-*' \
  ~/.claude/skills/{context-builder,reverse-prompt,verify-plan} skills/
