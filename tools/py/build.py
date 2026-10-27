"""Builds build/Schedule I.app from your Windows copy of Schedule I (Mono build); run it through ./build.sh.

Steps, each skipped when up to date (after a game update only the affected work is redone):
  game        finds the game folder; checks it is the Mono build with the Unity/package versions pinned below
  deps        downloads into deps/ (pinned, checksummed): .NET SDK, Unity's macOS player module and the editor's
              Mono, Steamworks.NET, Input System and Burst; in the background, as only the assemblies step needs them
  tools       libmtlcheck, libdxbc2metal, the app's launcher
  shaders     D3D11 -> Metal shaders, graphics settings -> work/patched
  assemblies  the C# helpers, Steamworks.NET and Input System rebuilt for macOS (with the MonoScript tables of the
              game's copies), Assembly-CSharp IL patch -> work/managed
  icon        app icon from Schedule I.exe -> work/icon
  app         assemble_app.py -> build/Schedule I.app

The app's launcher (tools/launcher) runs this again when the game folder changed since the build (a Steam update).
"""
import argparse
import fcntl
import glob
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import struct
import subprocess
import sys
import threading
import time
import zlib
from xml.etree import ElementTree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paths  # noqa: E402
from paths import DATA_NAME, DEPS, ROOT, TOOLS, WORK  # noqa: E402

# The versions the game is built with. The downloads below match them, and the game step stops if the game files
# report others (e.g. after a game update), because the macOS libraries built from them would no longer fit.
UNITY_VERSION = '2022.3.62f2'
INPUT_SYSTEM_VERSION = '1.14.1'
STEAMWORKS_NET_VERSION = '2024.8.0'
BURST_VERSION = '1.8.21'
DOTNET_SDK_VERSION = '10.0.401'

STEAM = os.path.expanduser('~/Library/Application Support/Steam')
CROSSOVER_BOTTLES = os.path.expanduser('~/Library/Application Support/CrossOver/Bottles')
STEAM_APP_ID, STEAM_DEPOT_ID = '3164500', '3164501'

CS_PROJECTS = ('tools/csharp/monoscripttypes', 'tools/csharp/MacPortCompat', 'tools/csharp/MacPortRuntime',
               'tools/csharp/MacPortBench', 'tools/csharp/ilpatch', 'work/build/steamworks', 'work/build/inputsystem')
BENCH_PROJECT = 'tools/csharp/MacPortBench'  # only built for --bench
# Libraries rebuilt for macOS from source -> the game's copy, whose Unity-generated MonoScript table they must carry
MONOSCRIPT_TABLES = {'work/build/steamworks': 'com.rlabrecque.steamworks.net.dll',
                     'work/build/inputsystem': 'Unity.InputSystem.dll'}
# Files rewritten by the shader converter (relative to the data folder) and the files the conversion depends on
CONVERTED = ('globalgamemanagers.assets', 'resources.assets', 'sharedassets0.assets', 'sharedassets1.assets',
             'sharedassets2.assets', 'Resources/unity_builtin_extra')
CONVERTER = ('tools/py/port_shaders.py', 'tools/py/patch_shaders.py', 'tools/py/convert.py', 'tools/py/compute.py',
             'tools/py/ushader.py', 'tools/py/dxbc2metal.py', 'tools/py/mtlcheck.py',
             'tools/dxbc2metal/build/libdxbc2metal.dylib', 'tools/mtlcheck/libmtlcheck.dylib')
# What assemble_app.py uses besides the game folder, deps/ and work/managed: skipping an unchanged app saves
# re-signing it (minutes for the 6 GB bundle)
APP_INPUTS = ('tools/py/assemble_app.py', 'tools/py/burst_aot.py', 'tools/py/paths.py', 'work/patched/.stamp',
              'work/icon/ScheduleI.icns', 'work/build/steamworks/out/com.rlabrecque.steamworks.net.dll',
              'tools/csharp/MacPortRuntime/out/MacPortRuntime.dll', 'tools/launcher/launcher')
BENCH_APP_INPUTS = ('tools/csharp/MacPortBench/out/MacPortBench.dll',)

IL2CPP_HELP = '''%s is the IL2CPP build (Steam's default branch), but the port needs the Mono build.
In Steam: Schedule I > Properties > Game Versions & Betas > select "alternate", let it update, then run ./build.sh again.'''
NOT_FOUND_HELP = '''Couldn't find the Windows version of Schedule I in your Steam libraries (or CrossOver bottles).
Install it with Steam (e.g. in CrossOver, or with a Steam compatibility tool) or copy the game folder from a Windows PC,
switch it to the "alternate" (Mono) branch, then run:  ./build.sh --game "/path/to/Schedule I"'''


class BuildError(Exception):
    pass


_running = set()  # child processes, stopped if the build ends while the downloads still run in the background
_stopping = threading.Event()


def start(cmd, **kw):
    if _stopping.is_set():
        raise BuildError('stopped')
    p = subprocess.Popen(cmd, **kw)
    _running.add(p)
    return p


def stop_children(grace=0):
    """Stops the child processes; those still running after grace seconds (when interrupted, they got the signal too
    and may be cleaning up) are terminated."""
    _stopping.set()
    deadline = time.time() + grace
    for p in list(_running):
        try:
            p.wait(max(0, deadline - time.time()))
        except subprocess.TimeoutExpired:
            p.terminate()


def run(cmd, quiet=False, **kw):
    """Runs a command; with quiet=True its output is only shown when it fails."""
    if quiet:
        kw.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    p = start(cmd, **kw)
    out = p.communicate()[0]  # if interrupted, p stays in _running for stop_children
    _running.discard(p)
    if p.returncode != 0:
        if quiet:
            sys.stdout.write(out[-8000:])
        raise BuildError('command failed (exit %d): %s' % (p.returncode, ' '.join(cmd)))


def run_logged(cmd, log, show=False):
    """Runs a command with its output going to a log file (and with show=True also to the console, as it comes);
    returns the last line of the log."""
    with open(log, 'w') as f:
        if show:
            p = start(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='replace')
            for line in p.stdout:
                f.write(line)
                print('    ' + line, end='', flush=True)
            p.wait()
            _running.discard(p)
            returncode = p.returncode
        else:
            returncode = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT).returncode
    with open(log, errors='replace') as f:
        lines = f.read().splitlines()
    if returncode != 0:
        if not show:
            sys.stdout.write('\n'.join(lines[-40:]) + '\n')
        raise BuildError('command failed (exit %d), see %s' % (returncode, log))
    return lines[-1] if lines else ''


def read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)


def file_digest(path, algo='sha256'):
    h = hashlib.new(algo)
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 22), b''):
            h.update(chunk)
    return h.hexdigest()


def file_id(path):
    """Size and modification time, which tell whether a game file changed (Steam rewrites the files an update
    changes); hashing the 600 MB the shader converter reads took most of a build with nothing to do."""
    if not os.path.exists(path):
        return '-'
    st = os.stat(path)
    return '%d %d' % (st.st_size, st.st_mtime_ns)


def read_stamps(path):
    """{name: fingerprint} from a stamp file of "name fingerprint" lines."""
    return dict(line.split(' ', 1) for line in (read_text(path) or '').splitlines() if ' ' in line)


def write_stamps(path, stamps):
    write_text(path, ''.join('%s %s\n' % item for item in sorted(stamps.items())))


def up_to_date(target, sources):
    return os.path.exists(target) and all(os.path.getmtime(target) >= os.path.getmtime(s) for s in sources)


def source_files(*dirs):
    """The files under dirs, without CMake build folders."""
    files = []
    for d in dirs:
        for root, subdirs, names in os.walk(d):
            subdirs[:] = [s for s in subdirs if not s.startswith('build')]
            files += [os.path.join(root, n) for n in names]
    return files


def tool(name):
    return os.path.join(TOOLS, 'py', name)


def lock_work():
    """Waits until no other build of this folder runs (./build.sh, or the app's launcher updating the app); returns
    the lock file, which holds the lock until it is closed."""
    os.makedirs(WORK, exist_ok=True)
    f = open(os.path.join(WORK, '.lock'), 'w')
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('    waiting for the other build to finish (./build.sh, or the app updating itself)')
        fcntl.flock(f, fcntl.LOCK_EX)
    return f


# --- game

def is_game_dir(d):
    return os.path.isfile(os.path.join(d, DATA_NAME, 'globalgamemanagers'))


def is_il2cpp(d):
    return os.path.exists(os.path.join(d, 'GameAssembly.dll')) or os.path.isdir(os.path.join(d, DATA_NAME, 'il2cpp_data'))


def vdf_paths(vdf):
    """Library folder paths listed in a Steam libraryfolders.vdf."""
    text = read_text(vdf) or ''
    return [p.replace('\\\\', '\\') for p in re.findall(r'"path"\s+"((?:[^"\\]|\\.)*)"', text)]


def wine_path(bottle, path):
    """Maps a Windows path inside a Wine bottle (C:\\Games\\Steam) to the macOS one."""
    m = re.match(r'([A-Za-z]):\\(.*)', path)
    return os.path.join(bottle, 'dosdevices', m.group(1).lower() + ':', *m.group(2).split('\\')) if m else path


def candidate_game_dirs():
    """Where Steam (for Mac, or Windows Steam in CrossOver) installs the game."""
    libraries = [STEAM] + vdf_paths(os.path.join(STEAM, 'steamapps', 'libraryfolders.vdf'))
    for bottle in sorted(glob.glob(os.path.join(CROSSOVER_BOTTLES, '*'))):
        steam = os.path.join(bottle, 'drive_c', 'Program Files (x86)', 'Steam')
        libraries.append(steam)
        libraries += [wine_path(bottle, p) for p in vdf_paths(os.path.join(steam, 'steamapps', 'libraryfolders.vdf'))]
    dirs = [os.path.join(lib, 'steamapps', 'common', 'Schedule I') for lib in libraries]
    # Steam console "download_depot" output
    for steamapps in (os.path.join(STEAM, 'steamapps'), os.path.join(STEAM, 'Steam.AppBundle/Steam/Contents/MacOS/steamapps')):
        dirs.append(os.path.join(steamapps, 'content', 'app_' + STEAM_APP_ID, 'depot_' + STEAM_DEPOT_ID))
    unique = {}
    for d in dirs:
        if is_game_dir(d):
            unique.setdefault(os.path.realpath(d), d)
    return list(unique.values())


def game_dir_in(path):
    """The game folder for a folder given by the user: the game folder itself, a folder inside it (such as
    "Schedule I_Data") or a Steam library containing it; None if it is none of these."""
    path = os.path.abspath(os.path.expanduser(path))
    for d in (path, os.path.dirname(path), os.path.join(path, 'Schedule I'), os.path.join(path, 'common', 'Schedule I'),
              os.path.join(path, 'steamapps', 'common', 'Schedule I')):
        if is_game_dir(d):
            return d
    return None


def choose_folder():
    """A folder picked in a Finder dialog; '' when cancelled, None when no dialog can be shown (e.g. over SSH)."""
    r = subprocess.run(['osascript', '-e', 'POSIX path of (choose folder with prompt "Select the Schedule I folder '
                        '(the Windows version, containing Schedule I_Data)")'], capture_output=True, text=True)
    if r.returncode == 0:
        return r.stdout.strip()
    return '' if '-128' in r.stderr else None


def typed_folder():
    try:
        line = input('    Drag the folder into this window and press Return, or type its path: ').strip()
    except EOFError:
        return ''
    try:
        words = shlex.split(line)  # a dragged path comes with its spaces escaped
    except ValueError:
        words = []
    return words[0] if len(words) == 1 else line


def ask_for_game():
    """Asks the user to locate the game folder: in a Finder dialog, or in the terminal when there is no GUI."""
    if not sys.stdin.isatty():
        raise BuildError(NOT_FOUND_HELP)
    print('    Couldn\'t find Schedule I in your Steam libraries. Please locate the game folder (the Windows version,')
    print('    containing "%s"; it must be on Steam\'s "alternate" branch).' % DATA_NAME)
    while True:
        picked = choose_folder()
        if picked is None:
            picked = typed_folder()
        if not picked:
            raise BuildError(NOT_FOUND_HELP)
        game = game_dir_in(picked)
        if game:
            return game
        print('    %s does not contain "%s", please try again.' % (picked, DATA_NAME))


def find_game(explicit):
    if explicit:
        game = game_dir_in(explicit)
        if not game:
            raise BuildError('%s is not the game folder (the one containing "%s")' % (os.path.abspath(explicit), DATA_NAME))
        return game
    saved = paths.saved_game_dir()
    if saved and is_game_dir(saved) and not is_il2cpp(saved):
        return saved
    found = candidate_game_dirs()
    mono = [d for d in found if not is_il2cpp(d)]
    if mono or found:
        return (mono or found)[0]  # an IL2CPP install gets explained by check_game()
    return ask_for_game()


def game_versions(data):
    """{name: (version found in the game files or None, pinned version)}"""
    managed = os.path.join(data, 'Managed')

    def search(name, pattern, encoding='ascii'):
        try:
            with open(os.path.join(managed, name), 'rb') as f:
                m = re.search(pattern, f.read())
        except OSError:
            return None
        return m.group(1).decode(encoding) if m else None

    with open(os.path.join(data, 'globalgamemanagers'), 'rb') as f:
        m = re.search(rb'(\d{4}\.\d+\.\d+[abfpx]\d+)', f.read(256))  # serialized file header
    return {
        'Unity': (m.group(1).decode() if m else None, UNITY_VERSION),
        # package paths in the MonoScript tables / Steamworks.NET's version constant (UTF-16)
        'Input System': (search('Unity.InputSystem.dll', rb'com\.unity\.inputsystem@(\d+\.\d+\.\d+)'), INPUT_SYSTEM_VERSION),
        'Steamworks.NET': (search('com.rlabrecque.steamworks.net.dll', rb'((?:\d\x00){4}(?:\.\x00(?:\d\x00)+){2})',
                                  'utf-16-le'), STEAMWORKS_NET_VERSION),
        'Burst': (search('Unity.Burst.dll', rb'com\.unity\.burst@(\d+\.\d+\.\d+)'), BURST_VERSION),
    }


def check_game(game, skip_version_check):
    data = os.path.join(game, DATA_NAME)
    if is_il2cpp(game):
        raise BuildError(IL2CPP_HELP % game)
    if not os.path.isfile(os.path.join(data, 'Managed', 'Assembly-CSharp.dll')):
        raise BuildError('%s has no Managed/Assembly-CSharp.dll (incomplete or not the Windows build?)' % data)
    mismatches = []
    for name, (found, pinned) in game_versions(data).items():
        if found is None:
            print('    warning: could not determine the game\'s %s version' % name)
        elif found != pinned:
            mismatches.append('%s %s (the port is pinned to %s)' % (name, found, pinned))
    if mismatches and not skip_version_check:
        raise BuildError('the game files use ' + ', '.join(mismatches) + '.\nThe port needs updating for this game '
                         'version (pins in tools/py/build.py); --skip-version-check builds anyway.')
    write_text(paths.GAME_DIR_FILE, game + '\n')


# --- deps

def untar(strip=0):
    def extract(archive, dest):
        run(['tar', '-xzf', archive, '-C', dest, '--strip-components', str(strip)])
    return extract


def xar_member(pkg, member):
    """(offset, length) of a stored (uncompressed) member of a xar archive, e.g. the Payload of a .pkg component."""
    with open(pkg, 'rb') as f:
        magic, header_size, _, toc_size = struct.unpack('>4sHHQ', f.read(16))
        if magic != b'xar!':
            raise BuildError('%s is not a xar archive' % pkg)
        f.seek(header_size)
        node = ElementTree.fromstring(zlib.decompress(f.read(toc_size))).find('toc')
    for name in member.split('/'):
        node = next((el for el in node.findall('file') if el.findtext('name') == name), None)
        if node is None:
            raise BuildError('%s has no %s' % (pkg, member))
    data = node.find('data')
    if data.find('encoding').get('style') != 'application/octet-stream':
        raise BuildError('%s: %s is compressed inside the archive' % (pkg, member))
    return header_size + toc_size + int(data.findtext('offset')), int(data.findtext('length'))


def pkg_payload(member, *tar_args):
    """Extracts a .pkg component's payload (gzip'd cpio) straight out of the .pkg, without a temporary copy."""
    def extract(archive, dest):
        offset, length = xar_member(archive, member)
        tar = start(['tar', '-xzf', '-', '-C', dest, *tar_args], stdin=subprocess.PIPE)
        try:
            with open(archive, 'rb') as f:
                f.seek(offset)
                while length:
                    chunk = f.read(min(length, 1 << 22))
                    if not chunk:
                        break
                    tar.stdin.write(chunk)
                    length -= len(chunk)
        except BrokenPipeError:
            pass  # tar stopped early; its exit status reports the failure
        finally:
            try:
                tar.stdin.close()
            except BrokenPipeError:
                pass
        status = tar.wait()
        _running.discard(tar)
        if status != 0 or length:
            raise BuildError('could not extract %s from %s' % (member, os.path.basename(archive)))
    return extract


UNITY_DOWNLOAD = 'https://download.unity3d.com/download_unity/7670c08855a9/'
DEPENDENCIES = (  # (install dir, url, digest, size, extract)
    (os.path.dirname(paths.DOTNET),
     'https://builds.dotnet.microsoft.com/dotnet/Sdk/%s/dotnet-sdk-%s-osx-arm64.tar.gz' % (DOTNET_SDK_VERSION, DOTNET_SDK_VERSION),
     'sha512:69f64eb00dc045398755c440b152225d544301a345a146a16e86a56a0c52b7c94b2c331520e976dbb821f18d31930aafbd25bb85961e3517e0665414ce0cbcff',
     230753980, untar()),
    # only the editor's MonoBleedingEdge (macOS class libraries, and mono to run the Burst compiler) from the 4.5 GB editor
    (paths.UNITY_MONO, UNITY_DOWNLOAD + 'MacEditorInstallerArm64/Unity-%s.pkg' % UNITY_VERSION,
     'sha256:5d2575c1b10a2a9f1f89bf40631a6b9bec3628fe16ca5f2203720af7943a3f05',
     4504585852, pkg_payload('Unity.pkg.tmp/Payload', '--strip-components', '5', './Unity/Unity.app/Contents/MonoBleedingEdge')),
    (paths.UNITY_MAC, UNITY_DOWNLOAD + 'MacEditorTargetInstaller/UnitySetup-Mac-Mono-Support-for-Editor-%s.pkg' % UNITY_VERSION,
     'sha256:ada8c5ebaa3431d54a5490df95196d8b439a55d57a703c5aade9d48fd33375e9',
     551827460, pkg_payload('TargetSupport.pkg.tmp/Payload', '--exclude', './Variations/macos_x64*')),
    (paths.STEAMWORKS_NET, 'https://github.com/rlabrecque/Steamworks.NET/archive/refs/tags/%s.tar.gz' % STEAMWORKS_NET_VERSION,
     'sha256:7ce40d2e3d7dd9a84f46375ee62439cba280be54ae562c272965c81ed6682985',
     1144572, untar(1)),
    (paths.INPUT_SYSTEM, 'https://download.packages.unity.com/com.unity.inputsystem/-/com.unity.inputsystem-%s.tgz' % INPUT_SYSTEM_VERSION,
     'sha256:7a8328583da4fb1a0607c90de1690927f2cab15e5001356da271b6ecea4a7182',
     16849787, untar(1)),
    (paths.BURST, 'https://download.packages.unity.com/com.unity.burst/-/com.unity.burst-%s.tgz' % BURST_VERSION,
     'sha256:cf544366bd61bd9367a087c68de714bba77c044a658ba2f2421fec0ddc142df6',
     603699285, untar(1)),
)


def download(url, digest, path, log, quiet):
    algo, want = digest.split(':')
    part = path + '.part'
    for complete in (path, part):  # left by a run that stopped before extracting (or before renaming)
        if os.path.exists(complete) and file_digest(complete, algo) == want:
            os.replace(complete, path)
            return
    log('downloading %s' % url)
    for attempt in range(3):
        if attempt == 2 and os.path.exists(part):
            os.remove(part)  # last try from scratch, in case resuming is the problem
        curl = start(['curl', '-fL', '-sS' if quiet else '--progress-bar', '--connect-timeout', '30', '--retry', '5',
                      '--retry-delay', '5', '--retry-all-errors', '-C', '-', '-o', part, url])
        status = curl.wait()
        _running.discard(curl)
        if status == 0:
            break
    else:
        raise BuildError('download failed: %s' % url)
    got = file_digest(part, algo)
    if got != want:
        os.remove(part)
        raise BuildError('checksum mismatch for %s\n  expected %s\n  got      %s' % (url, want, got))
    os.replace(part, path)


def missing_deps():
    return [d for d in DEPENDENCIES if read_text(os.path.join(d[0], '.dep')) != '%s %s\n' % (d[1], d[2])]


def install_deps(missing, progress=None):
    """Downloads and extracts dependencies; progress: a Downloads object when running in the background."""
    prefix = '    [deps] ' if progress else '    '
    for dest, url, digest, size, extract in missing:
        name = os.path.basename(url)
        archive = os.path.join(DEPS, 'downloads', os.path.basename(dest) + '-' + name)
        os.makedirs(os.path.dirname(archive), exist_ok=True)
        if progress:
            progress.current = (name, archive + '.part', size)
        download(url, digest, archive, lambda msg: print(prefix + msg, flush=True), quiet=bool(progress))
        if progress:
            progress.current = (name, None, size)
        print('%sextracting %s' % (prefix, name), flush=True)
        partial = dest + '.partial'
        shutil.rmtree(partial, ignore_errors=True)
        os.makedirs(partial)
        extract(archive, partial)
        write_text(os.path.join(partial, '.dep'), '%s %s\n' % (url, digest))
        shutil.rmtree(dest, ignore_errors=True)
        os.rename(partial, dest)
        os.remove(archive)


class Downloads:
    """Installs dependencies in a background thread, while the steps that don't need them run."""

    def __init__(self, missing):
        self.current, self.error = None, None
        self.thread = threading.Thread(target=self._run, args=(missing,), daemon=True)
        self.thread.start()

    def _run(self, missing):
        try:
            install_deps(missing, self)
        except BaseException as e:  # reported by wait()
            self.error = e

    def wait(self):
        """Waits for the downloads, showing their progress now and then."""
        while True:
            self.thread.join(20)
            if not self.thread.is_alive():
                break
            name, part, size = self.current or ('', None, 0)
            if part and os.path.exists(part):
                print('    [deps] %s: %.1f of %.1f GB' % (name, os.path.getsize(part) / 1e9, size / 1e9), flush=True)
        if self.error:
            raise self.error


# --- tools

def dotnet_env():
    env = dict(os.environ, DOTNET_ROOT=os.path.dirname(paths.DOTNET), DOTNET_CLI_HOME=os.path.join(DEPS, 'dotnet-home'),
               NUGET_PACKAGES=os.path.join(DEPS, 'nuget'), DOTNET_CLI_TELEMETRY_OPTOUT='1', DOTNET_NOLOGO='1',
               DOTNET_SKIP_FIRST_TIME_EXPERIENCE='1')
    env['PATH'] = os.path.dirname(paths.DOTNET) + os.pathsep + env.get('PATH', '')
    return env


def build_tools():
    # Metal compiler access, used to validate every converted shader
    src, lib = os.path.join(TOOLS, 'mtlcheck', 'mtlcheck.m'), os.path.join(TOOLS, 'mtlcheck', 'libmtlcheck.dylib')
    if not up_to_date(lib, [src]):
        run(['xcrun', 'clang', '-fobjc-arc', '-dynamiclib', '-O2', '-framework', 'Foundation', '-framework', 'Metal',
             src, '-o', lib], quiet=True)
    # DXBC -> MSL translator (forked HLSLcc), with the CMake from deps/venv
    cmake = os.path.join(os.path.dirname(sys.executable), 'cmake')
    build_dir = os.path.join(TOOLS, 'dxbc2metal', 'build')
    m = re.search(r'^CMAKE_COMMAND:INTERNAL=(.*)$', read_text(os.path.join(build_dir, 'CMakeCache.txt')) or '', re.M)
    if m and not os.path.realpath(m.group(1)).startswith(os.path.realpath(DEPS) + os.sep):
        shutil.rmtree(build_dir)  # configured by another CMake, or before this folder was moved
    if not os.path.exists(os.path.join(build_dir, 'CMakeCache.txt')):
        run([cmake, '-S', os.path.join(TOOLS, 'dxbc2metal'), '-B', build_dir, '-DCMAKE_BUILD_TYPE=Release'], quiet=True)
    # (make takes a while to find nothing to do: it checks every object and header)
    if not up_to_date(os.path.join(build_dir, 'libdxbc2metal.dylib'),
                      source_files(os.path.join(TOOLS, 'dxbc2metal'), os.path.join(TOOLS, 'hlslcc'))):
        run([cmake, '--build', build_dir, '-j', str(os.cpu_count() or 4)], quiet=True)
    # the app's executable: rebuilds the app if the game changed, then starts Unity's player
    src, tbd = os.path.join(TOOLS, 'launcher', 'launcher.m'), os.path.join(TOOLS, 'launcher', 'UnityPlayer.tbd')
    if not up_to_date(paths.LAUNCHER, [src, tbd]):
        run(['xcrun', 'clang', '-fobjc-arc', '-O2', '-mmacosx-version-min=11.0', '-framework', 'Foundation', src,
             '-Xlinker', '-weak_library', '-Xlinker', tbd, '-o', paths.LAUNCHER], quiet=True)


def project_inputs(project, managed):
    """Fingerprint of what a C# project is built from: the downloads, its files and the game assemblies it uses (by
    size and date), so that a game update only rebuilds the projects using what changed."""
    h = hashlib.sha256()
    for d in (os.path.dirname(paths.DOTNET), paths.UNITY_MONO, paths.UNITY_MAC, paths.STEAMWORKS_NET, paths.INPUT_SYSTEM):
        h.update((read_text(os.path.join(d, '.dep')) or '-').encode())
    files = [os.path.join(ROOT, 'Directory.Build.props')]
    # a rebuilt library also depends on the tool that writes its MonoScript table
    for d in (project, 'tools/csharp/monoscripttypes') if project in MONOSCRIPT_TABLES else (project,):
        for root, dirs, names in os.walk(os.path.join(ROOT, d)):
            dirs[:] = sorted(x for x in dirs if x not in ('obj', 'bin', 'out'))
            files += [os.path.join(root, n) for n in sorted(names) if n != 'UnitySourceGeneratedAssemblyMonoScriptTypes_v1.cs']
    for p in files:
        h.update(('%s %s\n' % (os.path.relpath(p, ROOT), file_digest(p))).encode())
    game = set()
    for p in glob.glob(os.path.join(ROOT, project, '*.csproj')):
        game.update(re.findall(r'\$\(GameManaged\)/([^<]+?\.dll)', read_text(p)))
    if project in MONOSCRIPT_TABLES:
        game.add(MONOSCRIPT_TABLES[project])
    for name in sorted(game):
        h.update(('%s %s\n' % (name, file_id(os.path.join(managed, name)))).encode())
    return h.hexdigest()


def build_assemblies(game, bench):
    managed = os.path.join(game, DATA_NAME, 'Managed')
    # C# helpers, and Steamworks.NET / Input System rebuilt for macOS; dotnet takes seconds even when there's nothing
    # to do, so a project is only built when something it's built from changed
    stamp = os.path.join(WORK, '.projects.stamp')
    stamps = read_stamps(stamp)
    projects = [p for p in CS_PROJECTS if bench or p != BENCH_PROJECT]
    wanted = {p: project_inputs(p, managed) for p in projects}
    stale = [p for p in projects if stamps.get(p) != wanted[p] or not glob.glob(os.path.join(ROOT, p, 'out', '*.dll'))]
    if stale:
        env = dotnet_env()
        try:
            for project in stale:  # in CS_PROJECTS order: monoscripttypes, which writes the tables, comes first
                stamps.pop(project, None)
                write_stamps(stamp, stamps)
                d = os.path.join(ROOT, project)
                if project in MONOSCRIPT_TABLES:
                    run([paths.DOTNET, os.path.join(TOOLS, 'csharp/monoscripttypes/out/monoscripttypes.dll'),
                         os.path.join(managed, MONOSCRIPT_TABLES[project]),
                         os.path.join(d, 'UnitySourceGeneratedAssemblyMonoScriptTypes_v1.cs')], quiet=True, env=env)
                run([paths.DOTNET, 'build', d, '-c', 'Release', '-o', os.path.join(d, 'out'), '-nologo', '-v:q',
                     '-p:GameManaged=' + managed], quiet=True, env=env)
                stamps[project] = wanted[project]
                write_stamps(stamp, stamps)
        finally:
            run([paths.DOTNET, 'build-server', 'shutdown'], quiet=True, env=env)
        print('    built ' + ', '.join(os.path.basename(p) for p in stale))
    # Assembly-CSharp with its ComputeShader.SetBuffer calls routed through MacPortCompat, and the port assemblies
    # that replace or join the game's (assemble_app.py copies everything in work/managed)
    out = os.path.join(WORK, 'managed')
    src, dst = os.path.join(managed, 'Assembly-CSharp.dll'), os.path.join(out, 'Assembly-CSharp.dll')
    ilpatch = os.path.join(TOOLS, 'csharp/ilpatch/out/ilpatch.dll')
    compat = os.path.join(TOOLS, 'csharp/MacPortCompat/out/MacPortCompat.dll')
    stamp = os.path.join(out, '.ilpatch.stamp')
    inputs = ' '.join(file_digest(p) for p in (src, ilpatch, compat))
    if read_text(stamp) != inputs or not os.path.exists(dst):
        os.makedirs(out, exist_ok=True)
        print('    ' + run_logged([paths.DOTNET, ilpatch, src, dst, managed, compat], os.path.join(out, 'ilpatch.log')))
        write_text(stamp, inputs)
    elif not stale:
        print('    up to date')
    for dll in (compat, os.path.join(ROOT, 'work/build/inputsystem/out/Unity.InputSystem.dll')):
        shutil.copy2(dll, out)


# --- shaders

def convert_shaders(game, force, main_branch_version):
    data = os.path.join(game, DATA_NAME)
    out = os.path.join(WORK, 'patched')
    # A file is converted again when it changed or the converter did (or the system Metal compiler, which validates
    # the shaders); globalgamemanagers, which a game update always changes (version), only gets the settings patch
    h = hashlib.sha256(('macOS %s\n' % platform.mac_ver()[0]).encode())
    for rel in CONVERTER:
        h.update(('%s %s\n' % (rel, file_digest(os.path.join(ROOT, rel)))).encode())
    wanted = {rel: '%s %s' % (h.hexdigest(), file_id(os.path.join(data, rel))) for rel in CONVERTED}
    settings_args = ['--main-branch-version'] if main_branch_version else []
    wanted['globalgamemanagers'] = '%s %s %s' % (file_digest(tool('patch_settings.py')),
                                                 file_id(os.path.join(data, 'globalgamemanagers')), settings_args)
    stamp = os.path.join(out, '.stamp')
    stamps = read_stamps(stamp)
    stale = [rel for rel, fingerprint in wanted.items()
             if force or stamps.get(rel) != fingerprint or not os.path.exists(os.path.join(out, rel))]
    if not stale:
        print('    up to date')
        return
    for rel in stale:  # until it's converted again
        stamps.pop(rel, None)
    write_stamps(stamp, stamps)
    os.makedirs(os.path.join(out, 'Resources'), exist_ok=True)
    py = [sys.executable, '-I']
    files = [rel for rel in CONVERTED if rel in stale]
    if files:
        print('    converting %s (a few minutes the first time, after that only new shaders are compiled)' %
              ('the game data' if len(files) == len(CONVERTED) else ', '.join(files)), flush=True)
        run_logged(py + [tool('port_shaders.py'), data, out, *files, '--validate'], os.path.join(out, 'shaders.log'),
                   show=True)
        stamps.update((rel, wanted[rel]) for rel in files)
        write_stamps(stamp, stamps)
    if 'globalgamemanagers' in stale:
        print('    ' + run_logged(py + [tool('patch_settings.py'), os.path.join(data, 'globalgamemanagers'),
                                        os.path.join(out, 'globalgamemanagers'), *settings_args],
                                       os.path.join(out, 'settings.log')))
        stamps['globalgamemanagers'] = wanted['globalgamemanagers']
        write_stamps(stamp, stamps)


# --- icon

def make_icon(game):
    exe, icns = os.path.join(game, 'Schedule I.exe'), os.path.join(WORK, 'icon', 'ScheduleI.icns')
    if up_to_date(icns, [exe, tool('make_icon.py')]):
        print('    up to date')
        return
    try:
        run([sys.executable, '-I', tool('make_icon.py'), exe, icns], quiet=True)
    except BuildError as e:
        print('    warning: no app icon (%s), using Unity\'s default' % e)


# --- app

def game_files(game):
    """The game's data files as "size mtime_ns path" lines (without dot files such as .DS_Store): what the app's
    launcher compares the game folder with before starting the game."""
    data = os.path.join(game, DATA_NAME)
    lines = []
    for root, dirs, files in os.walk(data):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for f in files:
            if not f.startswith('.'):
                st = os.stat(os.path.join(root, f))
                lines.append((os.path.relpath(os.path.join(root, f), data), st.st_size, st.st_mtime_ns))
    return ''.join('%d %d %s\n' % (size, mtime, rel) for rel, size, mtime in sorted(lines))


def app_inputs(game, args, game_list, update):
    """Fingerprint of everything the app is assembled from."""
    h = hashlib.sha256(repr((game, args.bench, args.development, args.no_burst, args.main_branch_version)).encode())
    h.update((game_list + json.dumps(update, sort_keys=True)).encode())
    for d in (paths.UNITY_MAC, paths.UNITY_MONO, paths.STEAMWORKS_NET, paths.BURST):
        h.update((read_text(os.path.join(d, '.dep')) or '-').encode())
    for rel in APP_INPUTS + (BENCH_APP_INPUTS if args.bench else ()):
        p = os.path.join(ROOT, rel)
        h.update(('%s %s\n' % (rel, file_digest(p) if os.path.exists(p) else '-')).encode())
    for d in (os.path.join(WORK, 'managed'), os.path.join(WORK, 'patched')):
        for root, dirs, files in os.walk(d):
            dirs.sort()
            for f in sorted(files):
                st = os.stat(os.path.join(root, f))
                h.update(('%s %d %d\n' % (os.path.relpath(os.path.join(root, f), d), st.st_size, st.st_mtime_ns)).encode())
    return h.hexdigest()


def assemble(game, args):
    app = os.path.realpath(args.out or os.path.join(paths.BUILD, 'Schedule I Bench.app' if args.bench else 'Schedule I.app'))
    stamp = os.path.join(WORK, 'app-stamps', hashlib.sha1(app.encode()).hexdigest()[:16])
    game_list = game_files(game)
    # What the app's launcher needs to rebuild it (./build.sh --game Game --out <app>) when the game folder no longer
    # matches the files listed in the app; none for the benchmark app, which bench.py runs as built
    update = None if args.bench else {
        'Game': game, 'Repository': ROOT,
        'BuildArguments': [flag for flag, on in (('--development', args.development), ('--no-burst', args.no_burst),
                                                 ('--skip-version-check', args.skip_version_check),
                                                 ('--main-branch-version', args.main_branch_version)) if on]}
    fingerprint = app_inputs(game, args, game_list, update)
    if not args.force and read_text(stamp) == fingerprint and os.path.exists(os.path.join(app, 'Contents', 'Info.plist')):
        print('    up to date: ' + app)
        return
    if os.path.exists(stamp):
        os.remove(stamp)
    listing = os.path.join(WORK, 'game-files.txt')
    write_text(listing, game_list)
    cmd = [sys.executable, '-I', tool('assemble_app.py'), '--game', game, '--out', app, '--game-files', listing]
    cmd += [flag for flag, on in (('--bench', args.bench), ('--development', args.development),
                                  ('--no-burst', args.no_burst)) if on]
    if update:
        cmd += ['--update', json.dumps(update)]
    run(cmd)
    write_text(stamp, fingerprint)


def main():
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(prog='./build.sh', description='Builds build/Schedule I.app from your Windows copy of '
                                 'Schedule I (Steam branch "alternate"). Downloads everything else into deps/.')
    ap.add_argument('--game', help='the game folder (contains "Schedule I_Data"); by default the one of the last build, '
                    'one found in your Steam libraries, or you are asked to locate it')
    ap.add_argument('--out', help='app to build (default: build/Schedule I.app)')
    ap.add_argument('--bench', action='store_true', help='build the benchmark app (see tools/py/bench.py)')
    ap.add_argument('--development', action='store_true', help="use Unity's development player (more logging)")
    ap.add_argument('--no-burst', action='store_true', help='skip the Burst AOT compile (Burst jobs then run as managed code)')
    ap.add_argument('--force', action='store_true', help='convert the game data and assemble the app again even if '
                    'they are up to date')
    ap.add_argument('--skip-version-check', action='store_true',
                    help="build even if the game's Unity/package versions differ from the pinned ones")
    ap.add_argument('--main-branch-version', action='store_true',
                    help='report the game version without its " Alternate" label, so that multiplayer works with '
                    "players on Steam's default branch of the same version (untested by the developer)")
    args = ap.parse_args()
    t0 = time.time()
    try:
        print('==> game')
        lock = lock_work()  # noqa: F841 (held until the build ends)
        game = find_game(args.game)
        print('    ' + game)
        check_game(game, args.skip_version_check)
        print('==> deps')
        missing, downloads = missing_deps(), None
        if missing:
            free = shutil.disk_usage(ROOT).free / 1e9
            if free < 11:
                print('    warning: only %.0f GB free; a first build needs about 11 GB' % free)
            print('    downloading %.1f GB in the background' % (sum(d[3] for d in missing) / 1e9))
            downloads = Downloads(missing)
        else:
            print('    up to date')
        print('==> tools')
        build_tools()
        print('==> shaders')
        convert_shaders(game, args.force, args.main_branch_version)
        if downloads:
            print('==> deps')
            downloads.wait()
        print('==> assemblies')
        build_assemblies(game, args.bench)
        print('==> icon')
        make_icon(game)
        print('==> app')
        assemble(game, args)
    except BuildError as e:
        stop_children()
        sys.exit('\nerror: %s' % e)
    except KeyboardInterrupt:
        stop_children(grace=20)
        sys.exit(130)
    print('done in %d:%02d. Start Steam, then open the app.' % divmod(int(time.time() - t0), 60))


if __name__ == '__main__':
    main()
