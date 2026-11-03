#!/bin/bash
# Measure how big the git history of one simulated variant is after N commits.
#
# usage: measure.sh RUN_DIR VARIANT N [N ...]
#   RUN_DIR  the WORK_DIR given to sim.py (it holds one git repository per variant)
#   N        number of commits to keep, counted from the first one (1 = the starting point)
#
# For each N the repository is cloned, cut at commit N and fully repacked with limited memory
# (a plain "git gc --aggressive" was killed for lack of memory on the large files), and the size of the pack is printed.
RUN=$1; V=$2; shift 2
OUT="$RUN/measure-tmp"
mkdir -p "$OUT"
for K in "$@"; do
  D="$OUT/${V}_$K"
  rm -rf "$D"
  git clone -q --no-local --no-hardlinks "$RUN/$V" "$D"
  SHA=$(git -C "$D" rev-list --reverse HEAD | sed -n "${K}p")
  git -C "$D" reset -q --hard "$SHA"
  rm -f "$D/.git/ORIG_HEAD"
  git -C "$D" reflog expire --expire=now --all
  git -C "$D" remote remove origin
  if git -C "$D" -c pack.windowMemory=1g -c pack.threads=2 repack -a -d -f -q --window=50 --depth=50; then OK=true; else OK=false; fi
  git -C "$D" prune --expire=now
  N=$(git -C "$D" rev-list --count HEAD)
  KB=$(git -C "$D" count-objects -v | awk '/size-pack/{p=$2} /^size:/{l=$2} END{print p+l}')
  echo "{\"variant\":\"$V\",\"commits\":$N,\"repack_ok\":$OK,\"pack_MB\":$(python3 -c "print(round($KB*1024/1e6,1))")}"
  rm -rf "$D"
done
