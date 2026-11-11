#!/usr/bin/env bash
# Saves your OpenRouter API key to .env without showing it on screen or in shell history.
# Usage (from the repo folder): bash scripts/set-openrouter-key.sh
set -euo pipefail
cd "$(dirname "$0")/.."

git check-ignore -q .env || { echo "Refusing: .env is not gitignored here."; exit 1; }

printf "Paste your OpenRouter API key (input is hidden), then press Enter: "
read -rs key
echo
key="$(printf '%s' "$key" | tr -d '[:space:]')"
if [[ ! "$key" =~ ^sk-or- ]]; then
  echo "That doesn't look like an OpenRouter key (they start with sk-or-). Nothing saved."
  exit 1
fi
if (( ${#key} < 40 )) || [[ "$key" == *"..."* || "$key" == *"…"* ]]; then
  echo "That key is only ${#key} characters. It looks like the shortened preview from the key list,"
  echo "not the full key. Create a new key and use the copy button on the full key. Nothing saved."
  exit 1
fi

touch .env
chmod 600 .env
# Replace any existing OPENROUTER_API_KEY line, keep everything else.
grep -v '^OPENROUTER_API_KEY=' .env > .env.tmp || true
printf 'OPENROUTER_API_KEY=%s\n' "$key" >> .env.tmp
mv .env.tmp .env
chmod 600 .env
unset key
echo "Saved to .env (readable only by you). The key was not printed."
