"""Assembles a native macOS (Apple Silicon) Schedule I.app from the Windows build.

- Unity 2022.3.62f2 macOS Mono player (UnityPlayer.app template), started by tools/launcher, which first rebuilds
  the app if the game folder changed (MacPortUpdate in Info.plist)
- macOS Mono class libraries (unityjit-macos) and macOS UnityEngine modules
- game data with D3D11 shaders replaced by converted Metal shaders (work/patched)
- Steamworks.NET rebuilt for macOS + Valve's universal libsteam_api
- Burst code compiled for arm64 (burst_aot.py)

--bench builds "Schedule I Bench.app" instead: separate bundle id (own PlayerPrefs and save folder), the
MacPortBench harness and frame timing stats enabled (see bench.py).
"""
import argparse
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paths  # noqa: E402
from paths import DATA_NAME, ROOT  # noqa: E402

MODULE = paths.UNITY_MAC
PLAYER = MODULE + '/Variations/macos_arm64_player_nondevelopment_mono/UnityPlayer.app/Contents'
PLAYER_DEV = MODULE + '/Variations/macos_arm64_player_development_mono/UnityPlayer.app/Contents'
MAC_MANAGED = MODULE + '/Variations/mono/Managed'
MAC_BCL = paths.UNITY_MONO + '/lib/mono/unityjit-macos'
PATCHED = ROOT + '/work/patched'
STEAMWORKS_DLL = ROOT + '/work/build/steamworks/out/com.rlabrecque.steamworks.net.dll'
STEAM_BUNDLE = paths.STEAMWORKS_NET + '/com.rlabrecque.steamworks.net/Plugins/steam_api.bundle'
STEAM_APP_ID = '3164500'
UNITY_CHANGESET = '7670c08855a9'
BUNDLE_ID = 'com.TVGS.ScheduleI'
RUNTIME_DLL = ROOT + '/tools/csharp/MacPortRuntime/out/MacPortRuntime.dll'
BENCH_DLL = ROOT + '/tools/csharp/MacPortBench/out/MacPortBench.dll'
LOAD_AFTER_SCENE, LOAD_BEFORE_SCENE = 0, 1  # RuntimeInitializeLoadType

# Files rewritten by the shader converter (relative to the data folder)
PATCHED_FILES = ['globalgamemanagers', 'globalgamemanagers.assets', 'resources.assets', 'sharedassets0.assets',
                 'sharedassets1.assets', 'sharedassets2.assets', 'Resources/unity_builtin_extra']


_clones_work = {}  # (source volume, destination volume) -> bool


def clone(src, dst):
    """APFS copy-on-write clone (instant, no extra space); a normal copy across volumes or on other file systems."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    volumes = (os.stat(src).st_dev, os.stat(os.path.dirname(dst)).st_dev)
    if _clones_work.get(volumes, True):
        _clones_work[volumes] = subprocess.run(['cp', '-c', src, dst], stderr=subprocess.DEVNULL).returncode == 0
        if _clones_work[volumes]:
            return
    shutil.copy2(src, dst)


def clone_tree(src, dst, skip=()):
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        if any(rel == s or rel.startswith(s + os.sep) for s in skip):
            continue
        for f in files:
            if f == '.DS_Store' or f.endswith('.meta'):
                continue
            r = os.path.normpath(os.path.join(rel, f))
            if r in skip:
                continue
            clone(os.path.join(root, f), os.path.join(dst, r))


def read_ahead(root):
    """Reads every file under root once, several at a time: codesign hashes them all with small reads that each wait
    for the disk (~30 s for the 6 GB app on an internal SSD, minutes on a USB one); from memory it takes ~4 s."""
    files = []
    for r, _, names in os.walk(root):
        files += [p for p in (os.path.join(r, n) for n in names) if not os.path.islink(p)]

    stop = threading.Event()

    def read(path):
        with open(path, 'rb', buffering=0) as f:
            buf = bytearray(1 << 20)
            while not stop.is_set() and f.readinto(buf):
                pass
    with ThreadPoolExecutor(8) as pool:
        try:
            list(pool.map(read, sorted(files, key=os.path.getsize, reverse=True)))
        except BaseException:  # e.g. interrupted: don't wait for the reads
            stop.set()
            raise


def rewrite_json(path, edit):
    with open(path) as f:
        d = json.load(f)
    edit(d)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(d, f, separators=(',', ':'))
    os.replace(tmp, path)  # replaces the clone instead of writing through it


def register_startup(data_dst, dll, class_name, load_type):
    """Adds a port assembly to the player and has Unity call MacPort.<class_name>.Init() at startup
    (through the ScriptingAssemblies.json / RuntimeInitializeOnLoads.json manifests, no game code changes)."""
    name = os.path.splitext(os.path.basename(dll))[0]
    clone(dll, os.path.join(data_dst, 'Managed', name + '.dll'))

    def add_assembly(d):
        d['names'].append(name + '.dll')
        d['types'].append(16)
    rewrite_json(os.path.join(data_dst, 'ScriptingAssemblies.json'), add_assembly)
    rewrite_json(os.path.join(data_dst, 'RuntimeInitializeOnLoads.json'), lambda d: d['root'].append(
        {'assemblyName': name, 'nameSpace': 'MacPort', 'className': class_name, 'methodName': 'Init',
         'loadTypes': load_type, 'isUnityClass': False}))


def install_bench(data_dst):
    """Adds the MacPortBench harness and enables frame timing stats."""
    import UnityPy
    register_startup(data_dst, BENCH_DLL, 'Bench', LOAD_AFTER_SCENE)
    ggm = os.path.join(data_dst, 'globalgamemanagers')
    env = UnityPy.load(ggm)
    for obj in env.objects:
        if obj.type.name == 'PlayerSettings':
            tt = obj.read_typetree()
            tt['enableFrameTimingStats'] = True
            obj.save_typetree(tt)
    data = next(iter(env.files.values())).save()
    os.remove(ggm)
    with open(ggm, 'wb') as f:
        f.write(data)


def game_version(game):
    """The version of the patched globalgamemanagers (see patch_settings.py --main-branch-version): the macOS player
    reports CFBundleShortVersionString as Application.version, which multiplayer lobbies compare."""
    try:
        import UnityPy
        patched = os.path.join(PATCHED, 'globalgamemanagers')
        env = UnityPy.load(patched if os.path.exists(patched) else os.path.join(game, DATA_NAME, 'globalgamemanagers'))
        for obj in env.objects:
            if obj.type.name == 'PlayerSettings':
                tt = obj.read_typetree()
                return tt.get('bundleVersion') or '1.0'
    except Exception as e:
        print('warning: could not read version:', e)
    return '1.0'


def build_app(app, args):
    bundle_id = BUNDLE_ID + ('.bench' if args.bench else '')
    contents = os.path.join(app, 'Contents')
    exe_name = 'Schedule I'
    player = PLAYER_DEV if args.development else PLAYER

    # --- player binaries; the executable is the launcher, which calls the player's UnityPlayer.dylib like Unity's stub
    clone(paths.LAUNCHER, os.path.join(contents, 'MacOS', exe_name))
    for f in os.listdir(os.path.join(player, 'Frameworks')):
        clone(os.path.join(player, 'Frameworks', f), os.path.join(contents, 'Frameworks', f))
    clone(os.path.join(player, 'Resources/unity default resources'), os.path.join(contents, 'Resources/unity default resources'))
    entry = os.path.join(MODULE, 'Source/Player/MacPlayer/MacPlayerEntryPoint/Resources')
    clone_tree(os.path.join(entry, 'MainMenu.nib'), os.path.join(contents, 'Resources/MainMenu.nib')) if os.path.isdir(os.path.join(entry, 'MainMenu.nib')) else clone(os.path.join(entry, 'MainMenu.nib'), os.path.join(contents, 'Resources/MainMenu.nib'))
    icon = os.path.join(ROOT, 'work/icon/ScheduleI.icns')
    clone(icon if os.path.exists(icon) else os.path.join(entry, 'PlayerIcon.icns'), os.path.join(contents, 'Resources/PlayerIcon.icns'))
    # --- Mono config
    clone_tree(os.path.join(MODULE, 'MonoBleedingEdge/etc'), os.path.join(contents, 'MonoBleedingEdge/etc'))

    # --- game data
    data_src = os.path.join(args.game, DATA_NAME)
    data_dst = os.path.join(contents, 'Resources/Data')
    clone_tree(data_src, data_dst, skip=('Plugins', 'Managed') + tuple(PATCHED_FILES))
    for rel in PATCHED_FILES:
        src = os.path.join(PATCHED, rel)
        if not os.path.exists(src):
            print('WARNING: %s not converted yet, using the D3D11 original' % rel)
            src = os.path.join(data_src, rel)
        clone(src, os.path.join(data_dst, rel))
    # macOS player built-in resources (Metal compute shaders, etc.)
    clone(os.path.join(player, 'Resources/unity default resources'), os.path.join(data_dst, 'Resources/unity default resources'))

    # --- managed assemblies
    managed_src = os.path.join(data_src, 'Managed')
    managed_dst = os.path.join(data_dst, 'Managed')
    for f in sorted(os.listdir(managed_src)):
        if not f.endswith('.dll'):
            continue
        src = os.path.join(managed_src, f)
        if f == 'com.rlabrecque.steamworks.net.dll':
            src = STEAMWORKS_DLL
        elif os.path.exists(os.path.join(MAC_MANAGED, f)):
            src = os.path.join(MAC_MANAGED, f)
        elif os.path.exists(os.path.join(MAC_BCL, f)):
            src = os.path.join(MAC_BCL, f)
        elif os.path.exists(os.path.join(MAC_BCL, 'Facades', f)):
            src = os.path.join(MAC_BCL, 'Facades', f)
        clone(src, os.path.join(managed_dst, f))

    # IL-patched game assemblies and port helper assemblies (tools/csharp)
    overrides = os.path.join(ROOT, 'work/managed')
    if os.path.isdir(overrides):
        for f in sorted(os.listdir(overrides)):
            if f.endswith('.dll'):
                clone(os.path.join(overrides, f), os.path.join(managed_dst, f))

    register_startup(data_dst, RUNTIME_DLL, 'Runtime', LOAD_BEFORE_SCENE)
    if args.bench:
        install_bench(data_dst)

    # --- native plugins (Burst compiles while the rest is assembled and read ahead for codesign)
    burst = None
    if not args.no_burst:
        from burst_aot import compile_burst
        burst = ThreadPoolExecutor(1).submit(compile_burst, data_dst)
    clone_tree(STEAM_BUNDLE, os.path.join(contents, 'PlugIns/steam_api.bundle'))
    with open(os.path.join(contents, 'MacOS/steam_appid.txt'), 'w') as f:
        f.write(STEAM_APP_ID)

    # --- Info.plist
    version = game_version(args.game)
    info = {
        'CFBundleDevelopmentRegion': 'English',
        'CFBundleExecutable': exe_name,
        'CFBundleGetInfoString': 'Schedule I %s (macOS port, Unity 2022.3.62f2)' % version,
        'CFBundleIconFile': 'PlayerIcon.icns',
        'CFBundleIdentifier': bundle_id,
        'CFBundleInfoDictionaryVersion': '6.0',
        'CFBundleSupportedPlatforms': ['MacOSX'],
        'LSApplicationCategoryType': 'public.app-category.simulation-games',
        'GCSupportsGameMode': True,
        'CFBundleName': 'Schedule I Bench' if args.bench else 'Schedule I',
        'CFBundlePackageType': 'APPL',
        'CFBundleShortVersionString': version,
        'CFBundleVersion': version,
        'NSMainNibFile': 'MainMenu',
        'NSPrincipalClass': 'PlayerApplication',
        'UnityBuildNumber': UNITY_CHANGESET,
        'LSMinimumSystemVersion': '11.0',
        'NSAppTransportSecurity': {'NSAllowsArbitraryLoads': True},
        'NSHighResolutionCapable': True,
        'LSEnvironment': {'SteamAppId': STEAM_APP_ID, 'SteamGameId': STEAM_APP_ID},
        'NSMicrophoneUsageDescription': 'Voice chat',
    }
    if args.update:
        info['MacPortUpdate'] = json.loads(args.update)
    with open(os.path.join(contents, 'Info.plist'), 'wb') as f:
        plistlib.dump(info, f)
    with open(os.path.join(contents, 'PkgInfo'), 'w') as f:
        f.write('APPL????')
    if args.game_files:  # the state of the game folder, which the launcher compares it with
        shutil.copyfile(args.game_files, os.path.join(contents, 'Resources/MacPortGameFiles.txt'))

    if not args.no_sign:
        read_ahead(app)
    if burst:
        clone(burst.result(), os.path.join(contents, 'PlugIns/lib_burst_generated.bundle'))
    if not args.no_sign:
        # ad-hoc signature (required on Apple Silicon once Info.plist/bundle contents changed)
        r = subprocess.run(['codesign', '--force', '--deep', '--sign', '-', app], capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit('codesign failed:\n' + r.stdout + r.stderr)
        # The first start of newly signed code waits while macOS checks it (16 s for this bundle on a USB SSD):
        # starting the launcher once now (its --check only compares the game files) gets that over with here, while
        # the launcher's progress panel is still up, instead of when the game starts
        if subprocess.run([os.path.join(contents, 'MacOS', exe_name), '--check'],
                          stdout=subprocess.DEVNULL).returncode not in (0, 1):
            sys.exit("the app's executable doesn't start")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--game', default=paths.saved_game_dir(), help='game folder (default: the one of the last build.sh run)')
    ap.add_argument('--out', default=None)
    ap.add_argument('--development', action='store_true', help='use the development player (more logging)')
    ap.add_argument('--no-sign', action='store_true')
    ap.add_argument('--no-burst', action='store_true', help='skip Burst AOT (Burst jobs then run as managed code)')
    ap.add_argument('--bench', action='store_true', help='build the benchmark variant (see bench.py)')
    ap.add_argument('--update', metavar='JSON', help="what the app's launcher needs to rebuild the app when the game "
                    'changes (from build.py)')
    ap.add_argument('--game-files', metavar='FILE', help="the game's data files as listed by build.py, which the "
                    'launcher compares the game folder with')
    args = ap.parse_args()
    if not args.game:
        ap.error('--game is required (no previous build.sh run)')

    app = os.path.abspath(args.out or ROOT + ('/build/Schedule I Bench.app' if args.bench else '/build/Schedule I.app'))
    # Assembled next to the app and swapped in when complete: an assembly that fails or is stopped leaves the app as it
    # was (its launcher may be the one waiting for this build)
    new = os.path.join(os.path.dirname(app), '.new-' + os.path.basename(app))
    old = os.path.join(os.path.dirname(app), '.old-' + os.path.basename(app))
    for d in (new, old):
        if os.path.exists(d):
            shutil.rmtree(d)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))  # (how build.py stops it) so that it cleans up
    try:
        build_app(new, args)
    except BaseException:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        shutil.rmtree(new, ignore_errors=True)
        raise
    if os.path.exists(app):
        os.rename(app, old)
    os.rename(new, app)
    shutil.rmtree(old, ignore_errors=True)
    print('built', app)


if __name__ == '__main__':
    main()
