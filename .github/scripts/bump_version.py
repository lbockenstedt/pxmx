"""Bump a MAJOR.MINOR VERSION file by one minor step, preserving zero-padding.

Branch-owned versioning: each branch (dev/qa/main) advances its OWN counter.
Promotion never carries VERSION across branches, so the value read here is
always this branch's own lineage -- qa on 1.45 becomes 1.46 no matter what dev
says.

MAJOR is never advanced automatically; it is set by hand. At MINOR 99 the bump
HOLDS rather than rolling over, and says so loudly. A previous version of this
script held SILENTLY and every run stayed green while the version froze --
which meant deployed hubs stopped seeing updates and nobody noticed.
"""
import re
import sys

path = sys.argv[1]
with open(path) as fh:
    raw = fh.read().strip()

m = re.match(r"^(\d+)\.(\d+)$", raw)
if not m:
    # Legacy ".NN" or any other shape: bump the final numeric run, keeping width.
    nums = list(re.finditer(r"\d+", raw))
    if not nums:
        print(f"::warning::{path}: no numeric segment in {raw!r}; left unchanged")
        sys.exit(0)
    last = nums[-1]
    new = raw[: last.start()] + str(int(last.group()) + 1).zfill(len(last.group())) + raw[last.end():]
else:
    major, minor_s = m.group(1), m.group(2)
    minor = int(minor_s)
    if minor >= 99:
        print(f"::warning::{path}: held at {raw} -- MINOR is exhausted. "
              f"Bump MAJOR by hand to resume automatic versioning.")
        sys.exit(0)
    new = f"{major}.{str(minor + 1).zfill(max(2, len(minor_s)))}"

with open(path, "w") as fh:
    fh.write(new + "\n")
print(new)
