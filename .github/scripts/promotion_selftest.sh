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

echo "== a unit extended over later units names all of them =="
# Unit 1 (PR #8) adds zeta; unit 2 (a direct push) fixes zeta. Unit 1 cannot be
# promoted alone (its zeta is already superseded), so both travel together, and
# the PR must still be named after the oldest unit while listing both.
git checkout -q -B dev origin/dev
git checkout -q -b unitZ
echo "zeta" > zeta.py; git add zeta.py; git commit -qm "add zeta"
git checkout -q dev
git merge -q --no-ff unitZ -m "Merge pull request #8 from o/unitZ" -m "feat: add the zeta module"
echo "zeta fixed" > zeta.py; git commit -qam "fix zeta"
git push -q origin dev; git fetch -q origin
GITHUB_OUTPUT="$T/ext.out" PROMOTE_SPLIT=1 SRC=dev TGT=qa BR=promote/dev-to-qa bash .github/scripts/promote.sh >/dev/null
chk "extended unit carries the later fix" "$(cat zeta.py)" "zeta fixed"
chk "named after the oldest unit's PR"    "$(git log -1 --pretty=%s)" "promote: dev -> qa (#8)"
chk "subject says more units ride along"  "$(sed -n '/^unit_subject<</{n;p;}' "$T/ext.out")" "feat: add the zeta module (+1 more)"
chk "every carried unit is listed"        "$(sed -n '/^units_carried<</,/^PROMOTE_EOF/p' "$T/ext.out" | sed '1d;$d' | tr '\n' '|')" "- #8 feat: add the zeta module|- fix zeta|"
git push -q origin promote/dev-to-qa:qa; git fetch -q origin

echo "== units batched forward after a conflict are named too =="
# qa gets a repair that dev's PR #9 predates, so unit 1 conflicts in isolation.
# A back-merge (unit 2) resolves it, and the promotion carries both units: the
# PR must be named after #9 and list both, not just the back-merge.
git checkout -q -B qa origin/qa
echo "eta repaired" > eta.py; git add eta.py; git commit -qm "repair eta on qa"; git push -q origin qa
git checkout -q -B dev origin/dev
git checkout -q -b unitE
echo "eta dev" > eta.py; git add eta.py; git commit -qm "add eta"
git checkout -q dev
git merge -q --no-ff unitE -m "Merge pull request #9 from o/unitE" -m "feat: add the eta module"
git fetch -q origin
git merge -q --no-ff origin/qa -m "backmerge qa into dev" >/dev/null 2>&1 || true
echo "eta dev" > eta.py; git checkout -q --ours VERSION; git add eta.py VERSION
git commit -q --no-edit
git push -q origin dev; git fetch -q origin
GITHUB_OUTPUT="$T/conf.out" PROMOTE_SPLIT=1 SRC=dev TGT=qa BR=promote/dev-to-qa bash .github/scripts/promote.sh >/dev/null 2>&1
chk "batched unit carries the resolution" "$(cat eta.py)" "eta dev"
chk "named after the conflicting PR"      "$(git log -1 --pretty=%s)" "promote: dev -> qa (#9)"
chk "subject counts the batched unit"     "$(sed -n '/^unit_subject<</{n;p;}' "$T/conf.out")" "feat: add the eta module (+1 more)"
chk "both batched units are listed"       "$(sed -n '/^units_carried<</,/^PROMOTE_EOF/p' "$T/conf.out" | sed '1d;$d' | tr '\n' '|')" "- #9 feat: add the eta module|- backmerge qa into dev|"
git push -q origin promote/dev-to-qa:qa; git fetch -q origin

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

echo "== fleet-owned file added on both sides does not stall the promotion =="
# sync-fleet-files copies the file onto dev and onto qa separately: add/add.
git checkout -q -B qa origin/qa; mkdir -p .github
echo "fleet text on qa" > .github/copilot-instructions.md; git add .github/copilot-instructions.md; git commit -qm "sync on qa"; git push -q origin qa
git checkout -q -B dev origin/dev
echo "fleet text on dev" > .github/copilot-instructions.md; git add .github/copilot-instructions.md; git commit -qm "sync on dev"
echo "zz" > zz.py; git add zz.py; git commit -qm "add zz"
git push -q origin dev; git fetch -q origin
SRC=dev TGT=qa BR=promote/dev-to-qa bash .github/scripts/promote.sh >/dev/null 2>&1 || true
chk "fleet file resolved to the source copy" "$(cat .github/copilot-instructions.md 2>/dev/null)" "fleet text on dev"
git push -q origin promote/dev-to-qa:qa; git fetch -q origin

echo "== a conflict in a real source file still stops for a human =="
git checkout -q -B qa origin/qa; echo "qa side" > real.py; git add real.py; git commit -qm "qa real"; git push -q origin qa
git checkout -q -B dev origin/dev; echo "dev side" > real.py; git add real.py; git commit -qm "dev real"; git push -q origin dev; git fetch -q origin
out=$(SRC=dev TGT=qa BR=promote/dev-to-qa bash .github/scripts/promote.sh 2>&1 || true)
case "$out" in *"merge conflict outside VERSION"*) echo "  PASS real conflict still reported"; pass=$((pass+1));;
  *) echo "  FAIL expected a conflict report, got: $out"; fail=$((fail+1));; esac

echo "== a fleet-file conflict in the back-merge direction, or in a repo-owned agent, still stops =="
git merge --abort >/dev/null 2>&1 || true; git reset -q --hard
git checkout -q -B qa origin/qa; mkdir -p .github/agents; echo "qa agent" > .github/agents/$(basename "$PWD")-x.agent.md; echo "qa fleet" > .github/ai-budget.json; echo "qa" > .github/ai-budget.json.bak
git add .github; git commit -qm "qa owned"; git push -q origin qa
git checkout -q -B dev origin/dev; mkdir -p .github/agents; echo "dev agent" > .github/agents/$(basename "$PWD")-x.agent.md; echo "dev fleet" > .github/ai-budget.json; echo "dev" > .github/ai-budget.json.bak
git add .github; git commit -qm "dev owned"; git push -q origin dev; git fetch -q origin
out=$(GITHUB_REPOSITORY="o/$(basename "$PWD")" SRC=dev TGT=qa BR=promote/dev-to-qa bash .github/scripts/promote.sh 2>&1 || true)
case "$out" in *"-x.agent.md"*"ai-budget.json.bak"*) echo "  PASS repo-owned agent and look-alike file conflicts still reported"; pass=$((pass+1));;
  *) echo "  FAIL expected a conflict report, got: $out"; fail=$((fail+1));; esac
out=$(SRC=qa TGT=dev BR=backmerge/qa-to-dev bash .github/scripts/promote.sh 2>&1 || true)
case "$out" in *"ai-budget.json"*"ai-budget.json.bak"*) echo "  PASS back-merge fleet conflict still reported"; pass=$((pass+1));;
  *) echo "  FAIL expected a conflict report, got: $out"; fail=$((fail+1));; esac

echo "== a promotion script added on both sides resolves to the source copy, and lm syncs all three =="
git merge --abort >/dev/null 2>&1 || true; git reset -q --hard
F=.github/scripts/promotion_selftest.sh
git checkout -q -B qa origin/qa; echo "qa script" > $F; git add $F; git commit -qm "qa script"; git push -q origin qa
# Earlier sections left unrelated conflicts between dev and qa; start dev from qa.
git checkout -q -B dev origin/qa~1; echo "dev script" > $F; git add $F; git commit -qm "dev script"; git push -q -f origin dev; git fetch -q origin
# Run the real promoter, not the clone's file, which is the one in conflict.
if SRC=dev TGT=qa BR=promote/dev-to-qa bash "$TPL/promote.sh" >/dev/null 2>&1; then rc=ok; else rc=failed; fi
chk "the promotion ran to completion" "$rc" "ok"
chk "the script resolved to the source copy" "$(cat $F 2>/dev/null)" "dev script"
if [ -f "$TPL/../../.github/twins.json" ]; then
  for f in promote.sh bump_version.py promotion_selftest.sh; do
    grep -q "\"\.github/scripts/$f\"" "$TPL/../../.github/twins.json" && grep -q "'\.github/scripts/$f'" "$TPL/../../.github/workflows/sync-fleet-files.yml" \
      && { echo "  PASS $f is in the fleet sync"; pass=$((pass+1)); } || { echo "  FAIL $f missing from twins.json or the sync trigger"; fail=$((fail+1)); }
  done
fi

echo "== AGENTS.md: a conflict only in the fleet-rules block resolves; one outside it still stops =="
mk() { printf 'repo facts: %s\n<!-- fleet-rules:begin (synced from lm/.github/fleet-rules.md -- edit there) -->\n%s\n<!-- fleet-rules:end -->\n' "$1" "$2"; }
git merge --abort >/dev/null 2>&1 || true; git reset -q --hard
git checkout -q -B qa origin/qa; mk base v0 > AGENTS.md; git add AGENTS.md; git commit -qm "agents base"; git push -q -f origin qa
git checkout -q -B dev qa; mk base v-dev > AGENTS.md; git commit -qam "dev block"; git push -q -f origin dev
git checkout -q qa; mk base v-qa > AGENTS.md; git commit -qam "qa block"; git push -q origin qa; git fetch -q origin
if SRC=dev TGT=qa BR=promote/dev-to-qa bash "$TPL/promote.sh" >/dev/null 2>&1; then rc=ok; else rc=failed; fi
chk "block-only conflict: promotion ran" "$rc" "ok"
chk "block-only conflict: source block kept" "$(sed -n 3p AGENTS.md)" "v-dev"
git merge --abort >/dev/null 2>&1 || true; git reset -q --hard
git checkout -q -B qa origin/qa; mk base v0 > AGENTS.md; git commit -qam "agents base 2"; git push -q -f origin qa
git checkout -q -B dev qa; mk dev-facts v-dev > AGENTS.md; git commit -qam "dev facts"; git push -q -f origin dev
git checkout -q qa; mk qa-facts v-qa > AGENTS.md; git commit -qam "qa facts"; git push -q origin qa; git fetch -q origin
out=$(SRC=dev TGT=qa BR=promote/dev-to-qa bash "$TPL/promote.sh" 2>&1 || true)
case "$out" in *"merge conflict outside VERSION"*) echo "  PASS conflict outside the block still reported"; pass=$((pass+1));;
  *) echo "  FAIL expected a conflict report, got: $out"; fail=$((fail+1));; esac

echo; echo "RESULT: $pass passed, $fail failed"; rm -rf "$T"; [ "$fail" -eq 0 ]
