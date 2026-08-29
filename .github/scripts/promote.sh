#!/usr/bin/env bash
# Build a branch carrying CODE ONLY from $SRC into $TGT.
#
# Used for BOTH directions of the flow:
#   forward  (promote.yml)   dev -> qa -> main, plus the dev -> main override
#   backward (backmerge.yml) main -> qa and main -> dev, so a commit made
#                            directly on main does not leave the lower branches
#                            permanently behind.
# One script for both on purpose: the VERSION pinning and conflict handling
# below are the fiddly part, and a second copy of them would drift.
#
# VERSION is branch-owned: every tracked VERSION file is pinned to $TGT's value
# and then advanced one step, so a version set on the source branch NEVER leaks
# across (dev at 10.00 does not make qa 10.00 -- qa goes 1.45 -> 1.46).
# Pinning also resolves the VERSION merge conflict that would otherwise stall
# every promotion PR. Any other conflict is left for a human.
#
# Env: SRC, TGT, BR, and optional LABEL (wording only -- "promote"/"backmerge").
# Writes changed=true/false to $GITHUB_OUTPUT if set.
set -euo pipefail

: "${SRC:?}" "${TGT:?}" "${BR:?}"
LABEL="${LABEL:-promote}"
out="${GITHUB_OUTPUT:-/dev/null}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

version_files() { git ls-tree -r --name-only "origin/$TGT" | grep -E '(^|/)VERSION$' || true; }

git rev-parse --verify "origin/$TGT" >/dev/null 2>&1 || { echo "::error::target branch $TGT does not exist"; exit 1; }

git checkout -q -B "$BR" "origin/$TGT"

# --no-ff: the promotion is always an explicit, revertable commit.
git merge --no-commit --no-ff "origin/$SRC" || true

# Pin VERSION to the target's lineage. Listing from the TARGET tree means a
# VERSION file added on the source is simply never carried over.
while IFS= read -r f; do
  [ -n "$f" ] || continue
  git checkout "origin/$TGT" -- "$f"
done < <(version_files)

# Drop any VERSION that exists only on the source, so the target keeps sole
# ownership of versioning.
while IFS= read -r f; do
  [ -n "$f" ] || continue
  git rm -q --cached "$f" 2>/dev/null || true
  rm -f "$f"
done < <(git diff --cached --name-only --diff-filter=A | grep -E '(^|/)VERSION$' || true)

if git ls-files -u | grep -q .; then
  echo "::error::merge conflict outside VERSION -- resolve $SRC -> $TGT by hand:"
  git ls-files -u | awk '{print "  " $4}' | sort -u
  exit 1
fi

if git diff --cached --quiet && git diff --quiet; then
  echo "Nothing to carry: $TGT already contains $SRC (ignoring VERSION)."
  echo "changed=false" >> "$out"
  exit 0
fi

# Advance the TARGET's own version inside the promotion commit. Doing it here
# rather than as a later push to qa/main matters: those branches require a PR,
# so a bot pushing straight at them would be rejected by the ruleset.
while IFS= read -r f; do
  [ -n "$f" ] || continue
  before="$(cat "$f")"
  python3 "$here/bump_version.py" "$f" >/dev/null
  echo "  $f: $before -> $(cat "$f")"
  git add "$f"
done < <(version_files)

git commit -q -m "$LABEL: $SRC -> $TGT" \
  -m "Code-only $LABEL. VERSION stays on ${TGT}'s own sequence, advanced one step here."
echo "changed=true" >> "$out"
# The approve step must wait for the run belonging to THIS commit; approving
# whichever parked run happens to exist first races with the run GitHub is
# still creating for the new head.
echo "sha=$(git rev-parse HEAD)" >> "$out"
