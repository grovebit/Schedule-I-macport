"""Burst AOT compilation for the macOS arm64 player (what the Unity editor does during a player build).

Compiles the [BurstCompile] jobs/functions of the player assemblies into lib_burst_generated.bundle with
the Burst version the game shipped with (1.8.21), running the package's bcl.exe under the editor's Mono.
The function hashes match the Windows lib_burst_generated.dll, so the player finds the native code.

usage: python -I burst_aot.py <player Data folder>   (prints the path of the cached bundle)
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paths  # noqa: E402

BURST_RUNTIME = paths.BURST + '/.Runtime'
MONO = paths.UNITY_MONO + '/bin/mono'
KEY_FOLDER = paths.UNITY_MAC
CACHE = paths.WORK + '/burst'
TARGET = 'ARMV8A_AARCH64_HALFFP'  # the editor's choice for Apple Silicon players
SCRIPTING_ASSEMBLY_USER = 16
NOT_BURST_ROOTS = {'MacPortBench.dll', 'MacPortRuntime.dll'}  # port helpers: no Burst code, keep the cache shared


def root_assemblies(data_dir):
    with open(os.path.join(data_dir, 'ScriptingAssemblies.json')) as f:
        d = json.load(f)
    managed = os.path.join(data_dir, 'Managed')
    return sorted(os.path.join(managed, n) for n, t in zip(d['names'], d['types'])
                  if t == SCRIPTING_ASSEMBLY_USER and n not in NOT_BURST_ROOTS and os.path.exists(os.path.join(managed, n)))


def compile_burst(data_dir, log=print):
    """Returns the path of lib_burst_generated.bundle for a player Data folder (cached by input hash)."""
    roots = root_assemblies(data_dir)
    h = hashlib.sha1()
    with open(os.path.join(BURST_RUNTIME, 'version.txt'), 'rb') as f:
        h.update(f.read())
    h.update(TARGET.encode())
    for p in roots:
        h.update(os.path.basename(p).encode())
        with open(p, 'rb') as f:
            h.update(hashlib.sha1(f.read()).digest())
    out_dir = os.path.join(CACHE, h.hexdigest()[:16])
    lib = os.path.join(out_dir, 'lib_burst_generated.bundle')
    if os.path.exists(lib):
        return lib

    managed = os.path.join(data_dir, 'Managed')
    with tempfile.TemporaryDirectory(prefix='burst_aot_') as tmp:
        options = ['--key-folder=' + KEY_FOLDER, '--decode-folder=' + os.path.join(tmp, 'decode'), '--platform=macOS']
        options += ['--root-assembly=' + p for p in roots]
        options += ['--output=' + os.path.join(tmp, 'out', 'lib_burst_generated'), '--temp-folder=' + os.path.join(tmp, 'temp'),
                    '--target=' + TARGET, '--assembly-folder=' + managed,
                    '--generate-link-xml=' + os.path.join(tmp, 'burst.link.xml'), '--opt-level=2']
        resp = os.path.join(tmp, 'options.rsp')
        with open(resp, 'w') as f:
            f.write('\n'.join(options) + '\n')
        log('burst: compiling %d assemblies for %s' % (len(roots), TARGET))
        r = subprocess.run([MONO, os.path.join(BURST_RUNTIME, 'bcl.exe'), '+burstc', '@' + resp],
                           cwd=tmp, capture_output=True, text=True)
        built = os.path.join(tmp, 'out', 'lib_burst_generated.bundle')
        if r.returncode != 0 or not os.path.exists(built):
            raise RuntimeError('burst compilation failed (%d):\n%s%s' % (r.returncode, r.stdout[-4000:], r.stderr[-4000:]))
        os.makedirs(out_dir, exist_ok=True)
        shutil.copy2(os.path.join(tmp, 'out', 'lib_burst_generated.txt'), out_dir)
        shutil.copy2(built, lib)  # last, so an interrupted run is never mistaken for a cache hit
    return lib


if __name__ == '__main__':
    print(compile_burst(os.path.abspath(sys.argv[1])))  # the compiler runs in a temporary folder
