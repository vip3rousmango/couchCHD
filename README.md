# CouchCHD

CouchCHD is a controller-first disc library compressor for the living room. It converts supported disc images with MAME's `chdman`, verifies the result, builds portable multi-disc playlists, and keeps the originals until you explicitly purge them. It does not download games. Use disc images you are authorized to process.

## Install from GitHub

Run this **on the target Linux device**. Use Konsole in Desktop Mode on Steam Deck/Bazzite, or open an SSH session to your retro appliance first:

```sh
curl -fsSL https://raw.githubusercontent.com/vip3rousmango/couchCHD/main/scripts/install-device.sh | sh
```

This runs code from this repository as your current user. To inspect it first, download the script to a file, read it, then run `sh install-device.sh`. Don't use `sudo` on the Deck. No build machine, clone, Python installation, or GitHub login is required on the target.

The installer selects the x86-64 AppImage or aarch64 Linux tarball from the pinned [GitHub testing release](https://github.com/vip3rousmango/couchCHD/releases/tag/v0.1.0-preview.1), verifies its SHA-256 against that release's manifest, and runs `--version` before activating it. It stops on a missing release, failed download or checksum mismatch. Both architectures require a **64-bit userspace, glibc 2.36+**, an executable data volume, and compatible graphics/input drivers for the UI. A Pi 4 running a 32-bit image is not supported by these binaries.

Desktop Linux gets an Applications menu entry and launches immediately if a graphical display is available. On appliances the installer registers the launcher, tells you how to refresh the menu, and leaves the current frontend running. Setup does not convert or purge anything.

| Detected frontend | Where to launch CouchCHD |
| --- | --- |
| [Recalbox EmulationStation](https://wiki.recalbox.com/en/advanced-usage/systems-display) | **Ports → CouchCHD** via a custom port system, not a Settings plugin. Restart Recalbox normally to reload the system list. Enable **Show Ports System** if hidden. |
| [Batocera](https://wiki.batocera.org/systems:ports) | **Ports → CouchCHD**; update the game lists. |
| [RetroPie](https://github.com/RetroPie/RetroPie-Setup/blob/master/scriptmodules/supplementary/retropiemenu.sh) | The **RetroPie configuration** carousel. Install as the frontend user, not with `sudo`. |
| ROCKNIX | **Ports → CouchCHD** on supported 64-bit images. |
| [Standalone EmulationStation](https://github.com/Aloshi/EmulationStation#writing-an-es_systemscfg) / [ES-DE](https://gitlab.com/es-de/emulationstation-de/-/blob/master/USERGUIDE.md) / native EmuDeck ES-DE | Uses a configured, shell-compatible Ports system. Rescan or restart the frontend after registration. Setup reports when it cannot safely register. |
| RetroDECK Flatpak | Use the host desktop or Steam entry. The installer doesn't grant sandbox permissions or insert a host executable into a sandbox that can't launch it. |

These are integration targets, not hardware certifications. Test with disposable media first; display drivers and the installed OS image matter as much as the board model.

### Recalbox Web Manager

**Ports → CouchCHD** opens the app on the console, not in a browser. In Recalbox 10.1.2, Manager v3's [sidebar](https://gitlab.com/recalbox/recalbox/-/blob/10.1.2/projects/manager3/src/layouts/MainLayout.vue) and [routes](https://gitlab.com/recalbox/recalbox/-/blob/10.1.2/projects/manager3/src/router/routes.ts) are compiled into the [system image](https://gitlab.com/recalbox/recalbox/-/blob/10.1.2/package/recalbox-manager3/recalbox-manager3.mk); its [ROM table](https://gitlab.com/recalbox/recalbox/-/blob/10.1.2/projects/manager3/src/components/emulation/EmulationSystemsRomsTabSystemContent.vue) has no launch action. We found no supported third-party menu registration. The installer doesn't patch Manager, remount the OS, or start a web service. A native browser tab would need an upstream extension point or an upstream CouchCHD integration.

To check conversion history from Bazzite without opening another network port:

```sh
ssh root@recalbox.local '/recalbox/share/system/couchchd/run-couchchd --root /recalbox/share/roms --history'
```

### Other Linux frontends

For a frontend with an existing ROM root and a **Ports system configured to execute `.sh` files**, register explicitly if its layout wasn't recognized:

```sh
app=~/.local/share/couchchd/run-couchchd  # or the full launcher path printed by setup
"$app" --register-frontend ports "$app" --root /path/to/roms
```

This writes `ports/CouchCHD.sh`; it doesn't install an emulator or make an unsupported file type executable by a frontend. For launchers with an **Add application** workflow, point it at `run-couchchd` instead. Steam registration with artwork is available from inside CouchCHD's Settings. Console-only RetroArch/Lakka images without an external application launcher do not gain a Settings plugin. Neither macOS, Windows, musl-only Linux, nor 32-bit images can use these Linux binaries.

## Optional: private SSH builds

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

Bazzite probes the target over SSH and streams the matching artifact. No Python, GitHub token, HTTP server, or internet connection is needed on the target. Installation and frontend registration are the same as for the public installer.

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

On GitHub, run **Native Linux release candidates** manually to build both architectures without publishing. Numeric preview tags (`vX.Y.Z-preview.N`) publish test prereleases after native build checks; other version tags stage drafts for maintainer review. See [bundle installation and checksums](packaging/INSTALL.txt).

CouchCHD is licensed under [GPL-3.0-only](LICENSE). MAME, Qt, SDL and other distributed dependencies retain their respective licenses. Product requirements and platform caveats are in [SPEC.md](SPEC.md).
