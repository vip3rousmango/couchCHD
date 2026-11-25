# CouchCHD

CouchCHD is a controller-first disc library compressor for the living room. It converts supported disc images with MAME's `chdman`, verifies the result, builds portable multi-disc playlists, and keeps the originals until you explicitly purge them. It does not download games. Use disc images you are authorized to process.

## One-command device setup (private SSH)

**Once on Bazzite:** keep this checkout at `~/couchCHD`. Put each trusted Linux build **and its matching `SHA256SUMS-<arch>`** in `dist/release/<arch>/`. These are the directories produced by `scripts/build_linux.sh` on native Debian 12 build machines. With a private Actions build, the equivalent download from the checkout is:

```sh
gh run download RUN_ID -n release-x86_64 -D dist/release/x86_64
gh run download RUN_ID -n release-aarch64 -D dist/release/aarch64
```

Only the target architecture is required. The source checkout alone is not a binary build. Do not generate a new checksum for an untrusted download.

**Pi 4 / Recalbox — type this on Bazzite**, from the checkout, substituting your normal SSH destination:

```sh
python3 scripts/send-device.py root@recalbox.local
```

Bazzite probes the target over SSH, streams the matching build and authenticates it against the build manifest. No Python, GitHub token, inbound HTTP server, or internet connection is needed on the Pi. Installation uses `/recalbox/share/system/couchchd`. First launch runs a **headless scan** of `/recalbox/share/roms` and prints the result over SSH. It does not convert or purge anything. For the TV UI, first stop EmulationStation normally, then run `/recalbox/share/system/couchchd/run-couchchd` on the Pi. The ARM bundle requires 64-bit userspace, glibc 2.36+ and working graphics drivers for the UI.

**Steam Deck — type this in Desktop Mode’s Konsole.** Set `host` to your Bazzite SSH login; Bazzite must accept SSH connections. Confirm its host-key fingerprint on first use. SSH keys avoid repeated password prompts.

```sh
host=user@bazzite.local; ssh "$host" 'cat couchCHD/scripts/install-device.sh' | sh -s -- "$host"
```

The Deck downloads the x86-64 AppImage from Bazzite, checks SHA-256, and opens the UI as your normal desktop user. Relaunch with `~/.local/share/couchchd/run-couchchd` (under `$XDG_DATA_HOME` if set). If the Deck accepts SSH, the Bazzite push command also works with `deck@steamdeck.local`; a display-less session prints the launcher path instead of opening a window.

Both paths stage and test the executable before switching the launcher. Repeating setup reuses the same build directory. Distinct previous builds remain under `builds/`; configs, originals, backups and the undo ledger are not replaced. No release is published and no system partition is remounted.

## Run from source

Python 3.11+; `chdman` 0.256 or newer is required **for conversion and extraction**, not for scanning. Use the project-built binary, put a supported `chdman` on `PATH`, or set `COUCHCHD_CHDMAN` to its absolute path. Optional cloud ingestion uses an already configured `rclone` installation. A mounted NAS needs no rclone configuration.

```sh
python3 -m venv venv
. venv/bin/activate
python -m pip install '.[gui,test]'
couchchd --root ~/Emulation/roms
```

For a headless install, use `python -m pip install .` instead. Select the **ROM root** (the directory containing `psx/`, `dreamcast/`, etc.). Use the same root for every instance. Do not point another instance, transfer tool, or emulator at a subdirectory that is already being processed. Do not move the library or edit the database during a batch.

```sh
# Inspect without converting or moving any ROMs. Writes lock/history in the library.
couchchd --root /games/roms --headless
# Verify first with originals left in their current locations.
couchchd --root /games/roms --headless --auto --system psx --keep-originals
# Convert a different system and quarantine its verified originals (the default).
couchchd --root /games/roms --headless --auto --system saturn
# Read one disc at a time from a configured remote; never delete remote originals.
couchchd --root /games/roms --auto --ingest 'gdrive:Disc archives'
couchchd --root /games/roms --history
couchchd --root /games/roms --undo
couchchd --root /games/roms --extract '/games/roms/psx/Example.chd' --dest /games/extracted
```

Headless commands print JSON to stdout and progress/errors to stderr. Batch exit status: `0` completed, `1` error, `2` paused/cancelled. After fixing a storage or battery warning, start the batch again. Existing CHDs are not overwritten. Inspect scan issues separately: an archived, incomplete, or invalid input is not a successful conversion.

## What verification covers

- CUE/GDI tracks must resolve to nonempty regular files; ambiguous case matches, unsupported layouts, truncated sectors, and active transfers are not eligible. Original descriptors are never edited in place. ISO images require a recognized volume header. See [the system and format table](SPEC.md).
- CD uses `createcd` defaults; DVD uses `createdvd -hs 2048`. Before publication the child must succeed, `chdman verify` must pass, and the CHD geometry must match the input. The RetroAchievements indicator describes this format policy, **not** a network lookup or proof that a game is in the RetroAchievements database.
- Originals move to `_couchchd_backup/` only after verified publication. Shared tracks stay in place while a live descriptor might still need them. Relative `.m3u` entries point to `.chd_discs/`; single discs keep their normal CHD names. Stop Recalbox EmulationStation before metadata changes; re-run Steam ROM Manager after paths change.
- The SQLite ledger (`couchchd_history.db`) tracks filesystem operations. Undo restores originals without overwriting conflicts and retains generated CHDs under `_couchchd_backup/.undo/`. A changed file is grounds to stop, not permission to overwrite it.
- Quarantine on the same volume does **not** reclaim space. After testing the CHDs in your emulator and making an independent backup, `couchchd --root /games/roms --purge --yes` permanently deletes authenticated quarantined files. A purged batch cannot be undone. BIN/CUE extraction creates a playable disc representation, not an exact reconstruction of original descriptor text, filenames, or layout.

Quarantine is not protection against drive failure. Keep an independent backup of valuable dumps and the ledger. For a NAS, use a filesystem with working cross-process locks, flush, and rename semantics. For local scratch use a fast filesystem with adequate headroom (`TMPDIR` on Linux). Recalbox data belongs on the persistent `/recalbox/share` partition.

## Controller

D-pad / left stick navigates. South confirms, east goes back, west inspects, north triggers the current primary action. Bumpers switch tabs; Start opens settings. Prompts follow the controller family. Use a **gamepad** Steam Input layout rather than a keyboard-emulating layout. All dialogs stay inside one window.

Settings offers three cleanup policies: quarantine (default), keep in place, and immediate deletion. Immediate deletion requires an explicit confirmation, authenticates backups for the current job, and cannot be undone. It never authorizes purging unrelated backups.

## Development and distribution

```sh
python -m pytest
```

Tests use synthetic sectors and temporary libraries, not bundled ROMs. Install `chdman` or set `COUCHCHD_CHDMAN` to exercise the real conversion round trips as well as fault-injection tests.

The native Linux release build compiles `chdman` from the pinned official MAME source using [`scripts/fetch_chdman.sh`](scripts/fetch_chdman.sh); it is not a downloaded opaque executable. The release targets are an x86-64 AppImage and an ARM64 Recalbox bundle. The source checkout does not itself contain these binaries. Device compatibility, display drivers and controllers must be validated on the target OS before describing a build as a tested release.

On GitHub, run **Native Linux release candidates** to build both architectures without publishing. A `v*` tag stages a draft release with SHA-256 manifests. [Bundle installation and checksums](packaging/INSTALL.txt).

CouchCHD is licensed under [GPL-3.0-only](LICENSE). MAME, Qt, SDL and other distributed dependencies retain their respective licenses. Product requirements and platform caveats are in [SPEC.md](SPEC.md).
