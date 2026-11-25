\# Product & Technical Specification: CouchCHD  
\*\*A Controller-First, Failsafe ROM Compression & Multi-Disc Management App\*\*

\---

\#\# 1\. Executive Summary & Vision  
\*\*CouchCHD\*\* is an open-source, gamepad-first utility designed to compress disc-based ROM collections into \`.chd\` (Compressed Hunks of Data) format, verify mathematical integrity, automatically organize multi-disc games into frontend-clean \`.m3u\` playlists, and reclaim storage directly from a living-room or handheld UI—without requiring a keyboard, mouse, or desktop terminal.

\#\#\# Core Design Principles  
1\. \*\*Zero Data Loss Above All:\*\* Atomic writes, two-stage verification, shared-track reference checks, and quarantine-by-default ensure no user library is ever corrupted or accidentally wiped.  
2\. \*\*100% Controller-First:\*\* Built from the ground up for D-Pad/Stick navigation (\`A\` Confirm, \`B\` Back, \`X\` Inspect/Options, \`Y\` Primary Action) in Steam Gaming Mode and on TV frontends.  
3\. \*\*Self-Contained & Immutable-OS Ready:\*\* Zero system package dependencies; ships with bundled static \`chdman\` binaries (\`x86\_64\` and \`aarch64\`).

\#\#\# Target Platforms  
1\. \*\*Bazzite / SteamOS (\`x86\_64\`):\*\* Steam Deck, ROG Ally, Legion Go, and HTPCs (compatible with EmuDeck & RetroDECK directory structures).  
2\. \*\*Recalbox (\`aarch64\`):\*\* Raspberry Pi 4 (8GB) and similar ARM64 SBCs (compatible with EmulationStation / Recalbox ROM folder conventions).  
3\. \*\*Desktop Linux / Windows (Secondary):\*\* Standard windowed mode for bulk library preparation on NAS/PC before syncing.

\---

\#\# 2\. Recommended Tech Stack  
\* \*\*Core Language:\*\* Python 3.11+  
\* \*\*Controller-First UI Framework:\*\*  
  \* \*Primary:\* \*\*PySide6 (Qt6 / QML)\*\* with SDL2 gamepad input routing (\`pygame\` / \`evdev\` fallback) for smooth 60fps 10-foot UI rendering on Wayland, X11, and KMS/DRM.  
  \* \*Fallback Headless/SSH Mode:\* CLI flag \`--headless \--auto\` for automated cron/systemd batch runs on Raspberry Pi or NAS boxes.  
\* \*\*Bundled Binaries (\`./bin/{arch}/\`):\*\*  
  \* \`chdman\` (\`\>= 0.256\` static \`x86\_64\` and \`aarch64\` builds from MAME tools).  
  \* \*(Optional Phase 2)\* \`dolphin-tool\` for GameCube/Wii \`.iso\` \-\> \`.rvz\` compression.

\---

\#\# 3\. System & Format Rules Engine

CouchCHD detects target systems automatically via folder aliases (supporting EmuDeck, RetroDECK, and Recalbox conventions) and validates file headers before conversion:

\| System \| Folder Aliases \| Source Formats \| \`chdman\` Mode \| Required Flags \|  
\| :--- \| :--- \| :--- \| :--- \| :--- \|  
\| \*\*PlayStation 1\*\* \| \`psx\`, \`ps1\` \| \`.cue\` (with \`.bin\`), \`.iso\` \| \`createcd\` \| Default (RA Hash Safe) \|  
\| \*\*Sega CD\*\* \| \`segacd\`, \`megacd\` \| \`.cue\` (with \`.bin\`) \| \`createcd\` \| Default (RA Hash Safe) \|  
\| \*\*Sega Saturn\*\* \| \`saturn\` \| \`.cue\` (with \`.bin\`) \| \`createcd\` \| Default (RA Hash Safe) \|  
\| \*\*Dreamcast\*\* \| \`dreamcast\` \| \`.gdi\`, \`.cue\` (with \`.bin\`) \| \`createcd\` \| Default (RA Hash Safe) \|  
\| \*\*PlayStation 2 (DVD)\*\* \| \`ps2\` \| \`.iso\` (\`\>= 700 MB\`) \| \`createdvd\` \| \`-hs 2048\` (RA Hash Safe) \|  
\| \*\*PlayStation 2 (CD)\*\* \| \`ps2\` \| \`.cue\` (with \`.bin\`) \| \`createcd\` \| Default (RA Hash Safe) \|  
\| \*\*PSP\*\* \| \`psp\` \| \`.iso\` \| \`createdvd\` \| \`-hs 2048\` (RA Hash Safe) \|

\* \*\*RetroAchievements Hash Guarantee:\*\* Hunk sizes are strictly locked to Redump/RetroAchievements standards (\`createcd\` default; \`createdvd \-hs 2048\`). Every verified \`.chd\` displays a \*\*"RetroAchievements Hash Safe"\*\* indicator.

Here **Default** means standard CD hunk geometry. On portable devices, `createcd` may use the lossless `cdzl,cdfl` codec pair to avoid LZMA trial compression. This does not relax checksum or track-layout verification.

**Additional verified formats:** The queue also supports PSP CSO (CHD remains the default), Dolphin RVZ for raw GameCube/Wii images, XISO for original Xbox full-disc dumps, trimmed read-only Nintendo 3DS cartridges, and single-ROM 7z archives for supported cartridge systems. See [accepted inputs and compatibility](README.md#formats-and-emulator-compatibility). XISO and trimming change the physical layout; both always retain the original. Neither a checksum nor successful conversion establishes emulator, save, or achievement compatibility.

\---

\#\# 4\. Multi-Disc Grouping & \`.m3u\` Automation  
\* \*\*Regex Detection:\*\* Matches standard No-Intro / Redump multi-disc tags:  
  \`^(?P\<title\>.+?)\\s\*\\((?:Disc\|Disk\|CD)\\s\*\\d+(?:\\s\*of\\s\*\\d+)?\\).\*\$\`  
\* \*\*Frontend-Clean Directory Structure:\*\*  
  1\. Create hidden directory \`\<system\_dir\>/.chd\_discs/\` (leading dot \`.\` hides sub-discs from EmulationStation-DE, Recalbox, and Steam ROM Manager scanners).  
  2\. Move all multi-disc \`.chd\` files for that title into \`.chd\_discs/\`.  
  3\. Generate \`\<system\_dir\>/\<Title\>.m3u\` using relative paths (e.g., \`.chd\_discs/Final Fantasy VII (USA) (Disc 1).chd\`) so saves and playlists remain portable across Bazzite and Recalbox.  
\* \*\*Metadata & Box Art Preservation:\*\* Check \`gamelist.xml\` and \`media/\` folders for existing \`\<Title\> (Disc 1)\` entries and clone/relink scraped box art and metadata to \`\<Title\>.m3u\` so scraped artwork is never orphaned.  
\* \*\*Single-Disc Rule:\*\* Never wrap single-disc games in \`.m3u\` playlists.

\---

\#\# 5\. Cloud & Network Ingestion Pipeline (Google Drive / NAS Pull-and-Compress)  
Designed for users who store uncompressed ROM archives in cloud storage (e.g., Google Drive) or a home NAS to avoid dumping giant \`.bin/.cue\` files onto limited handheld/SD storage:

1\. \*\*Optional \`rclone\` Remote Integration:\*\*  
   \* Auto-detect if \`rclone\` is configured on the system (e.g., \`gdrive:\`, \`nas:\`) or allow selecting a mounted cloud/SMB/NFS folder.  
2\. \*\*Streamlined "Pull \-\> Compress \-\> Verify \-\> Purge Temp" Loop:\*\*  
   * Ingest one remote game at a time into a private temporary workspace. Verify the converted output before publishing. Never change remote originals.
   * Budget scratch and destination space separately by filesystem; temporary inputs and output may need to coexist. Retaining a local backup does not reclaim its space.
3. **Reuse an existing compressed game:** register named mounted libraries or configured rclone sources. Cache last-seen inventories, distinguish offline entries, and pull a selected game or playlist pack without recompression. A match by name is not content identity. Never mirror deletions or overwrite a conflicting destination.
4. **Handheld destinations:** a controller-selectable firmware/player profile filters export formats and determines the supported folder layout. This is distinct from native execution; unsupported or unknown emulator features must not be implied by a device model.
5. **First-party Android Wi-Fi handoff:** a foreground sender offers only the selected platform and games. Discovery does not authorize reading. Both devices compare a session code before transfer over pinned TLS; there is no background listener, remote upload or mirror. Android asks for the platform folder, checks existing files before copying and reads back new files before declaring success. The companion does not convert or launch games.

\---

\#\# 6\. Data Safety, Redundancy & Failsafe Architecture (Mandatory)

\#\#\# 6.1 Pre-Flight Integrity Gate  
Before touching any files, the scanner validates:  
1\. \*\*Cue / GDI Track Resolution:\*\* Parse every \`FILE "..."\` line in \`.cue\` and track line in \`.gdi\`. Confirm all referenced \`.bin\`/\`.img\`/\`.raw\` files exist and are \`\> 0 bytes\`.  
2\. \*\*Windows-to-Linux Case-Sensitivity Fix:\*\* Perform case-insensitive lookup for track filenames referenced inside \`.cue\` sheets. If casing differs (e.g., \`TRACK01.BIN\` vs \`track01.bin\`), generate an in-memory/temp \`.cue.tmp\` for \`chdman\` without altering the user's original \`.cue\`.  
3. **Active transfer and archive guard:** skip partial or still-changing files. Inspect bounded single-disc ZIP packages in mixed local/mounted libraries. Stream a PSP ISO directly to CSO when selected; use private, capacity-checked storage for seekable inputs. Retain the outer ZIP in every cleanup policy. Report encrypted, nested, unsupported, or ambiguous layouts without automatically unpacking them.
4\. \*\*Single-Instance Lockfile (\`couchchd.lock\`):\*\* Prevent concurrent instances from scanning or modifying the same ROM directory.

\#\#\# 6.2 Atomic 4-Stage Conversion Pipeline  
1\. \*\*Isolated Temp Output:\*\* Write exclusively to \`.couchchd\_tmp/\<GameName\>.chd.part\`. Startup recovery purges orphaned \`.part\` files automatically on boot.  
2\. \*\*Stage 1 (Process Check):\*\* Confirm \`chdman\` exits with code \`0\` and output size \`\> 0\`.  
3\. \*\*Stage 2 (Mathematical Verification):\*\* Run \`chdman verify \-i \<GameName\>.chd.part\` to validate internal SHA-1/CRC32 hunk checksums.  
4\. \*\*Stage 3 (Sector/Track Count Match):\*\* Run \`chdman info \-i \<GameName\>.chd.part\` and assert track count and total sector frames match the source image.  
5\. \*\*Stage 4 (Atomic Commit):\*\* Execute \`os.replace()\` to move \`\<GameName\>.chd.part\` \-\> \`\<system\_dir\>/\<GameName\>.chd\`.

\#\#\# 6.3 Non-Destructive Cleanup ("Quarantine First" Policy)  
* **Quarantine first:** move eligible verified originals to the backup area. Keep machine verification separate from the player's play-test mark. Preview the exact backups and affected undo batches before irreversible reviewed cleanup; reject stale confirmation. The space planner must distinguish expected compression savings from free bytes on the library volume.
\* \*\*Shared-Track Reference Protection:\*\* Before moving or deleting any \`.bin\` file, cross-check all other \`.cue\`/\`.gdi\` files in the directory. If two \`.cue\` files share a \`.bin\` track (common in \`1G1R\` / merged sets), never remove the shared track until all referencing \`.cue\` files are converted.  
\* \*\*Transaction Ledger & One-Click Undo (\`couchchd\_history.db\`):\*\* Log every move/conversion in SQLite. Provide an \*\*"Undo Last Batch"\*\* button to restore quarantined files and unpack \`.m3u\` folders, plus a per-game \*\*"Extract Back to BIN/CUE"\*\* (\`chdman extractcd\`) option.

\---

\#\# 7\. Storage Exhaustion & Real-World Edge-Case Matrix

\#\#\# 7.1 Disk Full (\`ENOSPC\`) Prevention & Recovery  
\* \*\*Smallest-First Queue Sorting:\*\* When free space is tight (\`free\_space \< total\_source\_size \* 0.6\`) and immediate cleanup is active, sort games \*\*smallest-first\*\* so early conversions free up gigabytes of headroom for large PS2/Dreamcast games at the end of the queue.  
\* \*\*Pre-Job Space Reservation Ledger:\*\* Each worker reserves \`estimated\_chd\_size \+ 1 GB buffer\` before starting so parallel workers never collide at 100% disk usage. Dynamically scales workers down to \`1\` as free space narrows, and pauses cleanly with a controller prompt if headroom drops below \`2 GB\`.  
\* \*\*Hard \`ENOSPC\` Catch:\*\* Catch \`OSError(errno.ENOSPC)\`, immediately kill active \`chdman\` child processes, purge \`.couchchd\_tmp/\*.part\`, and pause without corrupting any files.

\#\#\# 7.2 Hardware & Emulation Edge Cases

\| Edge Case \| Automated CouchCHD Handling \|  
\| :--- \| :--- \|  
\| \*\*FAT32 / exFAT SD Cards\*\* \| Detect filesystem via \`statvfs\`. Block files \`\> 4 GB\` on FAT32 with clear warning; use copy-verify-unlink fallback if cross-device/exFAT atomic rename fails. \|  
\| \*\*Slow SMB/NFS Network Mounts\*\* \| Detect \`cifs\`/\`nfs\`/\`sshfs\` mounts and enable \*\*Local Scratch Staging\*\* (copy source to local \`/tmp\`, compress on multi-core CPU, verify, upload \`.chd\` back). \|  
\| \*\*Dual-Layer PS2 DVDs (\`\> 4.7 GB\`)\*\* \| Enforce \`createdvd \-hs 2048\` and verify \`chdman \>= 0.256\` to prevent layer-break corruption on games like \*God of War II\* and \*Gran Turismo 4\*. \|  
\| \*\*Unicode & Special Characters\*\* \| Pass argument lists (\`shell=False\`) exclusively; decode \`.cue\` sheets using fallback chain: \`utf-8-sig\` \-\> \`cp1252\` \-\> \`shift\_jis\`. \|  
\| \*\*Battery & Thermal Watchdog\*\* \| Pause queue automatically at \*\*15% battery\*\*; throttle workers to \`1\` if CPU temperature exceeds \*\*88°C\*\*. \|  
\| \*\*Steam Deck Sleep / Suspend\*\* \| Listen to \`systemd-logind\` \`PrepareForSleep\` D-Bus signal; pause dispatch on sleep, wait 3s after wake for microSD/USB remounts, and auto-resume. \|

\---

Use code with caution.

8\. Controller-First UX & UI Specification

Input Mapping (SDL2 Standard Gamepad)

• D-Pad / Left Stick: Navigate cards, system toggles, and game lists.  
• A (South): Confirm / Toggle item / Open modal.  
• B (East): Back / Pause or Cancel batch (with confirmation dialog).  
• X (West): Inspect game details, track list, or trigger Extract to BIN/CUE.  
• Y (North): primary context action (for example start a batch, open the catalog, or preview reviewed cleanup). A primary press alone must not purge backups.
• LB / RB: switch top-level tabs (Dashboard | Systems | Multi-Disc & M3U | NAS / Cloud | History & Backups).
• Start: Open Settings (Thermal Profile, Quarantine Policy, Add to Steam).

UI Polish & Feedback

• Target Resolutions: Native 1280x800 (Steam Deck) and 1920x1080 (TV/Recalbox) with large, readable 10-foot typography (\>= 18pt body).  
• Subtle Sound Effects & Haptics: Optional low-volume UI ticks, completion chime, and SDL2 controller rumble pulse when a batch completes or requires user attention (e.g., low storage warning).  
• Dynamic Button Prompts: Bottom legend bar auto-switches glyphs between Xbox/Steam Deck (A/B/X/Y), PlayStation (Cross/Circle/Square/Triangle), and Nintendo (B/A/Y/X) based on detected SDL2 controller type.  
• One-Click Steam Integration: Button in Settings to register CouchCHD as a Non-Steam Game in Gaming Mode with custom grid/hero artwork.

9\. Platform-Specific Profiles & Packaging

Bazzite / SteamOS (x86\_64)

• Distribution: Self-contained AppImage (and optional Flatpak) bundling Python, PySide6, and static x86\_64 chdman.  
• Auto-Detected Paths: \~/Emulation/roms, /run/media/mmcblk0p1/Emulation/roms, \~/RetroDECK/roms.  
• Thermal Presets: Handheld Silent (50% cores, default on battery), Balanced (75% cores), Docked Max (100% cores).

Recalbox on Raspberry Pi 4 (aarch64)

• Distribution: Portable aarch64 bundle / standalone launcher script compatible with Recalbox (/recalbox/share/roms).  
• Smart Hardware Guardrails: Default worker cap of 2 threads to prevent SD card / USB 3.0 I/O saturation; automatically flags ps2 as "Not Recommended on Pi 4" while enabling psx, segacd, saturn, dreamcast, and psp.

10\. Repository Layout & Development Milestones

couchchd/  
├── bin/  
│   ├── x86\_64/chdman          \# Static MAME chdman \>= 0.256  
│   └── aarch64/chdman         \# Static ARM64 chdman \>= 0.256  
├── src/  
│   ├── core/  
│   │   ├── scanner.py         \# Pre-flight validation, .cue/.gdi parser, shared-track check  
│   │   ├── converter.py       \# Atomic 4-stage chdman runner, verify, ENOSPC guard  
│   │   ├── multidisc.py       \# Regex grouping, .chd\_discs/ & .m3u builder, gamelist.xml linker  
│   │   ├── cloud\_ingest.py    \# rclone / NAS pull-compress-verify scratch pipeline  
│   │   └── ledger.py          \# SQLite audit log, Quarantine manager, Undo & Extract  
│   ├── platform/  
│   │   ├── hardware.py        \# Battery, thermal, sleep/wake D-Bus, FAT32/SMB mount detection  
│   │   └── profiles.py        \# Bazzite/SteamOS & Recalbox Pi 4 path & worker presets  
│   └── ui/  
│       ├── gamepad.py         \# SDL2 controller router & dynamic glyph detector  
│       └── qml/               \# 10-foot PySide6/QML views & HUD components  
├── tests/  
│   ├── fixtures/              \# Synthetic multi-track .bin/.cue, case-mismatch, shared-track tests  
│   └── test\_safety.py         \# ENOSPC simulation, verify failure rollback, undo ledger tests  
├── LICENSE                    \# GPLv3  
└── SPEC.md                    \# This document

Phased Build Plan

• Milestone 1 (Core Safety Engine & CLI): Implement scanner.py, converter.py, multidisc.py, ledger.py, and full pytest suite covering shared tracks, case mismatches, corrupted inputs, and simulated disk-full rollbacks.  
• Milestone 2 (Hardware Watchdogs & Cloud Ingest): Add hardware.py (battery/thermal/sleep/mount detection) and cloud\_ingest.py (rclone single-game scratch streaming).  
• Milestone 3 (Controller-First QML UI): Build the 10-foot PySide6 interface, SDL2 gamepad router, dynamic button glyphs, and live space-saved dashboard.  
• Milestone 4 (Packaging & Community Release): Build x86\_64 AppImage (Bazzite/SteamOS) \+ aarch64 Recalbox bundle, test on Steam Deck/Bazzite and Pi 4, and publish v1.0 open-source release.

\#\# 11\. Engineering Notes & Platform Pitfall Research (Dev Reference)

\#\#\# 11.1 Steam Gaming Mode (\`gamescope\`) Controller & Focus Traps  
\* \*\*The \`gamescope\` Window Trap:\*\* On Bazzite and SteamOS in Gaming Mode, the \`gamescope\` compositor forces the primary window fullscreen, \*\*but\*\* if PySide6 opens a secondary modal dialog (\`QDialog\` or native file picker), \`gamescope\` can lose focus or render the dialog off-screen/uncontrollable by gamepad.  
  \* \*\*Rule for UI Agents:\*\* Never use native OS file dialogs or separate \`QDialog\` windows. Render all modals, folder browsers, and alerts as \*\*in-scene QML overlays\*\* inside a single root \`ApplicationWindow\`.  
\* \*\*Steam Input Double-Input Prevention:\*\* Steam Input translates controller buttons into keyboard/mouse events \*while\* SDL2 simultaneously reads raw gamepad HID events—causing every D-Pad press to move two menu items at once.  
  \* \*\*Rule for Input Agents:\*\* Set \`SDL\_GAMECONTROLLER\_IGNORE\_DEVICES\` or consume raw SDL2 events exclusively while suppressing synthetic Qt arrow-key events when a gamepad is active, and ship a recommended community Steam Input template ("Gamepad with Joystick Trackpad").

\#\#\# 11.2 Recalbox OverlayFS & Read-Only Root Gotchas  
\* \*\*Recalbox Partition Layout:\*\* On Raspberry Pi 4, Recalbox mounts \`/\` (root) as a \*\*read-only SquashFS/OverlayFS\*\* that resets on reboot, while \`/recalbox/share/\` is the persistent user data partition (often formatted as \`exFAT\` or \`ext4\`).  
  \* \*\*Rule for Packaging Agents:\*\* Never store CouchCHD binaries, SQLite databases (\`couchchd\_history.db\`), or temp scratch directories in \`/opt\` or \`/usr\`. Store the app bundle, config, and SQLite ledger inside \`/recalbox/share/system/couchchd/\` (or relative to the ROMs drive) so updates and reboots never wipe the Undo history.  
\* \*\*Recalbox \`gamelist.xml\` Caching:\*\* Recalbox's EmulationStation fork keeps \`gamelist.xml\` in RAM while running and \*\*overwrites it on shutdown/reboot\*\*.  
  \* \*\*Rule for Metadata Agents:\*\* Warn users (or trigger Recalbox's HTTP API \`POST /reloadgames\` / stop \`emulationstation\` cleanly) before modifying \`gamelist.xml\`, otherwise Recalbox will clobber newly linked \`.m3u\` metadata when the Pi reboots.

\#\#\# 11.3 Subtle Disc Image & Frontend Quirks  
\* \*\*Dreamcast \`.gdi\` Track Path Quirk:\*\* Unlike \`.cue\` files where filenames are quoted (\`FILE "Track 01.bin" BINARY\`), \`.gdi\` files use space-delimited columns (\`1 0 4 2352 track01.bin 0\`). Filenames containing spaces in \`.gdi\` sheets are either quoted or parsed by counting columns from the left (columns 1–4) and right (last column \= offset), treating everything in between as the filename. Ensure the \`.gdi\` parser handles filenames with spaces\!  
\* \*\*PSP \`.chd\` Compatibility Floor:\*\* \`chdman createdvd \-hs 2048\` works in \*\*PPSSPP v1.17+\*\*, but older PPSSPP builds (sometimes bundled on older Recalbox releases) only support \`.iso\` or \`.cso\`.  
  \* \*\*Rule for Profile Agents:\*\* On Recalbox profiles, check the installed PPSSPP version (\`PPSSPPSDL \--version\`) before enabling \`psp\` compression by default; if \`\< 1.17\`, keep \`psp\` unchecked with a tooltip explaining why.  
\* \*\*Steam ROM Manager (SRM) \`.m3u\` Parser Behavior:\*\* On Bazzite/EmuDeck, Steam ROM Manager parses \`.m3u\` files by default. If a user previously parsed \`(Disc 1).cue\` into Steam, converting to \`.m3u\` requires reminding the user to re-run Steam ROM Manager (or providing a shortcut button to launch SRM) so Steam shortcuts point to the new \`.m3u\` entry instead of a dead \`.cue\` path.

\#\#\# 11.4 Open-Source Trust & CI/CD Checksums  
\* \*\*Reproducible Binary Provenance:\*\* Because CouchCHD bundles static \`chdman\` binaries (\`x86\_64\` and \`aarch64\`), include a \`scripts/fetch\_chdman.sh\` build script in the repo that compiles \`chdman\` directly from official MAME GitHub release tags (\`mamedev/mame\`) via GitHub Actions CI and publishes SHA-256 checksums. Open-source users are understandably cautious about precompiled binaries touching their ROMs—verifiable CI builds establish immediate community trust.  
