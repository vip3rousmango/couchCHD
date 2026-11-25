#!/usr/bin/env bash
# Called from build_linux.sh in its private native build workspace.
# Only the fixture generator imports test code. Conversion uses the shipped executable.
set -euo pipefail
root=$1; python=$2; deps=$3; shift 3
library=$PWD/format-smoke
PYTHONPATH="$root/tests:$deps" "$python" - "$library" <<'PY'
from pathlib import Path
import os, sys, time
from test_cso import _iso
from test_trim3ds import _card
from test_rvz import write_disc as optical
from test_xiso import write_disc as xbox

base = Path(sys.argv[1])
for folder in ('psp', '3ds', 'gc', 'wii', 'xbox', 'nes'):
    (base / folder).mkdir(parents=True)
(base / 'psp/Probe.iso').write_bytes(_iso())
(base / '3ds/Probe.3ds').write_bytes(_card())
(base / 'nes/Probe.nes').write_bytes(b'NES\x1a' + bytes([1]) + bytes(11 + 16384))
for system in ('gc', 'wii'):
    optical(base / system / 'Probe.iso', system=system)
xbox(base / 'xbox/Probe.iso')
stamp = time.time() - 60
for path in base.glob('*/*'):
    os.utime(path, (stamp, stamp))
PY
"$@" --headless --root "$library" --auto --psp-format cso --keep-originals --preset silent \
    --system psp --system 3ds --system gc --system wii --system xbox --system nes
for game in psp/Probe.cso 3ds/Probe.trim.cci gc/Probe.rvz wii/Probe.rvz xbox/Probe.xiso.iso nes/Probe.7z; do
    test -s "$library/$game"
done
# Use a separate reader, not the single-member 7z writer's own verifier.
bsdtar -xOf "$library/nes/Probe.7z" Probe.nes | cmp - "$library/nes/Probe.nes"

# Exercise discovery, guarded cleanup and cross-library reuse through the
# shipped entry point, not Python imports from the checkout.
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
export XDG_DATA_HOME="$work/state"
mkdir -p "$work/working/psp" "$work/card" "$work/pulled"
cp -p "$library/psp/Probe.iso" "$work/working/psp/Game.iso"
roms=$work/working
"$@" --root "$roms" --health
"$@" --root "$roms" --system psp --psp-format cso --plan-gib 1 > "$work/plan.json"
"$@" --root "$roms" --apply-plan "$work/plan.json" --preset silent
"$@" --root "$roms" --review > "$work/review.json"
id=$("$python" -c 'import json,sys; print(next(r["id"] for r in json.load(open(sys.argv[1])) if r["path"].endswith("/Game.cso") and not r["tested"]))' "$work/review.json")
"$@" --root "$roms" --mark-tested "$id"
"$@" --root "$roms" --review-cleanup > "$work/reclaim.json"
token=$("$python" -c 'import json,sys; p=json.load(open(sys.argv[1])); assert p["count"] == 1; print(p["token"])' "$work/reclaim.json")
"$@" --root "$roms" --reclaim-reviewed "$token" --yes
"$@" --handheld-profiles > "$work/profiles.json"
"$python" -c 'import json,sys; assert {"rg405m","arkos","muos"} <= {p["id"] for p in json.load(open(sys.argv[1]))}' "$work/profiles.json"
"$@" --root "$roms" --handheld-list --handheld-profile android-t618 --handheld-player ppsspp > "$work/eligible.json"
"$python" -c 'import json,sys; assert [r["path"] for r in json.load(open(sys.argv[1]))] == ["psp/Game.cso"]' "$work/eligible.json"
"$@" --root "$roms" --handheld-export psp/Game.cso --dest "$work/card"
cmp "$roms/psp/Game.cso" "$work/card/psp/Game.cso"
"$@" --catalog-add 'Handheld SD' "$work/card" > "$work/source.json"
source_id=$("$python" -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$work/source.json")
"$@" --catalog-refresh
"$@" --catalog-pull "$source_id" psp/Game.cso --dest "$work/pulled"
cmp "$roms/psp/Game.cso" "$work/pulled/psp/Game.cso"

# The NTFS-like master is an ingest source, never a scratch directory. Process
# loose + zipped ISOs together and produce a CSO identical to loose conversion.
mkdir -p "$work/master/psp" "$work/zip-results"
cp -p "$library/psp/Probe.iso" "$work/master/psp/Loose.iso"
"$python" - "$work/master/psp" <<'PY'
from pathlib import Path
import os, sys, zipfile
folder = Path(sys.argv[1])
archive = folder / 'Packed.zip'
with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as files:
    files.write(folder / 'Loose.iso', 'Dump/Original.iso')
stamp = (folder / 'Loose.iso').stat().st_mtime
os.utime(archive, (stamp, stamp))
PY
"$@" --root "$work/zip-results" --auto --ingest "$work/master" --system psp --psp-format cso --preset silent
cmp "$work/zip-results/psp/Loose.cso" "$work/zip-results/psp/Packed.cso"
test -s "$work/master/psp/Packed.zip" && test -s "$work/master/psp/Loose.iso"
