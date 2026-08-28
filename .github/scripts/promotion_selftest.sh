#!/usr/bin/env bash
# Proves VERSION is branch-owned: a version set on dev never reaches qa/main,
# and each promotion advances only the TARGET branch's own sequence.
set -euo pipefail
T=$(mktemp -d); TPL="$(cd "$(dirname "$0")" && pwd)"
pass=0; fail=0
chk() { if [ "$2" = "$3" ]; then echo "  PASS $1: $2"; pass=$((pass+1)); else echo "  FAIL $1: got '$2' want '$3'"; fail=$((fail+1)); fi; }

git init -q --bare "$T/remote.git"
git clone -q "$T/remote.git" "$T/w"; cd "$T/w"
git config user.email t@t; git config user.name t
mkdir -p .github/scripts && cp "$TPL/bump_version.py" "$TPL/promote.sh" .github/scripts/
echo "1.13" > VERSION; echo "orig" > app.py
git add -A; git commit -qm init; git branch -M main
git push -q origin main; git push -q origin main:qa; git push -q origin main:dev

# qa has advanced on its own to 1.45; main stays on its own line.
git checkout -q -B qa origin/qa; echo "1.45" > VERSION; git commit -qam "qa version"; git push -q origin qa
# Someone sets dev to 10.00 and ships a real code change.
git checkout -q -B dev origin/dev; echo "10.00" > VERSION; echo "feature" > app.py
git commit -qam "dev feature + version 10.00"; git push -q origin dev
git fetch -q origin

echo "== promote dev -> qa =="
SRC=dev TGT=qa BR=promote/dev-to-qa bash .github/scripts/promote.sh
chk "qa VERSION (dev's 10.00 must NOT leak)" "$(cat VERSION)" "1.46"
chk "qa received the code change"            "$(cat app.py)" "feature"
git push -q origin promote/dev-to-qa:qa

echo "== promote qa -> main =="
git fetch -q origin
SRC=qa TGT=main BR=promote/qa-to-main bash .github/scripts/promote.sh
chk "main VERSION (own sequence, not qa's 1.46)" "$(cat VERSION)" "1.14"
chk "main received the code change"              "$(cat app.py)" "feature"
git push -q origin promote/qa-to-main:main

echo "== dev keeps its own version =="
git fetch -q origin; git checkout -q -B dev origin/dev
chk "dev untouched by promotion" "$(cat VERSION)" "10.00"

echo "== re-promote with nothing new =="
git fetch -q origin
out=$(SRC=dev TGT=qa BR=promote/dev-to-qa bash .github/scripts/promote.sh 2>&1 || true)
case "$out" in *"Nothing to promote"*) echo "  PASS no-op when already promoted"; pass=$((pass+1));;
  *) echo "  FAIL expected no-op, got: $out"; fail=$((fail+1));; esac

echo "== second cycle advances qa again =="
git checkout -q -B dev origin/dev; echo "feature2" > app.py; git commit -qam f2; git push -q origin dev; git fetch -q origin
SRC=dev TGT=qa BR=promote/dev-to-qa bash .github/scripts/promote.sh >/dev/null
chk "qa 1.46 -> 1.47" "$(cat VERSION)" "1.47"

echo; echo "RESULT: $pass passed, $fail failed"; rm -rf "$T"; [ "$fail" -eq 0 ]
