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

echo "== split promotion: one original PR per promotion PR =="
git push -q origin promote/dev-to-qa:qa
git fetch -q origin

# Three units land on dev: a merged PR, then a VERSION-only bump, then a direct
# push. The VERSION bump sits DELIBERATELY IN THE MIDDLE -- once VERSION is
# pinned back to qa it is a content no-op, so if the selector could not skip it
# the queue would deadlock on it and the direct push behind it would never be
# promoted. A no-op unit at the END of the queue would not prove this: there,
# "skipped it" and "nothing left to do" look identical.
git checkout -q -B dev origin/dev
git checkout -q -b unitA
echo "alpha" > alpha.py; git add alpha.py; git commit -qm "add alpha"
git checkout -q dev
git merge -q --no-ff unitA -m "Merge pull request #7 from o/unitA" -m "feat: add the alpha module"
echo "10.01" > VERSION; git commit -qam "chore: bump dev version"
echo "beta" > beta.py; git add beta.py; git commit -qm "direct: add beta"
git push -q origin dev; git fetch -q origin

PROMOTE_SPLIT=1 SRC=dev TGT=qa BR=promote/dev-to-qa bash .github/scripts/promote.sh >/dev/null
chk "unit 1 carries its own change"  "$(cat alpha.py 2>/dev/null)" "alpha"
chk "unit 1 withholds later work"    "$([ -f beta.py ] && echo present || echo absent)" "absent"
chk "unit 1 names the original PR"   "$(git log -1 --pretty=%s)" "promote: dev -> qa (#7)"
chk "unit 1 advances qa once"        "$(cat VERSION)" "1.48"
git push -q origin promote/dev-to-qa:qa; git fetch -q origin

# The next unit in line is the VERSION-only commit, which is a no-op. It must be
# skipped so the direct push BEHIND it still gets promoted.
out=$(PROMOTE_SPLIT=1 SRC=dev TGT=qa BR=promote/dev-to-qa bash .github/scripts/promote.sh 2>&1)
case "$out" in *"no content change"*) echo "  PASS no-op unit reported as skipped"; pass=$((pass+1));;
  *) echo "  FAIL expected a skip, got: $out"; fail=$((fail+1));; esac
chk "queue advanced past the no-op" "$(cat beta.py 2>/dev/null)" "beta"
chk "and still has unit 1"          "$(cat alpha.py 2>/dev/null)" "alpha"
chk "qa advanced once, not twice"   "$(cat VERSION)" "1.49"
git push -q origin promote/dev-to-qa:qa; git fetch -q origin

out=$(PROMOTE_SPLIT=1 SRC=dev TGT=qa BR=promote/dev-to-qa bash .github/scripts/promote.sh 2>&1 || true)
case "$out" in *"Nothing to promote"*) echo "  PASS queue drains when every unit is promoted"; pass=$((pass+1));;
  *) echo "  FAIL expected a drained queue, got: $out"; fail=$((fail+1));; esac

echo "== batched mode is unchanged by the split option =="
git checkout -q -B dev origin/dev
echo "gamma" > gamma.py; git add gamma.py; git commit -qm "add gamma"
echo "delta" > delta.py; git add delta.py; git commit -qm "add delta"
git push -q origin dev; git fetch -q origin
SRC=dev TGT=qa BR=promote/dev-to-qa bash .github/scripts/promote.sh >/dev/null
chk "batched takes every unit at once (gamma)" "$(cat gamma.py 2>/dev/null)" "gamma"
chk "batched takes every unit at once (delta)" "$(cat delta.py 2>/dev/null)" "delta"

echo "== the running script is immutable mid-promotion =="
# stage_to checks out the TARGET branch into the working tree, which replaces
# .github/scripts/* underneath the running script. If the run then picked its
# tooling back up from the working tree it would be executing the TARGET's code
# halfway through -- which is how a split promotion silently continued as the
# target's older batched script and swept up every remaining unit at once.
# Sabotaging the target's copy makes that hijack deterministic to detect.
git checkout -q -B qa origin/qa
cat > .github/scripts/bump_version.py <<'SAB'
import sys
open(sys.argv[1], "w").write("SABOTAGED\n")
SAB
git commit -qam "qa: tooling that must never run"
git push -q origin qa
git checkout -q -B dev origin/dev
echo "epsilon" > epsilon.py; git add epsilon.py; git commit -qm "add epsilon"
git push -q origin dev; git fetch -q origin
PROMOTE_SPLIT=1 SRC=dev TGT=qa BR=promote/dev-to-qa bash .github/scripts/promote.sh >/dev/null
case "$(cat VERSION)" in
  SABOTAGED) chk "target's tooling cannot hijack the run" "hijacked" "clean" ;;
  *)         chk "target's tooling cannot hijack the run" "clean"    "clean" ;;
esac
chk "and it still promoted the oldest unit" "$(cat gamma.py 2>/dev/null)" "gamma"

echo; echo "RESULT: $pass passed, $fail failed"; rm -rf "$T"; [ "$fail" -eq 0 ]
