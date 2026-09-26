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
#      PROMOTE_SPLIT=1 promotes ONE unit of work at a time (see below).
# Writes changed=true/false to $GITHUB_OUTPUT if set.
#
# SPLIT PROMOTION (PROMOTE_SPLIT=1)
# ---------------------------------
# By default this merges all of $SRC in one go, so a promotion PR arrives
# carrying every change that accumulated since the last one -- a 22-file diff
# spanning unrelated pieces of work. That is bad for the thing that actually
# gates promotion: review. A reviewer given one enormous diff cannot hold it
# all, and the skeptical-review panel demonstrably ran out of its file-fetch
# budget on exactly these PRs and rejected them for "could not verify X" rather
# than for anything wrong with the code.
#
# With PROMOTE_SPLIT=1 the promotion is cut back to the ORIGINAL units of work.
# The cut points are $SRC's FIRST-PARENT commits, which is precisely the list of
# things that landed on the branch: one entry per merged PR, one entry per
# direct push. The oldest un-promoted unit is chosen and the merge stops there.
#
# The unit is reached with an ordinary `git merge <ancestor-of-SRC>` rather than
# by cherry-picking it onto $TGT. That distinction is the whole reason this is
# safe: a cherry-pick of unit 3 without units 1 and 2 conflicts whenever they
# touched the same lines, while merging an ancestor of $SRC carries its history
# with it and conflicts no more than today's all-at-once merge does. Units are
# therefore promoted strictly in the order they landed.
#
# A unit that turns out to be a content no-op against $TGT is SKIPPED, not
# promoted. Without that this deadlocks: version-bump.yml commits VERSION-only
# changes to $SRC, VERSION is pinned back to $TGT's value below, so such a unit
# produces no diff at all -- and every subsequent run would keep selecting the
# same stuck unit, promote nothing, and never reach the real work behind it.
set -euo pipefail

: "${SRC:?}" "${TGT:?}" "${BR:?}"
LABEL="${LABEL:-promote}"
SPLIT="${PROMOTE_SPLIT:-0}"
out="${GITHUB_OUTPUT:-/dev/null}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Run from a private copy. `stage_to` below does `git checkout -B "$BR"
# "origin/$TGT"`, which rewrites the working tree -- INCLUDING THIS FILE. bash
# reads a script incrementally rather than slurping it, so once promote.sh
# differs between branches (exactly what happens while a change to it is being
# promoted) the interpreter can continue the run against the TARGET branch's
# version of itself. Observed: a split promotion correctly promoted unit 1,
# then silently continued as the target's older batched script and swept up
# every remaining unit in one go. Re-exec so the running code is immutable.
if [ -z "${PROMOTE_REEXEC:-}" ]; then
  PROMOTE_TMPDIR="$(mktemp -d)"
  cp "$here/promote.sh" "$PROMOTE_TMPDIR/"
  [ -f "$here/bump_version.py" ] && cp "$here/bump_version.py" "$PROMOTE_TMPDIR/"
  export PROMOTE_REEXEC=1 PROMOTE_TMPDIR
  exec bash "$PROMOTE_TMPDIR/promote.sh" "$@"
fi
trap '[ -n "${PROMOTE_TMPDIR:-}" ] && rm -rf "$PROMOTE_TMPDIR"' EXIT

version_files() { git ls-tree -r --name-only "origin/$TGT" | grep -E '(^|/)VERSION$' || true; }

git rev-parse --verify "origin/$TGT" >/dev/null 2>&1 || { echo "::error::target branch $TGT does not exist"; exit 1; }

# The endpoints to try, oldest first. Batched mode has exactly one: all of $SRC.
units=()
if [ "$SPLIT" = "1" ]; then
  while IFS= read -r c; do
    [ -n "$c" ] && units+=("$c")
  done < <(git rev-list --reverse --first-parent "origin/$TGT..origin/$SRC")
fi
[ "${#units[@]}" -gt 0 ] || units=("origin/$SRC")

# Build $BR as "$TGT plus everything up to <endpoint>", VERSION pinned.
# Returns 0 when that produced a real change, 1 when it is a content no-op.
stage_to() {
  local endpoint="$1"

  # A previous candidate may have left a merge in progress.
  git merge --abort >/dev/null 2>&1 || true
  git reset -q --hard

  git checkout -q -B "$BR" "origin/$TGT"

  # --no-ff: the promotion is always an explicit, revertable commit.
  git merge --no-commit --no-ff "$endpoint" || true

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
    return 1
  fi
  return 0
}

picked=""
picked_idx=0
for i in "${!units[@]}"; do
  if stage_to "${units[$i]}"; then
    picked="${units[$i]}"
    picked_idx="$i"
    break
  fi
  [ "$SPLIT" = "1" ] && echo "  skipping ${units[$i]} -- no content change against $TGT (VERSION-only?)"
done

if [ -z "$picked" ]; then
  # Phrase the no-op with $LABEL: "Nothing to promote" is the string every
  # repo's promotion_selftest.sh matches on, so the forward direction must keep
  # saying exactly that, while the reverse direction still reads correctly
  # ("Nothing to backmerge"). Changing this to a fixed phrase broke nw's CI.
  echo "Nothing to $LABEL: $TGT already contains $SRC (ignoring VERSION)."
  echo "changed=false" >> "$out"
  exit 0
fi

# COALESCE SUPERSEDED UNITS.
#
# Splitting by unit created a deadlock the first time it met a self-correcting
# change. Unit 1 shipped a defect; unit 2 fixed it. But unit 2 cannot be
# promoted until unit 1 merges, and unit 1 cannot merge because the review
# panel correctly rejects the defect that unit 2 already fixed. The fleet
# stalled with every repo holding the same rejected unit-1 PR.
#
# The root mistake is promoting a file at an intermediate state that a LATER
# unit has already corrected: that asks the panel to approve code known to be
# superseded. So once the oldest unit is picked, extend the endpoint forward
# over any later unit that touches a file this promotion already touches, and
# carry them together. Units that touch nothing in common are still left
# behind, so promotions stay small and reviewable.
if [ "$SPLIT" = "1" ]; then
  ext_idx="$picked_idx"
  changed="$(git diff --name-only "origin/$TGT...${units[$picked_idx]}" | sort -u)"
  j=$(( picked_idx + 1 ))
  while [ "$j" -lt "${#units[@]}" ]; do
    # Files this one unit changed. First-parent listing means ^ is the
    # previous unit, so this is exactly that unit's own contribution.
    unit_files="$(git diff --name-only "${units[$j]}^...${units[$j]}" 2>/dev/null | sort -u)"
    if [ -n "$unit_files" ] && [ -n "$changed" ] \
       && printf '%s\n' "$unit_files" \
          | comm -12 - <(printf '%s\n' "$changed") | grep -q .; then
      ext_idx="$j"
      # Everything from the target up to the new endpoint is in play now,
      # including any unit pulled in between.
      changed="$(git diff --name-only "origin/$TGT...${units[$j]}" | sort -u)"
    fi
    j=$(( j + 1 ))
  done
  if [ "$ext_idx" -ne "$picked_idx" ]; then
    echo "  extending unit $picked_idx -> $ext_idx: later unit(s) modify the same" \
         "file(s); promoting an already-superseded version would be rejected"
    if stage_to "${units[$ext_idx]}"; then
      picked="${units[$ext_idx]}"
      picked_idx="$ext_idx"
    else
      # Cannot happen (a superset of a real change is a real change), but if
      # it ever did, fall back to the unextended unit rather than promoting a
      # half-staged tree.
      echo "::warning::extension to ${units[$ext_idx]} was a content no-op -- keeping unit $picked_idx"
      stage_to "$picked" || true
    fi
  fi
fi

# Identify the unit for the PR title/body. A merge commit names its PR in the
# subject and carries the PR's own title on the first line of the body, which
# is far more useful than "Merge pull request #123 from user/branch".
unit_subject="$(git log -1 --format=%s "$picked")"
unit_pr="$(printf '%s' "$unit_subject" | sed -n 's/^Merge pull request #\([0-9][0-9]*\) .*/\1/p')"
if [ -n "$unit_pr" ]; then
  body_first="$(git log -1 --format=%b "$picked" | sed -n '/./{p;q;}')"
  [ -n "$body_first" ] && unit_subject="$body_first"
else
  # Squash merges land as "feat: thing (#123)".
  unit_pr="$(printf '%s' "$unit_subject" | sed -n 's/.*(#\([0-9][0-9]*\))[[:space:]]*$/\1/p')"
fi

# Upper bound: the units behind this one are not re-examined here, and any of
# them may yet be skipped as a no-op. Reported as "up to" for that reason.
remaining=$(( ${#units[@]} - picked_idx - 1 ))

if [ "$SPLIT" = "1" ]; then
  {
    echo "unit_sha=$(git rev-parse "$picked")"
    echo "unit_pr=$unit_pr"
    echo "remaining=$remaining"
  } >> "$out"
  # Multi-line values need the heredoc form of the step-output protocol.
  {
    echo "unit_subject<<PROMOTE_EOF"
    echo "$unit_subject"
    echo "PROMOTE_EOF"
  } >> "$out"
  echo "Promoting the oldest of ${#units[@]} un-promoted unit(s): ${unit_pr:+#$unit_pr }$unit_subject"
  echo "  up to $remaining further unit(s) will follow in later runs"
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

subject="$LABEL: $SRC -> $TGT"
[ "$SPLIT" = "1" ] && [ -n "$unit_pr" ] && subject="$subject (#$unit_pr)"

git commit -q -m "$subject" \
  -m "Code-only $LABEL. VERSION stays on ${TGT}'s own sequence, advanced one step here."
echo "changed=true" >> "$out"
# The approve step must wait for the run belonging to THIS commit; approving
# whichever parked run happens to exist first races with the run GitHub is
# still creating for the new head.
echo "sha=$(git rev-parse HEAD)" >> "$out"
