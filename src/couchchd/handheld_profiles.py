"""Host-side export presets. Selecting one asserts a layout, never detects a device or installs software."""
from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_PROFILE = 'rg405m'
_CHD = ('.chd',)
_DISC = ('.chd', '.m3u')  # Only complete relative playlists of CHDs are exportable.
_LIBRETRO = 'https://docs.libretro.com/library/'
_PSP = 'https://www.ppsspp.org/docs/getting-started/dumping-games/'
_ADAM = 'https://github.com/eduardofilo/RG350_adam_image/wiki/En:-3.-Content-installation'
_ADAM_CONFIG = 'https://github.com/eduardofilo/RG350_adam_image/blob/v2.1/data/local/home/.simplemenu/section_groups/'
_MINUI = 'https://github.com/shauninman/MinUI/blob/main/skeleton/BASE/README.txt'
_ADAM_CARTS = dict(nes='FC', snes='SFC', gb='GB', gbc='GBC', gba='GBA',
                   mastersystem='SMS', gamegear='GG', megadrive='MD')
_ARK = 'https://github.com/christianhaitian/arkos/wiki/ArkOS-Emulators-and-Ports-information'
_KNULLI = ('https://github.com/knulli-cfw/knulli-linux/blob/57afa662009641d181f57241bcc480119b131530/'
           'package/emulationstation/knulli-es-system/es_systems.yml')
_ROCK = 'https://rocknix.org/'
_PLATFORM_HINT = 'Select the existing game folder assigned to one system, not the card or ROM root.'
_ANDROID_HINT = ('Confirm the installed player and grant access to its game folder. If using a frontend, also check '
                 'its indexed extensions: e.g. a Sega CD player can open M3U even if its launcher hides playlists. '
                 + _PLATFORM_HINT)


@dataclass(frozen=True)
class _Player:
    label: str
    systems: dict[str, tuple[str, ...]]
    source: str
    group: str | None = None


_PLAYERS = {
    'pcsx-rearmed': _Player('PCSX ReARMed · PlayStation', {'psx': _DISC}, _LIBRETRO + 'pcsx_rearmed/'),
    'pcsx4all': _Player('PCSX4All (jdgleaver 1.1+) · PlayStation CHD', {'psx': _CHD},
                         'https://github.com/jdgleaver/RG350_pcsx4all/releases/tag/v1.1'),
    'adam-carts': _Player('Adam SimpleMenu / RetroArch · single-cartridge 7z',
                          dict.fromkeys(_ADAM_CARTS, ('.7z',)), _ADAM),
    'minui-ps': _Player('MinUI PS pak · single PlayStation CHD', {'psx': _CHD}, _MINUI),
    'flycast': _Player('Flycast (libretro) · Dreamcast', {'dreamcast': _DISC}, _LIBRETRO + 'flycast/'),
    'ppsspp': _Player('PPSSPP · CSO only (older or unknown version)', {'psp': ('.cso',)}, _PSP, 'ppsspp'),
    'ppsspp-1.17.1': _Player('PPSSPP 1.17.1+ · CSO and DVD CHD', {'psp': ('.cso', '.chd')}, _PSP, 'ppsspp'),
    'genesis-plus-gx': _Player('Genesis Plus GX · Sega CD', {'segacd': _DISC}, _LIBRETRO + 'genesis_plus_gx/'),
    'beetle-pce-fast': _Player('Beetle PCE Fast · PC Engine CD', {'pcenginecd': _DISC}, _LIBRETRO + 'beetle_pce_fast/'),
    'neocd': _Player('NeoCD · Neo Geo CD', {'neogeocd': _CHD}, _LIBRETRO + 'neocd/'),
    'opera': _Player('Opera · 3DO', {'3do': _CHD}, _LIBRETRO + 'opera/'),
    'yabasanshiro': _Player('YabaSanshiro (libretro) · Saturn CHD', {'saturn': _CHD}, _LIBRETRO + 'yabasanshiro/'),
    'armsx2': _Player('ARMSX2 · PS2 CHD (Android 8+, 64-bit OS)', {'ps2': _CHD},
                      'https://armsx2.net/docs/importing-roms'),
    'dolphin-rvz': _Player('Dolphin 5.0-12188+ · GameCube / Wii RVZ', {'gc': ('.rvz',), 'wii': ('.rvz',)},
                            'https://dolphin-emu.org/blog/2020/07/05/dolphin-progress-report-may-and-june-2020/'),
}
_CD_PLAYERS = ('pcsx-rearmed', 'genesis-plus-gx', 'beetle-pce-fast')
_COMMON = (*_CD_PLAYERS, 'flycast', 'ppsspp', 'ppsspp-1.17.1')
_EXTENDED = (*_COMMON, 'neocd', 'opera', 'yabasanshiro')
_ANDROID = (*_EXTENDED, 'dolphin-rvz', 'armsx2')
_H700 = ('RG28XX', 'RG34XX', 'RG35XX 2024', 'RG35XX Plus', 'RG35XX H', 'RG35XX SP',
         'RG40XX H', 'RG40XX V', 'RG CubeXX')
_RK3326 = ('RG351P', 'RG351M', 'RG351V')
_RK3566 = ('RG353V', 'RG353VS', 'RG353M')


@dataclass(frozen=True)
class _Profile:
    label: str
    models: tuple[str, ...]
    firmware: str
    destination: str
    hint: str
    players: tuple[str, ...]
    defaults: tuple[str, ...]
    sources: tuple[str, ...]
    # Only firmware-defined names, relative to an explicitly selected initialized ROM root.
    folders: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # A core supporting M3U does not mean the installed frontend indexes it.
    no_playlist: tuple[str, ...] = ()

    def matches_folder(self, name, system):
        # MinUI permits renamed folders but requires the exact uppercase parenthesized tag.
        return any(name == folder or (folder.startswith('(') and name.endswith(' ' + folder))
                   for folder in self.folders[system])

    def systems(self, player):
        return {s: (_CHD if s in self.no_playlist else exts) for s, exts in _PLAYERS[player].systems.items()}


def _folders(*systems, **alternatives):
    return {**{s: (s,) for s in systems}, **alternatives}


def _android(label, models, products, players=_ANDROID):
    return _Profile(label, models, 'Android · user-configured player folders', 'platform', _ANDROID_HINT,
                    players, (), tuple(p if p.startswith('https:') else 'https://anbernic.com/products/' + p
                                       for p in products))


_PROFILES = {
    DEFAULT_PROFILE: _Profile('RG405M · original pack', ('RG405M',), 'Android · original ROM layout', 'rg405m',
        'Original PS1 / Dreamcast / PSP pack. Defaults assume a CHD-capable PPSSPP. Choose CSO only for older builds.',
        ('pcsx-rearmed', 'flycast', 'ppsspp', 'ppsspp-1.17.1'), ('pcsx-rearmed', 'flycast', 'ppsspp-1.17.1'),
        ('https://anbernic.com/products/rg405m',)),
    'adam': _Profile('Adam image · RG280 / RG300X / RG350', ('RG280M', 'RG280V', 'RG300X', 'RG350', 'RG350M', 'RG350P'),
        'Adam image 2.1 / SimpleMenu (not stock OpenDingux)', 'rom-root',
        'Choose the initialized external roms folder. CD entries are single CHDs; no playlists. '
        'The optional cartridge preset is for single-ROM 7z files, not arcade sets.',
        ('pcsx4all', 'genesis-plus-gx', 'beetle-pce-fast', 'adam-carts'), ('pcsx4all',),
        (_ADAM, _ADAM_CONFIG + 'consoles.ini', _ADAM_CONFIG + 'handhelds.ini'),
        _folders(psx=('PS',), segacd=('SEGACD',), pcenginecd=('PCECD',),
                 **{s: (name,) for s, name in _ADAM_CARTS.items()}), ('segacd', 'pcenginecd')),
    'minui': _Profile('MinUI · original RG35XX / XX', ('RG35XX (original)', 'RG34XX SP',
        *(m for m in _H700 if m != 'RG35XX 2024')), 'MinUI · documented legacy builds', 'rom-root',
        'Choose Roms or the existing folder with the uppercase (PS) tag. Single CHDs only; MinUI has special playlist naming.',
        ('minui-ps',), ('minui-ps',), (_MINUI, 'https://github.com/shauninman/MinUI'), {'psx': ('(PS)',)}),
    'arkos': _Profile('ArkOS · RG351 / RG353 / RG503', (*_RK3326, 'RG351MP', *_RK3566, 'RG503'),
        'ArkOS (historical releases, not dArkOS)', 'rom-root', 'Choose the active EASYROMS game root, or its system folder. '
        'For PSP CHD confirm the installed frontend lists CHD as well as the PPSSPP version.',
        _EXTENDED, (*_CD_PLAYERS, 'flycast', 'ppsspp'), (_ARK,),
        _folders('psx', 'psp', 'dreamcast', 'segacd', 'neogeocd', 'saturn', '3do', pcenginecd=('pcenginecd', 'turbografxcd')),
        ('pcenginecd', 'segacd')),
    'darkos': _Profile('dArkOS · RG351MP / RG353 / RG503', ('RG351MP', *_RK3566, 'RG503'),
        'dArkOS', 'rom-root', 'Choose the active EASYROMS game root, or its initialized system folder. '
        'For PSP CHD confirm the installed frontend lists CHD as well as the PPSSPP version.',
        _COMMON, (*_CD_PLAYERS, 'flycast', 'ppsspp'),
        ('https://github.com/christianhaitian/dArkOS/wiki/dArkOS-Emulators-and-Ports-information',),
        _folders('psx', 'psp', 'dreamcast', 'segacd', pcenginecd=('pcenginecd', 'turbografxcd')), ('segacd', 'pcenginecd')),
    'rocknix': _Profile('ROCKNIX · RG351 / RG353 / ARC', (*_RK3326, *_RK3566, 'RG353P', 'RG353PS', 'RG ARC-D', 'RG ARC-S'),
        'ROCKNIX', 'rom-root', 'Choose the mounted roms directory, or one of its initialized system folders.',
        (*_CD_PLAYERS, 'ppsspp', 'ppsspp-1.17.1'), (*_CD_PLAYERS, 'ppsspp'),
        tuple(_ROCK + 'systems/' + s + '/' for s in ('psx', 'psp', 'segacd', 'pcengine')),
        _folders('psx', 'psp', segacd=('segacd', 'megacd'), pcenginecd=('pcenginecd', 'tg16cd'))),
    'knulli': _Profile('KNULLI · H700 XX', _H700, 'KNULLI', 'rom-root',
        'Choose SHARE/roms or its existing system folder. Confirm optional cores on your installed build.',
        _EXTENDED, ('pcsx-rearmed',), ('https://knulli.org/devices/', 'https://knulli.org/configure/multi-disc-games/', _KNULLI),
        _folders('psx', 'psp', 'dreamcast', 'segacd', 'pcenginecd', 'saturn', '3do', 'neogeocd'), ('pcenginecd',)),
    'muos': _Profile('muOS · XX / VITA Pro', (*_H700, 'RG34XX SP', 'RG35XX Pro', 'RG VITA Pro'), 'muOS / MustardOS', 'platform',
        'Folders under ROMS may have any name. Confirm the folder’s assigned core. ' + _PLATFORM_HINT,
        _EXTENDED, ('pcsx-rearmed', 'ppsspp'), ('https://muos.dev/systems/cores', 'https://muos.dev/installation/first-steps',
        'https://muos.dev/systems/homeconsole/ps1', 'https://muos.dev/systems/handheld/psp', 'https://muos.dev/installation/torrents')),
    'android-rk': _android('Android · RK3566 / RK3399', ('RG353M', 'RG353P', 'RG353V', 'RG ARC-D', 'RG552'),
        ('https://win.anbernic.com/product/347.html', 'https://win.anbernic.com/product/334.html', 'anbernic-rg552'), _COMMON),
    'gamma-core': _Profile('GammaOS Core · RK3566', (*_RK3566, 'RG353P', 'RG353PS', 'RG ARC-D', 'RG ARC-S', 'RG503'),
        'GammaOS Core · Android 13 TV (not stock Linux)', 'platform', _ANDROID_HINT, _COMMON, (),
        ('https://github.com/TheGammaSqueeze/GammaOSCore#supported-devices',
         'https://github.com/TapiocaFox/Daijishou/wiki/Players-and-File-Extensions'), no_playlist=('segacd',)),
    'android-t618': _android('Android · T618', ('RG405M', 'RG405V', 'RG505', 'RG VITA', 'RG Rotate'),
        ('rg405m', 'rg405v', 'rg505', 'rgvita-rgvita-pro', 'rg-rotate')),
    'android-t820': _android('Android · T820', ('RG Cube', 'RG556', 'RG406V', 'RG406H', 'RG Slide', 'RG476H'),
        ('rg-cube', 'rg556', 'rg-406v', 'https://win.anbernic.com/product/510.html', 'rgslide', 'rg476h')),
    'android-modern': _android('Android · other current families',
        ('RG557', 'RG477M', 'RG477V', 'RG VITA Pro', 'RG DS', 'RG55G1', 'RG DDS'),
        ('rg557', 'rg477m', 'rg477v', 'rgvita-rgvita-pro', 'rgds', 'rg-55g1', 'rg-dds')),
    'manual': _Profile('Other handheld · confirmed player', (), 'Stock, custom or unknown firmware', 'platform',
        'No model or firmware assumptions. Confirm a matching player, frontend extension and game folder. ' + _PLATFORM_HINT,
        (*_ANDROID, 'pcsx4all'), (), ()),
}


def profiles() -> list[dict]:
    """JSON-ready options (including firmware-filtered suffixes); not a hardware or playback certification."""
    return [dict(id=key, label=p.label, models=list(p.models), firmware=p.firmware, destination=p.destination,
                 hint=p.hint, folders={s: list(names) for s, names in p.folders.items()}, native=False,
                 evidence=('Host-side file export tested with a synthetic library; physical playback unverified.' if key == DEFAULT_PROFILE
                           else 'User-confirmed player and folder required.' if key == 'manual'
                           else 'Documented destination; physical export and playback pending.'), sources=list(p.sources),
                 players=[dict(id=k, label=_PLAYERS[k].label, group=_PLAYERS[k].group, default=k in p.defaults,
                               source=_PLAYERS[k].source, systems={s: list(x) for s, x in p.systems(k).items()})
                          for k in p.players]) for key, p in _PROFILES.items()]


def resolve_profile(key, players):
    """Resolve explicit choices, intersecting player suffixes with the selected frontend’s extension filter."""
    if key is None:
        key = DEFAULT_PROFILE
    if key not in _PROFILES:
        raise ValueError(f'Unknown handheld profile: {key}')
    profile = _PROFILES[key]
    if isinstance(players, str):
        players = (players,)
    allowed = {}
    groups = set()
    for name in dict.fromkeys(profile.defaults if players is None else players):
        if name not in profile.players:
            raise ValueError(f'Unknown player for {profile.label}: {name}')
        group = _PLAYERS[name].group
        if group and group in groups:
            raise ValueError(f'Choose only one {group.upper()} version.')
        groups.add(group)
        for system, exts in profile.systems(name).items():
            allowed.setdefault(system, set()).update(exts)
    return profile, allowed
