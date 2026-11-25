# Do you need CouchCHD if you already use EmuDeck?

**Usually not for a one-off conversion.** If you have an independent backup, want to replace the originals, and your game is covered below, start with **EmuDeck → Compressor**. It already knows where you installed your games and can process whole systems. If your library is already in formats that play well, install neither compressor for that library. CouchCHD earns its place when you want to review a large batch with a controller, verify each CHD before replacing anything, and be able to undo a mistake.

**Evidence boundary, 2026-11-25:** This compares [EmuDeck Desktop v2.7.1’s SteamOS button][button], the [public SteamOS backend at `f8d49a3`][script], and CouchCHD’s [published preview.9 with Linux binaries][release]. The button launches `~/.config/EmuDeck/backend/tools/chdconv/chddeck.sh` in Konsole; the backend is versioned separately from the desktop app. These are source and documentation findings, not a paired Steam Deck benchmark or proof that every output boots. The [current manual][manual] advertises “up to 70%” savings; that is not a prediction for your collection.

## What the EmuDeck button actually does

The [Application 101 wiki][wiki] lists CHD for several CD systems, RVZ for GameCube/Wii and CSO for PSP. The implementation is broader. It offers **Bulk Compression**, **Bulk Decompression**, or **Select a ROM**. Bulk finds eligible files recursively in known system folders, then asks which folders to process; single-file mode asks for an operation and a file. These are interactive choices, not a universal “best format” setting. [Folder and extension lists][formats] · [Bulk routing][bulk] · [Single-file and decompression routing][single]

| Bulk input | EmuDeck action | What matters for playback or preservation |
|---|---|---|
| `.cue` + tracks, `.gdi` + tracks, or `.iso` in the CHD folder list | `chdman5 createcd` for CUE/GDI; `createdvd -c zstd` for ISO | Includes PSX, PS2, Dreamcast, Saturn, Sega/Mega CD, PC Engine CD, PC-FX, Neo Geo CD, 3DO, Amiga and arcade aliases. A bare BIN is not a disc descriptor. ISO routing is by suffix here, not proof of DVD media or emulator support. |
| `.iso` / `.gcm` under `gc`, `wii`, `primehacks` | Dolphin/PrimeHack Flatpak’s `dolphin-tool` → `.rvz`, Zstandard level 5, 128 KiB blocks | Bulk discovery checks for a matching Flatpak. A format that Dolphin can play is not necessarily an input this scanner picks up. |
| `.iso` under `psp` | **Either** `.cso` (`ciso 9`) **or** `.chd` (`createdvd --hunksize 2048`) | Contrary to the wiki shorthand, the [bulk dialog’s OK is CHD][pspbulk]; the [single PSP dialog’s OK is CSO][pspone]. [PPSSPP][psp] supports CHD from 1.17; CSO also serves compatible real PSP custom firmware. |
| `.iso` under `xbox` | `extract-xiso -r` → `.xiso.iso`; bulk excludes already-named `.xiso.iso` | **Original Xbox**, not Xbox 360. [xemu][xemu] wants the game partition; a full disc dump also contains a video partition. A playable rebuild is not a bit-identical backup. |
| `.3ds` under `n3ds` | `3dstool` trims in place, then renames to `…(Trimmed).3ds` | Trimming is not lossless container compression. [Azahar][azahar] distinguishes `.cci` from `.3ds`; it also offers [reversible `.zcci` compression][zcci] for supported backups. Don’t assume a trimmed extension accepted by a parser will boot. |
| Selected cartridge extensions under whitelisted systems | One input → one `.7z`, `7z a -mx=9`; bulk checks for `7za`, conversion invokes `7z` | Examples: NES/Famicom, GB/GBC/GBA, SNES/SFC, N64, Lynx, Neo Geo Pocket, WonderSwan and Mega Drive. Not a generic “archive my ROM directory” action. Compare the [exact lists][formats] with the [installed parser/core][parsers]. |

**Originals and verification:** The [actual helpers][helpers] warn about backups and normally delete or replace the input after the converter reports success; CD cleanup searches sibling filenames in the descriptor. No keep-originals toggle, separate readback/`chdman verify`, or transaction undo appears in this script. The tool does log the run. Its reverse choices are specifically PSP CHD/CSO → ISO and GC/Wii-family RVZ → ISO, **not** undo for all formats. The wiki’s [checksums, Redump comparisons and emulator verification instructions][verify] are separate user actions. Don’t mistake a valid container for a good original dump.

## The library is more than its ROM files

- **Find the intended library.** EmuDeck reads `romsPath` from `~/.config/EmuDeck/settings.sh` via its [backend variables][vars] and [settings template][settings]. `~/Emulation/roms` is the internal default, not a requirement. The [file-management guide][files] documents SD/custom installs and linking subfolders on other volumes. After migration there may be *two* real trees: [EmuDeck keeps the old one][migration]. Preview.9 [probes common roots][roots] and accepts `--root`; its [scanner skips nested symlink directories][scanner]. A scan returning no games does not prove the SD library is empty.
- **Steam shortcuts are not live file references that repair themselves.** After `.cue` becomes `.chd`, re-parse, preview and save in Steam ROM Manager (SRM). The script [offers to open SRM only in `uiMode=zenity`][handoff]. ES-DE users need not create individual Steam shortcuts. A format appearing in an SRM glob proves discovery, not that the emulator accepts its contents: for example, compare the [Azahar parser][azparser] with [Azahar’s own policy][azahar].
- **One output per disc is not one entry per game.** EmuDeck’s [ES-DE guide][esde] describes `Title.m3u/Title.m3u` as an ES-DE-only convention, not compatible with SRM; [SRM’s guide][srm] uses an M3U plus exclusions for individual discs. EmuDeck also has a [separate playlist helper][emuplaylist]; it isn’t a step in the compressor. Keep disc order and relative paths. The guides exclude **PCSX2 and Xenia** from M3U workflows; the [shipped PCSX2 parser][pcsx2] accepts CHD but not M3U. Keep directly launchable discs for those emulators.
- **Preserve save identity before changing launch names.** For example, [Beetle PSX HW][memcards] defaults to memory cards named after the loaded content: switching `Game (Disc 1).cue` to `Game.m3u` can select a different card. Back up saves; launch with the new entry and load an existing in-game save before purging. EmuDeck [CloudBackup/CloudSync][cloud] concerns *saved games*, not disc archives or undo. CouchCHD’s [read-only ROM ingestion][ingest] from a mounted library or configured rclone remote is a different job. Neither replaces an independent dump backup.
- **Achievements:** EmuDeck [configures emulator logins][achievements]; RetroAchievements uses [system-specific hashes][hashes] (e.g. the PlayStation executable and its name), not the compressed file’s checksum. CouchCHD’s verification and hunk policy do not certify that a particular revision is linked, or that the chosen core recognizes it. Check identification in the emulator before deleting originals.

## Where CouchCHD earns the extra install

**Shipped in preview.9:** [Six CHD system families][scanner]: PS1 and PS2 (ISO or supported CUE), Dreamcast (GDI/CUE), Saturn and Sega CD (CUE), PSP (ISO). The [converter][verifychd] requires affirmative raw and overall CHD SHA-1 verification and checks source sector/track geometry before publication. [Gaming Mode, history and cleanup][couch] give you per-disc progress, resource limits on detected Deck/Pi devices, cancellation that retains completed discs, default quarantine and conflict-aware undo. Quarantine on the same drive **does not free the originals’ space until purge**; undo stops being possible after purge. [Multi-disc grouping][playlist] works on journal-verified sibling CHDs, not arbitrary libraries. Preview.9 lacks an emulator-specific grouping gate; do **not** rely on automatic grouping of a PCSX2 set. Its adjacent `gamelist.xml`/`media` handling is not a general migration of [EmuDeck ES-DE’s separate metadata and artwork directories][esde].

**Development, not a release claim:** Work on explicit EmuDeck discovery and formats beyond CHD (RVZ, CSO, cartridge archives, 3DS and original Xbox) is not evidence that preview.9 supports them. If one of those formats is the reason you would install CouchCHD, **use EmuDeck or the emulator’s native tool now**. Do not migrate a working EmuDeck library merely for a format count.

**Priority for CouchCHD:**

1. **Identify the active root before writing.** Read only literal install settings as data, show the selected volume, and report skipped/unmounted/linked roots rather than following writes across disks. Acceptance: a relocated SD install and stale SSD copy cannot silently select the wrong library.
2. **Protect the next launch.** Preview old → new launch paths, keep separate discs for non-playlist emulators, preserve existing playlists/sidecars, and hand off to SRM or ES-DE rather than rewriting Steam’s database. Acceptance: one expected frontend entry, correct disc swap, old save loads, and undo restores the old path.
3. **Make each new format justify cleanup.** Require a supported reader and a documented verification target: decoded-byte equality for lossless formats; verified game data **and retained original** for trimmed/rebuilt images. Check the actual parser *and* launch a legally redistributable fixture through the target emulator. Publish Deck measurements of elapsed time, peak free-space requirement and net bytes reclaimed after cleanup before claiming an advantage.

[button]: https://github.com/EmuDeck/emudeck-electron/blob/a6848a47610143facefd672350e19595f70007e5/src/renderer/pages/CHDToolPage.jsx#L25-L36
[script]: https://github.com/dragoonDorise/EmuDeck/blob/f8d49a38532636cf2e5d546b49dcde87ef731866/tools/chdconv/chddeck.sh
[formats]: https://github.com/dragoonDorise/EmuDeck/blob/f8d49a38532636cf2e5d546b49dcde87ef731866/tools/chdconv/chddeck.sh#L9-L72
[helpers]: https://github.com/dragoonDorise/EmuDeck/blob/f8d49a38532636cf2e5d546b49dcde87ef731866/tools/chdconv/chddeck.sh#L74-L270
[bulk]: https://github.com/dragoonDorise/EmuDeck/blob/f8d49a38532636cf2e5d546b49dcde87ef731866/tools/chdconv/chddeck.sh#L304-L502
[pspbulk]: https://github.com/dragoonDorise/EmuDeck/blob/f8d49a38532636cf2e5d546b49dcde87ef731866/tools/chdconv/chddeck.sh#L442-L469
[pspone]: https://github.com/dragoonDorise/EmuDeck/blob/f8d49a38532636cf2e5d546b49dcde87ef731866/tools/chdconv/chddeck.sh#L602-L622
[single]: https://github.com/dragoonDorise/EmuDeck/blob/f8d49a38532636cf2e5d546b49dcde87ef731866/tools/chdconv/chddeck.sh#L505-L744
[handoff]: https://github.com/dragoonDorise/EmuDeck/blob/f8d49a38532636cf2e5d546b49dcde87ef731866/tools/chdconv/chddeck.sh#L749-L777
[vars]: https://github.com/dragoonDorise/EmuDeck/blob/f8d49a38532636cf2e5d546b49dcde87ef731866/vars.sh#L1-L4
[settings]: https://github.com/dragoonDorise/EmuDeck/blob/f8d49a38532636cf2e5d546b49dcde87ef731866/settings.sh#L79-L85
[parsers]: https://github.com/dragoonDorise/EmuDeck/blob/f8d49a38532636cf2e5d546b49dcde87ef731866/configs/steam-rom-manager/userData/userConfigurations.json
[pcsx2]: https://github.com/dragoonDorise/EmuDeck/blob/f8d49a38532636cf2e5d546b49dcde87ef731866/configs/steam-rom-manager/userData/parsers/optional/sony_ps2_pcsx2.json
[azparser]: https://github.com/dragoonDorise/EmuDeck/blob/f8d49a38532636cf2e5d546b49dcde87ef731866/configs/steam-rom-manager/userData/parsers/optional/nintendo_3ds_azahar.json
[emuplaylist]: https://github.com/dragoonDorise/EmuDeck/blob/f8d49a38532636cf2e5d546b49dcde87ef731866/functions/ToolScripts/emuDeckM3U.sh
[manual]: https://manual.emudeck.com/using-app/7_compressor/
[wiki]: https://emudeck.github.io/emudeck-application/steamos/emudeck-application-101/#emudeck-compressor
[migration]: https://emudeck.github.io/emudeck-application/steamos/emudeck-application-101/#migrate-installation
[files]: https://emudeck.github.io/file-management/steamos/file-management/
[verify]: https://emudeck.github.io/file-management/steamos/file-management/#how-to-verify-roms
[srm]: https://emudeck.github.io/tools/steamos/steam-rom-manager/#how-to-manage-roms-with-multiple-discs
[esde]: https://emudeck.github.io/tools/steamos/es-de/
[cloud]: https://manual.emudeck.com/using-app/10_cloud_saves/
[achievements]: https://manual.emudeck.com/using-app/6_retro-achievements/
[hashes]: https://docs.retroachievements.org/developer-docs/game-identification.html
[memcards]: https://github.com/libretro/docs/blob/e0280f629ca0158f8861d34c1f50e6b5b20504e3/docs/library/beetle_psx_hw.md#L291-L307
[xemu]: https://xemu.app/docs/disc-images/#about-redump-isos
[psp]: https://www.ppsspp.org/docs/getting-started/dumping-games/#compressed-iso-formats
[azahar]: https://azahar-emu.org/blog/game-loading-changes/
[zcci]: https://azahar-emu.org/blog/compressed-backups/
[release]: https://github.com/vip3rousmango/couchCHD/releases/tag/v0.1.0-preview.9
[couch]: https://github.com/vip3rousmango/couchCHD/blob/dc2f41c7230bb63ffb63c55a166e192cfbf6293b/README.md
[roots]: https://github.com/vip3rousmango/couchCHD/blob/dc2f41c7230bb63ffb63c55a166e192cfbf6293b/src/couchchd/profiles.py#L45-L67
[scanner]: https://github.com/vip3rousmango/couchCHD/blob/dc2f41c7230bb63ffb63c55a166e192cfbf6293b/src/couchchd/scanner.py
[verifychd]: https://github.com/vip3rousmango/couchCHD/blob/dc2f41c7230bb63ffb63c55a166e192cfbf6293b/src/couchchd/converter.py#L185-L216
[playlist]: https://github.com/vip3rousmango/couchCHD/blob/dc2f41c7230bb63ffb63c55a166e192cfbf6293b/src/couchchd/multidisc.py#L167-L250
[ingest]: https://github.com/vip3rousmango/couchCHD/blob/dc2f41c7230bb63ffb63c55a166e192cfbf6293b/src/couchchd/cloud_ingest.py
