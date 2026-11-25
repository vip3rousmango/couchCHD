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
