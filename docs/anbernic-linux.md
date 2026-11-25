# Which library should I give my ANBERNIC Linux handheld?

**Use the firmware already on your handheld.** Prepare a copy on the library computer (for example your Bazzite PC, Deck or Pi); the handheld only needs to **play** it. Find a game that already appears in the handheld's menu and use the same games card, system folder and emulator for the first trial. If you can't identify those three things, keep the original format. A smaller file that disappears from the menu is not an upgrade.

**Evidence boundary — 2026-11-25.** The rows below are manufacturer specifications, actual release files and emulator documentation, **not hardware test results or a list of implemented picker presets**. The RG405M host-side handoff is the existing point of reference; it does not validate another firmware, core, BIOS or game. No Linux handheld playback, full-speed claim, native CouchCHD install or firmware migration is certified by this guide. Nothing here requires flashing a card, unlocking a bootloader, rooting Android or changing eMMC. Links to firmware identify it; they are not instructions to install it.

## Identify the machine first

Look on the back label and in the boot screen's **About / System Information**. Photograph both if the suffix is unclear. On dual-system models, identify **what you actually booted**, not what the box says it can run. For example, copying to a ROCKNIX `psx` folder is useful to ROCKNIX, not automatically to an Android emulator on the same machine.

| Family and examples | Hardware / firmware evidence | What to try first |
|---|---|---|
| **OpenDingux — RG350/P/M, RG280V/M, RG300X** | Adam groups [these Ingenic JZ4770 devices][adam]. This is **MIPS**, not a small ARM PC: [RG280V kernel board file][280dt] includes a [shared board description][odram] with two 256 MiB RAM ranges. [Libretro distinguishes original OpenDingux and Beta builds][odretro]. Adam 2.1 is an archived OpenDingux Beta software image, not the stock menu. Don't equate RG300 with RG300**X**. | One cartridge first; Adam also has named PS1 and Sega CD routes below. For stock or an unknown OPK (emulator package), try the owner's existing working file/selector; don't guess an Adam layout or a modern CHD decoder. |
| **RK3326 — RG351P/M/V/MP** | [ROCKNIX's P/M/V table][351] gives quad Cortex-A35, 1 GB DDR3L; V has a different screen, P lacks the M/V built-in Wi-Fi in that table. [AmberELEC][amber] explicitly covers P/M/V/**MP**. [ArkOS][ark] shipped separate P/M vs MP images; its upstream P/M image was final in 2021. The newer [dArkOS][dark] specifically names the MP among its RK3326 targets. | Cartridge or PS1 before PSP/Dreamcast. Use the selected firmware's folders, not the chip name. Don't treat the ROCKNIX P/M/V page as evidence for the MP or interchange any images. |
| **RK3566 — RG353V/VS, P/PS, M; RG ARC-D/S; RG503** | Quad Cortex-A55. [RG353 hardware][353]: V/P/M have 2 GB + 32 GB eMMC, VS/PS 1 GB and no eMMC; [ANBERNIC's V/VS comparison][vv] distinguishes Android/Linux from Linux only. [ARC-D/S][arc]: D has 2 GB + 32 GB eMMC, S 1 GB and no eMMC; [stock ARC firmware][stock] distinguishes dual D from single S. [RG503][503] is a 1 GB Linux handheld, **not RG505**. ROCKNIX warns that legacy eMMC bootloaders on the 353 and ARC models are unsupported. [KNULLI Scarab][scarab] publishes an **ARC-S** asset, not an ARC-D claim. | Use a known-good Linux boot/card as it is. PS1 first; test Sega CD/Dreamcast separately. A six-button ARC does not guarantee Saturn performance or disc swapping. No bootloader changes to use a library export. |
| **Original RG35XX** | The [manufacturer's comparison][xxcompare] lists quad Cortex-A9, **256 MB DDR3**, and Linux/Garlic on the original, versus Cortex-A53 on the 2024/Plus/H. [KNULLI explicitly does **not** support the original][orig]. [Black-Seraph's original GarlicOS][garlic1] and [MinUI][minui] are distinct interfaces. | Stock/Garlic: copy next to an existing game of the same system, verify the selected core. No H700 layout or universal archive assumption. MinUI uses the folder tags described below. |
| **Allwinner H700 — XX and related models** | [RG35XX Plus, H and 2024][xxcompare]: quad Cortex-A53, 1 GB LPDDR4; 2024 lacks the Plus's Wi-Fi/Bluetooth. [KNULLI device table][knmodels] and [Scarab release assets][scarab] cover RG28XX, RG34XX, RG35XX 2024/Plus/H/SP, RG40XX H/V and RG CubeXX; the release also names RG34XX SP and RG35XX Pro. [MustardOS 2606.0 images][mustard] include those model variants, RG SP and RG VITA Pro (the last is **not H700**). [ROCKNIX's device list][rnmodels] is separate; its [November release][rnrelease] even has distinct H700 DDR3/DDR4 artifacts. [RG34XXSP][34sp] and [RG SP][newsp] have their own product sheets. | First read the boot screen: **stock, muOS, KNULLI, ROCKNIX, Garlic or MinUI are not interchangeable destinations**. Cube**XX** is not the Android RG Cube; RG35XX **Pro**, **Plus** and the original are not aliases. |
| **Other Linux or dual-system branches** | [RG552][552spec]: RK3399, 4 GB LPDDR4, Android + Linux; both [ROCKNIX][552] and [AmberELEC][amber] document this model. [RG VITA Pro][vita]: RK3576, 4 GB, Android 14/Linux; base VITA is T618/Android 12. [RG DS][ds]: RK3568, 3 GB, Android 14/Linux; [DS Plus][dsplus]: RK3568, 1 GB, Linux. [RG 35][rg35] (no XX) is RK3326, 1 GB, Linux 64-bit. [RG 55G1][55g1] has a separate ROCKNIX port for Qualcomm G1 Gen 2, **not** evidence stock Android uses any ROCKNIX layout. | For an already-running documented CFW use its row below; otherwise use a manual stock destination. The [RG DS Linux 1.1 notes][dsnotes] name an NDS emulator update and a PSP Vulkan backend, **not** a PPSSPP version, ROM path or CHD promise. Don't infer Vita emulation from a product name. |
| **RG Nano** | ANBERNIC's [stock update notes][stock] distinguish the gmenu2x/RetroArch build. There is also a distinct [DrUm78 FunKey-OS Nano release][nano]; its [PS1 launcher][nanops] invokes PCSX ReARMed through **picoarch**, not the Adam PCSX4All OPK. | Treat as a small playback target. Start with a working cartridge file. Neither that launcher nor a chipset establishes that stock will list a `.chd`, `.m3u` or `.7z`; no automatic disc/archive route on that evidence. |

## Then choose the games folder for the firmware

A **games root** is the directory whose children are system folders. It may be the *top of a partition*, not a folder literally named `roms`. Paths below are examples on an existing installation. Preserve capitalization on the destination; never create a second `roms/roms` tree or copy games into `boot`, `BIOS`, `saves` or a system partition. Do not accept a computer's offer to initialize, repair or format an unreadable card.

| Installed firmware | Working library convention and readers | Limits that affect an export |
|---|---|---|
| **Adam Image 2.1 / SimpleMenu** | External card: `roms/PS` → **PCSX4All** (`.chd`, `.cue`, `.pbp` among others); `roms/SEGACD` → `.chd`/`.cue`; `roms/FC` → FCEUmm/QuickNES including `.nes`, `.zip`, `.7z`. These are literal [v2.1 launcher definitions][adames]. The [image's content guide][adamrom] lists `roms/SFC`, `MD`, `GB`, `GBC`, `GBA` under the same `roms` root, with `.7z` for these cartridge systems. | No `.m3u` in the shipped PS/SEGACD filters. Keep directly launchable discs. PCSX4All on this image requires the owner's `bios/SCPH1001.BIN`; the guide explicitly says zipped CUE+BIN does not work. A present `.chd` filter is not proof of every CHD codec. |
| **ArkOS / dArkOS** | For ArkOS, the mounted **EASYROMS partition itself** contains `psx`, `psp`, `gba`, etc.; use the **active** games card in a two-card setup. The [ArkOS][arksys] and [dArkOS][darksys] tables both list `psx` PCSX ReARMed + CHD/M3U and `psp` standalone PPSSPP + ISO/CHD/CSO/PBP. ArkOS also lists `dreamcast` Flycast + CHD/M3U, `segacd` Genesis Plus GX + CHD and `gba` mGBA + `.7z`. | [ArkOS is retired][ark]. Prefer **CSO** for PSP. Try CHD only if the **installed** menu lists it and its PPSSPP is 1.17 or newer; an old P/M card may match neither condition. ArkOS `segacd` does not list M3U; `dreamcast` explicitly strikes out `.7z`. Its wiki also puts saves beside ROMs: never overwrite a populated title to test a new format. |
| **AmberELEC (formerly 351ELEC)** | [GAMES partition / `/roms` on device][amberrom]; use the existing system directory. Its [PSP page][amberpsp] lists PPSSPPSDL standalone (default) and libretro PPSSPP, `/roms/psp`, and **`.iso`, `.cso`, `.pbp`**. | This is a useful counterexample to universal PSP CHD. An upstream emulator may support CHD and still not see the file through this frontend. [AmberELEC's systems index][ambersys] describes the development branch, not every installed release. |
| **ROCKNIX** | Use [the active games store][rnrom] (e.g. second SD `roms` or the existing network games share). [`roms/psx`][rnps]: CHD/M3U; PCSX ReARMed and its 32-bit variant listed for RK3326/RK3566. [`roms/psp`][rnpsp]: ISO/CSO/CHD, PPSSPP standalone/libretro. [`roms/gba`][rngba]: `.gba`, `.zip`, `.7z`, default mGBA. | Select the core actually installed. A network share may already be the games root. Do not turn on remote services, change passwords or rearrange an SD just to match an example. |
| **KNULLI / EmulationStation** | Only **one** active `/userdata`: SD1 `SHARE` **or** the active second card; games under its **`roms/`**, with precreated case-sensitive folders. [Storage rules][knstore]. [Scarab's shipped system definitions][knes] include `psx` (PCSX ReARMed if built; CHD/M3U), `psp` (PPSSPP; ISO/CSO/CHD), `dreamcast` (Flycast; CHD/M3U), `segacd` (Genesis Plus GX/PicoDrive; CHD/M3U), `gba`, `gb`, `nes`, `megadrive` (single-cartridge `.7z` listed). | Options are conditional on build packages. **Saturn:** the same manifest lists `.m3u` for the system but marks it **incompatible with libretro YabaSanshiro**. Use separate discs for that core. Inspect the actual system folder's `_info.txt` and the menu's selected emulator before exporting. KNULLI's storage docs offer network/USB access; an unreadable ext4 card on your computer is not an empty card. |
| **MustardOS / muOS 2606.0** | [`ROMS` on SD1, SD2 or supported USB][musys]. Subfolders can be named by the owner; matching aliases auto-assign, or highlight a folder/game → **X → Core** and assign just that file, directory or tree. [Core assignment][muassign]. Current documented defaults: [PS1 → PCSX ReARMed][mups], [PSP → standalone PPSSPP][mupsp], [GBA → mGBA][mugba]. The [first steps guide][mufirst] documents hiding files that are referenced by a neighboring M3U/CUE/GDI. | Choose an existing assigned folder; **do not rename it** to look like ArkOS. Specific game assignments override the folder. Hiding a playlist's children does not prove its reader supports disc swapping. The PSP page gives no PPSSPP version: prefer CSO for the first compressed trial. Older muOS menus can differ. |
| **MinUI** | Copy into `Roms` with **the existing uppercase parenthesized emulator tag**: e.g. `Roms/Sony PlayStation (PS)`; the display name may vary. The [author's shipped readme][minui] documents single CHD, CUE/BIN, official PBP; a multi-disc game uses a game-named subfolder containing a matching `.m3u` listing relative CUEs, one per line. | Don't manufacture a plain lowercase `psx` directory. Use the documented CUE example for the first swap trial. Multi-disc PBP larger than 2 GB is explicitly unsupported. Don't infer arbitrary 7z or a modern CHD decoder. |
| **Stock Linux / GarlicOS / unknown community builds** | Start with the **actual folder for one already visible game** on that card and test through the same menu (standalone vs RetroArch can mean a different reader). Check the [ANBERNIC firmware index][stock] by exact model. Original GarlicOS is documented for original RG35XX; [GarlicOS **2.0.2**][garlic2] has its own explicit supported-device list including RG35XX+, H and RG28XX. | A Garlic name is not proof of one shared layout across major versions. No general stock/stock-modified folder, archive filter, playlist, or decoder version is established here. Preserve the working file; don't borrow KNULLI paths or assume `PS`/`psx` means the same everywhere. |

### Choosing an output

- **One cartridge:** plain game first. Trial **one game per `.7z`** only when the installed system lists it and launch is acceptable. `.7z` recognition for `gba` says nothing about PSP, disc sets or arcade ROM sets. Keep arcade archives as supplied for their specific emulator/ROM-set version; don't unpack and repack them as cartridges.
- **One CD:** keep the `.cue` **with every track it names**, or the `.gdi` with its tracks. Try a separate CHD only where the frontend lists it and the reader supports that disc type. If the CHD lists but won't open, try the original through the same core before blaming the card. A verified container isn't proof of a good dump, a BIOS match, an old decoder's codec support or playable speed.
- **Several discs:** M3U is a **text index**, not a game or an archive. Preserve disc order, relative names and case. [PCSX ReARMed's Disk Control guide][pcsx] shows how to load the playlist for disc changes. Where the frontend/core combination is unknown, keep each disc independently launchable; don't invent a playlist or hide its only launch targets. A changed launch name can select a different save file.
- **PSP:** [PPSSPP's own format guide][ppsspp] calls CSO its oldest, most tested compressed ISO format. CHD starts with **1.17**; for PSP it recommends DVD-mode CHD, notes the 1.17 sensitivity to 2048-byte hunks, and dates Zstandard support to **1.17.1**. Don't apply a CD recipe to PSP. A known working ISO is a better fallback than promising CHD on an unidentified build.

## Five-minute first test (no install, no command line)

1. **Record the starting point.** Take a photo of the rear model label and About screen. Start one game you own, note the system and emulator/core name if shown, and reach actual gameplay. Choose a second noncritical title or homebrew for the copy test. Back up existing saves and the original files to another drive; no cleaning or deleting.
2. **Find the games, not the operating system.** Shut down cleanly before removing a card, or use a share already set up. Locate the **same** working game and note its enclosing system folder. If only boot files appear, the card is unreadable, or there are two plausible games roots, stop: don't format, guess a path or flash firmware.
3. **Copy one test game.** Stage/export on the computer first. Put the resulting game in that system's already-used folder, without overwriting any file, saves or BIOS. A playlist needs every referenced disc. Safely eject, reinsert, boot the same firmware; refresh/update the games list if that firmware uses one. On muOS, check the assigned core if prompted.
4. **Prove more than listing.** Select the title with the controller. Check for the expected number of menu entries, start real gameplay, listen for missing disc audio, make a new in-game save, exit normally and reload it. If migrating an existing game, check a **backed-up copy** of its old in-game save. For a multi-disc test, open the emulator's disc-control menu from the playlist and confirm a second disc is actually read (ideally at the game's disc-change prompt). A boot logo or seeing two filenames is not a pass.
5. **Write down the result before a batch.** `model/suffix | active OS/build | card/root/system folder | frontend + core/version | game + original → copied format | visible / playable / save reload / disc swap | issue`. Mark untried checks **not tested**. If the copy is invisible, check folder/case and the frontend filter; if it is visible but won't launch, check the selected core, own BIOS and original on that core. If it runs poorly, record the title; a different container is not a faster CPU. Remove only the *new test copy* if needed. Leave the original library intact.

[adam]: https://github.com/eduardofilo/RG350_adam_image/releases/tag/v2.1
[280dt]: https://github.com/OpenDingux/linux/blob/jz-6.1/arch/mips/boot/dts/ingenic/rg280v.dts
[odram]: https://github.com/OpenDingux/linux/blob/jz-6.1/arch/mips/boot/dts/ingenic/bits/gcw0-base.dtsi
[odretro]: https://www.libretro.com/index.php/retroarch-1-9-1-shines-brightly-on-opendingux/
[adames]: https://github.com/eduardofilo/RG350_adam_image/blob/v2.1/data/local/home/.simplemenu/section_groups/consoles.ini
[adamrom]: https://github.com/eduardofilo/RG350_adam_image/wiki/En:-3.-Content-installation
[351]: https://rocknix.org/devices/anbernic/rg351pmv/
[amber]: https://amberelec.org/
[amberrom]: https://amberelec.org/guides/getting-to-know-amberelec
[amberpsp]: https://amberelec.org/systems/sony-psp
[ambersys]: https://amberelec.org/systems
[ark]: https://github.com/christianhaitian/arkos/wiki
[arksys]: https://github.com/christianhaitian/arkos/wiki/ArkOS-Emulators-and-Ports-information
[dark]: https://github.com/christianhaitian/dArkOS/wiki
[darksys]: https://github.com/christianhaitian/dArkOS/wiki/dArkOS-Emulators-and-Ports-information
[353]: https://rocknix.org/devices/anbernic/rg353pmvvs/
[vv]: https://anbernic.com/products/rg353v-rg353vs
[arc]: https://rocknix.org/devices/anbernic/rgarc/
[503]: https://rocknix.org/devices/anbernic/rg503/
[stock]: https://anbernic.com/pages/ambernic-system-update-links-for-various-models
[xxcompare]: https://anbernic.com/blogs/news/differences-and-similarities-about-rg35xx-series
[orig]: https://knulli.org/devices/anbernic/rg35xx/
[knmodels]: https://knulli.org/devices/
[scarab]: https://github.com/knulli-cfw/knulli-linux/releases/tag/20260511
[knstore]: https://knulli.org/play/add-games/game-storage/
[knes]: https://github.com/knulli-cfw/knulli-linux/blob/20260511/package/emulationstation/knulli-es-system/es_systems.yml
[mustard]: https://muos.dev/installation/torrents
[musys]: https://muos.dev/systems
[muassign]: https://muos.dev/systems/cores
[mufirst]: https://muos.dev/installation/first-steps
[mups]: https://muos.dev/systems/homeconsole/ps1
[mupsp]: https://muos.dev/systems/handheld/psp
[mugba]: https://muos.dev/systems/handheld/gba
[rnmodels]: https://rocknix.org/devices/
[rnrelease]: https://github.com/ROCKNIX/distribution/releases/tag/20261101
[rnrom]: https://rocknix.org/play/add-games/
[rnps]: https://rocknix.org/systems/psx/
[rnpsp]: https://rocknix.org/systems/psp/
[rngba]: https://rocknix.org/systems/gba/
[34sp]: https://anbernic.com/products/rg34xxsp
[newsp]: https://anbernic.com/products/rg-sp
[rg35]: https://anbernic.com/products/rg-35
[garlic1]: https://www.patreon.com/blackseraph/posts/garlicos-for-76561333
[garlic2]: https://github.com/GarlicOS/buildroot/releases/tag/v2.0.2
[minui]: https://github.com/shauninman/MinUI/blob/main/skeleton/BASE/README.txt
[552spec]: https://br.anbernic.com/products/anbernic-rg552
[552]: https://rocknix.org/devices/anbernic/rg552/
[vita]: https://anbernic.com/products/rgvita-rgvita-pro
[ds]: https://anbernic.com/products/rgds
[dsnotes]: https://anbernic.com/blogs/news/rg-ds-linux-v1-1-update
[dsplus]: https://anbernic.com/products/rg-ds-plus
[55g1]: https://rocknix.org/devices/anbernic/rg55g1/
[nano]: https://github.com/DrUm78/FunKey-OS/releases/tag/fps-classics
[nanops]: https://github.com/DrUm78/FunKey-OS/blob/rg_nano/FunKey/board/funkey/rootfs-overlay/usr/games/launchers/psone_launch_pcsx.sh
[pcsx]: https://docs.libretro.com/library/pcsx_rearmed/
[ppsspp]: https://www.ppsspp.org/docs/getting-started/dumping-games/
