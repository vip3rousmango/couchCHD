# CouchCHD

CouchCHD is a controller-first ROM library compressor. It converts locally, verifies outputs before publication, and journals changes so you can undo a batch before purging backups. It works alongside EmuDeck; it does not install emulators or download games. Use images you are authorized to process.

## Install from GitHub

Run this **on the target Linux device**. Use Konsole in Desktop Mode on Steam Deck/Bazzite, or open an SSH session to your retro appliance first:

```sh
curl -f https://vip3rousmango.github.io/couchCHD/i | sh
```

This runs code from this repository as your current user. To inspect it first, download the script to a file, read it, then run `sh install-device.sh`. Don't use `sudo` on the Deck. No build machine, clone, Python installation, or GitHub login is required on the target.

The installer selects the x86-64 AppImage, aarch64 tarball or single-card Recalbox image from the pinned [GitHub testing release](https://github.com/vip3rousmango/couchCHD/releases/tag/v0.1.0-preview.16), verifies SHA-256 against the release manifest, and runs `--version` before activating it. Both architectures require **64-bit Linux userspace, glibc 2.36+**, and compatible graphics/input drivers for the UI. A Pi 4 running a 32-bit image is not supported.

Desktop Linux gets an Applications menu entry and launches immediately if a graphical display is available. On appliances the installer registers the launcher, tells you how to refresh the menu, and leaves the current frontend running. Setup does not convert or purge anything.

**Pi 4 / Recalbox over SSH:** run the public install command above **at the Pi prompt**. On a single SD card with [Recalbox 10.1.2’s exFAT masks](https://gitlab.com/recalbox/recalbox/-/blob/10.1.2/board/recalbox/fsoverlay/recalbox/scripts/recalbox-mount.sh) (`fmask=0133,dmask=0022`), setup stores a compressed SquashFS application image under `/recalbox/share/system/couchchd`. The launcher verifies it and mounts it read-only on demand. You don’t need another drive, an executable ROM partition, or enough RAM for a full unpacked copy. Use the normal Recalbox root login: this mode needs kernel loop and SquashFS support and permission to mount the image. Restart Recalbox when convenient and open **Ports → CouchCHD**. If Ports is hidden, enable **Show Ports System**.

Setup leaves the boot partition, SHARE mount options and ROMs alone. An explicitly `noexec` SHARE is **not** treated as an exFAT file-mode issue. In that case setup still looks for a separate trusted, executable Linux data volume and stops before downloading if none is available. If you already installed on another volume, leave it attached for launches and updates.

**Steam Deck / Gaming Mode:** In Desktop Mode, choose **Steam → Exit** (closing the window is not enough), then run the installer above. Setup registers the persistent `run-couchchd` launcher for your signed-in native Steam account. Return to Gaming Mode and choose **Library → Non-Steam → CouchCHD → Play**. You don't need to manually add the AppImage or desktop file. If you already did, leave the shortcut in place during setup.

**If Play immediately returns:** under the shortcut's **Properties → Shortcut**, Target should be `"/home/deck/.local/share/couchchd/run-couchchd"` for the default Deck installation, Start In should be `"/home/deck/.local/share/couchchd"`, and Launch Options should normally be empty. Under **Compatibility**, leave **Force the use of a specific Steam Play compatibility tool** unchecked: this app is native Linux. After attempting Play, inspect `~/.local/share/couchchd/steam-launch.log` in Desktop Mode. A fresh log means the launcher ran; if no fresh log appeared, check the target and compatibility setting first.

**Desktop Mode (including Bazzite):** search for **CouchCHD** in the application launcher, or open **Applications → Utilities → CouchCHD** on KDE. This installs a menu entry, not a file on your desktop. Fedora Atomic's `/home` → `/var/home` alias is supported; links inside your applications or icons directories are not followed. If an older installer printed “Installed” and then refused the menu path, exit CouchCHD and rerun the short install command in a desktop terminal to repair the entry. The x86-64 launcher uses software rendering to avoid OpenGL startup failures. In ES-DE, rescan the ROM directory and look under **Ports**.

**Update from the couch:** after installing preview.4 or later with the public installer, open **Settings → Update CouchCHD** when idle. One press checks the project's release channel and installs a newer verified build if available. Exit and reopen CouchCHD normally to use it; the updater doesn't restart Steam, change menu entries, or touch your ROM library. If you're on preview.3 or earlier, finish or cancel your batch, exit CouchCHD, then run the short install command once to get this option. Private and source installations stay on their existing update path.

After updating, reopen CouchCHD and choose **Settings → Refresh launcher artwork**. It fills missing portrait, landscape, hero, logo and icon slots and replaces recognized stock artwork; custom images stay untouched. For previews 1–4 the portrait and hero were drawn on-device, so replacement requires an exact match to the old render. Completely exit and restart Steam to reload cached art in both Desktop and Gaming Mode. If Refresh reports a missing shortcut icon, exit Steam in Desktop Mode and run Refresh again before reopening Steam. Recalbox reuses the portrait, and the desktop menu uses a persistent icon file.

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

### Choose a handheld by firmware and player

Use **Handheld export** on your Deck or Bazzite machine. Choose the destination profile, confirm the player/core you actually use, then choose the games and the mounted card folder. The picker filters formats for that combination. It does not equate matching hardware, an Android launcher, or an SD volume label with a working emulator. When the firmware permits custom folders, choose the existing folder for that platform instead of letting an app invent a second library.

See the [Linux and dual-boot device research](docs/anbernic-linux.md) and [Android device research](docs/anbernic-android.md) for model variants, specifications, firmware evidence, and a physical-device checklist. These are **export destinations**, not a list of tested native Linux installs. A console can play a transferred game without running CouchCHD. On the command line, `--handheld-profiles` lists profile IDs and compatible player IDs; use `--handheld-profile ID` and repeat `--handheld-player ID` with `--handheld-list` or `--handheld-export`.

**Testing an unfamiliar handheld?** Start with one nonessential game and its existing save. Record the model suffix, firmware name/version, exact player/core, card folder, whether the frontend finds it, controller navigation, and (where applicable) a disc swap. On a shell-accessible console, run this non-installing report for CPU userspace, RAM and visible tools:

```sh
curl -fsS https://vip3rousmango.github.io/couchCHD/i | sh -s -- --device-report
```

The report does not scan ROMs, change files, or upload anything. If there's no shell, **Settings → About** plus the emulator version is sufficient to start a test report.

### Anbernic RG405M / Android / Daijishō

**You can play the converted discs without installing CouchCHD on the handheld.** The stock [RG405M](https://anbernic.com/products/rg405m) has a 64-bit T618 and Android 12. Neither the Linux AppImage nor the glibc-based Recalbox ARM bundle is an Android app. Don't run the Linux installer on the RG405M or add its shell script as a Daijishō player.

1. On Bazzite, a Steam Deck, or a remote supported Linux host, copy one game into a working ROM library (for example, `roms/psx/` with its `.cue` **and all referenced tracks**). Convert there with **Keep originals** or the [`--keep-originals` CLI example](#run-from-source). Wait for verification.
2. Choose **Handheld export**, select the titles, and point it at the mounted card's ROMs folder. CouchCHD copies supported PSX/Dreamcast CHDs, complete CHD playlists or PSP CHD/CSO, checks the copied bytes, skips identical files, and refuses conflicting copies. For MTP-only USB transfer, export to a local folder first, then transfer its contents yourself; that final manual copy is not verified by CouchCHD. Keep the master library until you've tested booting and changing discs.
3. In Daijishō highlight the platform, choose **Add more → Select folder → Sync** (just **Sync** if the folder is already registered). Choose a [player that accepts the output](https://github.com/TapiocaFox/Daijishou/wiki/Players-and-File-Extensions). For PS1, [Beetle PSX](https://docs.libretro.com/library/beetle_psx/#chd) supports CHD and M3U; launch the playlist for disc swapping. For GameCube/Wii, [Dolphin recommends RVZ](https://dolphin-emu.org/docs/faq/#supported-dump-formats), not this CHD workflow. Changing a [player's filename filter](https://github.com/TapiocaFox/Daijishou/wiki/How-to-Use-Daijish%C5%8D) can make a game visible but can't teach the emulator a new format.

For your PSP batch, select a current [PPSSPP](https://www.ppsspp.org/docs/getting-started/dumping-games/) build: CHD support starts at 1.17, and CouchCHD uses its recommended DVD image mode for PSP ISOs. Smoke-test a game in the chosen emulator before removing sources; a valid checksum does not prove that the installed player supports the image.

**Converting on the RG405M itself would need a port.** A signed APK with a normal launcher activity could open from Daijishō's **Apps** tab or its [pin-apps widget](https://github.com/TapiocaFox/Daijishou/releases/tag/v1.3.0); neither shortcut runs Linux software. The APK would need to package the converter for Android's [`arm64-v8a` ABI](https://developer.android.com/ndk/guides/abis) (for example, chdman code built as a native library), not download and execute a helper from [writable app storage](https://developer.android.com/about/versions/10/behavior-changes-10#execute-permission). It would also need its **own** [Storage Access Framework folder grant](https://developer.android.com/training/data-storage/shared/documents-files#grant-access-directory) for a user-selected ROM subfolder, not a hard-coded SD path or Daijishō's permissions. Handling document URIs, resolving sibling tracks and safely verifying output before touching originals would need testing on actual hardware. No CouchCHD APK or tested on-device conversion is available.

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

## Formats and emulator compatibility

Choose a format for the **emulator that will play the game**, not just the smallest file. CouchCHD does not transcode an entire directory based on filename extension alone.

| Input | Output | What matters |
| --- | --- | --- |
| Supported CD/GD collections (`.cue` plus tracks; Dreamcast also `.gdi`) and PS2 DVD images | CHD | Existing PS1, PS2, Dreamcast, Saturn and Sega CD support, plus CUE-based PC Engine CD, PC-FX, Neo Geo CD, 3DO, Amiga CD32 and CDTV for compatible cores. Do not convert individual track files. |
| PSP ISO | CHD (default) or CSO | Choose the PSP format in Settings or pass `--psp-format cso`. Existing files are not renamed. |
| Raw GameCube or Wii ISO | RVZ | Dolphin format; no scrubbing. Compare decoded data against the source before publishing. |
| Original Xbox full-disc XGD1 image | `.xiso.iso` | A playable filesystem repack, **not** a sector-exact backup. Existing XISOs and Xbox 360 discs are not candidates. Always retains the input. |
| Nintendo 3DS CARD1 `.3ds` / `.cci` | `.trim.cci` | Preserves cartridge content and removes known padding; uses the [CCI extension accepted by Azahar](https://azahar-emu.org/blog/game-loading-changes/). Trimming does not decrypt a game. Writable CARD2 and ambiguous images are not eligible. Always retains the input. |
| Recognized single-cartridge ROMs for supported NES, SNES, Game Boy, GBA, N64, Mega Drive, Master System and Game Gear cores | `.7z` | One ROM per archive; verify the extracted bytes. Check that your frontend **and core** support archives. No arcade sets, BIOS collections, disc tracks or nested archives. |

Multidisc organization is separate from compression. Don't assume that an emulator accepting CHD accepts M3U. After updating an EmuDeck library, rescan ES-DE or rerun Steam ROM Manager as appropriate. See [when to use CouchCHD alongside EmuDeck](docs/emudeck-comparison.md).

### Mixed folders: loose discs and ZIPs

Do **not** unpack an entire collection first. A normal scan distinguishes loose discs, supported single-disc ZIP packages, already-playable cartridge archives, and packages that need manual review. An archive is not permission to overwrite a loose game with the same output name.

- **PSP ZIP → CSO:** choose CSO in Settings. CouchCHD reads the ISO member as a stream and opens it again to verify the resulting CSO sector by sector. The card needs space for the new output and a safety reserve, **not** for a temporary ISO.
- **ZIP → CHD or RVZ:** these tools need a seekable disc file. CouchCHD stages one supported disc at a time, checks space on the actual scratch and output filesystems, and removes its temporary data on completion or cancellation. On one volume, budget for **the expanded disc plus a conservative output allowance and reserve**, in addition to the ZIP already on disk. To put the expanded copy on a different disk, choose a temporary workspace or pass `--scratch /path/to/existing/folder`.
- **Original ZIPs stay put**, including under immediate cleanup. An emulator-ready output can be larger than its ZIP: packaging for play is not necessarily reclaiming space. ZIP conversions therefore do not count toward a space-recovery target. Encrypted, nested, ambiguous and unsupported packages are not bulk-extracted.

Accepted disc ZIPs are stored or DEFLATE-compressed (including ZIP64), with one supported ISO/GCM or one CUE/GDI and only its referenced tracks. The reader limits a package to 256 entries, a 256 KiB central directory, a 1 MiB descriptor and 16 GiB of expanded disc data. Extra games, unrelated files and nested archives require manual review. The ZIP filename supplies the output title, not the internal ISO filename.

For a read-only NTFS collection on Bazzite, use **mounted-folder ingest** into a separate writable library. Use the compressed catalog instead of ingest when another machine already has a playable output. Remote archive URLs and RAR/7z disc containers are not streamed as ZIPs; mount the source or handle unsupported packages separately.

### Reuse compressed games across devices

In **NAS / Cloud**, add named libraries: for example, the Deck's ROM folder, the Bazzite-mounted NTFS library, and the RG405M card while it is in a reader. Choose **Refresh**, browse the compressed inventory, then select a specific location and pull into a writable ROM library. A disconnected source keeps its last-seen inventory; it must be reachable to transfer. Matching paths identify possible copies, **not** identical discs or regions.

Sources can be existing mounted directories (local, SMB, NFS, SSHFS) or **already configured** rclone remotes, including SFTP. For SFTP, verify the peer's SSH host key and configure rclone's `known_hosts_file`. A Pi without rclone can use an OS-mounted share. No CouchCHD listener, automatic device enrollment, background mirror or delete propagation. Each receiver remembers the libraries you add to it. An Android card in a reader is a source; Daijishō is not a network service.

```sh
couchchd --catalog-add 'Deck' deck:/home/deck/Emulation/roms
couchchd --catalog-add 'Windows master' /mnt/windows/roms
couchchd --catalog-refresh
couchchd --catalog-list
# Choose a source ID and a relative path from the listing:
couchchd --catalog-pull SOURCE_ID 'psx/Example.chd' --dest /games/roms
```

`deck:` above must already exist in the **receiving** device's rclone configuration. Pull verifies transferred bytes, not emulator compatibility or the relationship to a raw ISO. Configured remote transfers may read the selected payload twice for independent verification. They do not add play-test marks or authorize deletion of any original.

### Make room without guessing which originals to delete

Choose a writable **working library** for conversion. If Bazzite can see a Windows NTFS drive with your master dumps, leave it mounted read-only and use it as an ingest source, not the conversion destination. Health checks and handheld exports can read a separate source folder without opening its conversion ledger.

1. Run a **library health check** before a big batch. Findings explain what to fix; they do not rewrite descriptors or delete tracks.
2. **Plan space** for all or selected systems. The plan shows estimates and excludes games that cannot be staged safely. Original files and verified outputs coexist until you reclaim space. An estimate is not a promise that this many bytes will be freed.
3. Run the plan, then launch the new output **in your emulator**. Check an existing save, music, and disc swaps when applicable. Mark the verified output as play-tested; unmark it if something fails. CouchCHD cannot infer a successful play-test from the game's checksum.
4. Preview reviewed cleanup and read the file list and affected batches before confirming. Permanent deletion ends Undo for the affected batches. **Keep an independent copy** of originals you cannot replace.

The same workflow works over SSH. For example, on a **working copy** of a PSP library:

```sh
couchchd --root /games/roms --health
couchchd --root /games/roms --system psp --plan-gib 10 > /tmp/space-plan.json
cat /tmp/space-plan.json
couchchd --root /games/roms --apply-plan /tmp/space-plan.json
couchchd --root /games/roms --review
```

Use the ID from `--review` with `--mark-tested ID` after playing. `--review-cleanup` lists exactly which backups are eligible and returns a confirmation token. `--reclaim-reviewed TOKEN --yes` is the **scoped** cleanup command; the older `--purge --yes` is **not** a shortcut for it.

## Run from source

Python 3.11+. CHD conversion/extraction requires `chdman` 0.256 or newer (`COUCHCHD_CHDMAN`, bundled copy or `PATH`); RVZ requires DolphinTool. Scanning doesn't need either helper. Optional cloud ingestion uses an already configured `rclone` installation. A mounted NAS needs no rclone configuration.

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
# Use CSO for PSP; keep the original ISOs for emulator testing.
couchchd --root /games/roms --auto --system psp --psp-format cso --keep-originals
# Convert a different system and quarantine its verified originals (the default).
couchchd --root /games/roms --headless --auto --system saturn
# Read one disc at a time from a configured remote; never delete remote originals.
couchchd --root /games/roms --auto --ingest 'gdrive:Disc archives'
couchchd --root /games/roms --history
couchchd --root /games/roms --undo
couchchd --root /games/roms --extract '/games/roms/psx/Example.chd' --dest /games/extracted
```

Headless commands print JSON to stdout and progress/errors to stderr. Batch exit status: `0` completed, `1` error, `2` paused/cancelled. After fixing a storage or battery warning, start the batch again. Existing CHDs are not overwritten. Inspect scan issues separately: an archived, incomplete, or invalid input is not a successful conversion.

### Large batches on a Deck or Pi

Treat the CPU budget and the compression speed as separate settings: using fewer CPUs leaves room for Gaming Mode, while doing less work per sector can make an ISO finish sooner. A busy CPU core can still show 100%; the converter's limit is relative to **all available logical CPUs**, not a guarantee about package temperature, power draw, or other applications.

Recognized Decks and Pis run **one disc at a time**, including verification. On an eight-thread Deck, Balanced and Docked allow the compressor five logical CPUs (62.5%); Silent allows four. A four-core Pi 4 allows two, or one on models with 2 GiB RAM or less or unknown memory. On these devices a detected temperature of 80°C or higher reduces the active conversion to one CPU for the rest of the disc. Limits rely on Linux CPU affinity and visible sensors; they are not a thermal guarantee.

On Linux, portable conversion workers run at lower scheduling priority. When CPU topology is available, the compressor leaves one physical core and its SMT siblings available to the frontend if the process has access to another core. This leaves room for the controller and compositor; it does not set a frame rate or restrict other applications.

During a batch, use the D-pad to browse individual discs. The percentage belongs to the current stage (compression or verification), not the entire batch. MiB/s is estimated logical disc data processed, not SD-card write speed. CPU is the converter's measured share of the device's available CPUs, not total system load. A blank metric means no measurement yet. Checking a row does not pause the converter.

If the device feels too hot during a batch, press **B → Cancel batch** and wait for confirmation. Completed verified discs stay in History. The interrupted disc restarts from its original on the next batch; it does not resume from a partial CHD. Let the device cool before restarting or updating.

## What verification covers

- CUE/GDI tracks must resolve to nonempty regular files; ambiguous case matches, unsupported layouts, truncated sectors, and active transfers are not eligible. Original descriptors are never edited in place. ISO images require a recognized volume header. See [the system and format table](SPEC.md).
- CD always retains the default `createcd` hunk size. Portable devices try CD Deflate and CD FLAC, skipping LZMA; other systems keep the default codec search. DVD-mode ISOs (PSP and PS2 DVD) use `createdvd -hs 2048 -c zlib`. Trying fewer lossless codecs can finish sooner at the cost of a larger file on some discs. Both raw and overall SHA-1 and matching disc geometry are still required. The RetroAchievements indicator describes this format policy, **not** a network lookup or proof that a game is in the RetroAchievements database.
- Originals move to `_couchchd_backup/` only after verified publication. Shared tracks stay in place while a live descriptor might still need them. Relative `.m3u` entries point to `.chd_discs/`; single discs keep their normal CHD names. Stop Recalbox EmulationStation before metadata changes; re-run Steam ROM Manager after paths change.
- The SQLite ledger (`couchchd_history.db`) tracks filesystem operations. Undo restores originals without overwriting conflicts and retains generated game images under `_couchchd_backup/.undo/`. Editing or replacing an output invalidates its backup-purge proof. XISO and trimmed 3DS outputs never authorize source removal.
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

Native release artifacts include the converter tools and notices; the source checkout does not contain ready-to-run Linux bundles. The build compiles `chdman` from [pinned MAME source](scripts/fetch_chdman.sh) and packages [DolphinTool](scripts/package_rvz.sh) for RVZ. Conversion tests use the frozen app on native x86-64 and aarch64 runners. These tests do not certify a particular GPU driver, console, emulator or game.

On GitHub, run **Native Linux release candidates** manually to build both architectures without publishing. Numeric preview tags (`vX.Y.Z-preview.N`) publish test prereleases after native build checks; other version tags stage drafts for maintainer review. See [bundle installation and checksums](packaging/INSTALL.txt).

CouchCHD is licensed under [GPL-3.0-only](LICENSE). MAME, Qt, SDL and other distributed dependencies retain their respective licenses. Product requirements and platform caveats are in [SPEC.md](SPEC.md).
