"""Patches globalgamemanagers for the macOS player (graphics API list: D3D11 -> Metal).

With --main-branch-version, also drops the " Alternate" label from the game version (PlayerSettings.bundleVersion,
Application.version), which multiplayer lobbies compare as text: the port can then play with players on Steam's
default branch of the same version (an IL2CPP build of the same game, which the developer doesn't test together).

usage: python -I patch_settings.py <input globalgamemanagers> <output> [--main-branch-version]
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import UnityPy  # noqa: E402

GFX_RENDERER_METAL = 16


def main():
    src, dst = sys.argv[1], sys.argv[2]
    main_branch_version = '--main-branch-version' in sys.argv[3:]
    env = UnityPy.load(src)
    sf = next(iter(env.files.values()))
    for obj in env.objects:
        if obj.type.name == 'BuildSettings':
            tt = obj.read_typetree()
            print('BuildSettings.m_GraphicsAPIs:', tt['m_GraphicsAPIs'], '->', [GFX_RENDERER_METAL])
            tt['m_GraphicsAPIs'] = [GFX_RENDERER_METAL]
            obj.save_typetree(tt)
        elif obj.type.name == 'PlayerSettings' and main_branch_version:
            tt = obj.read_typetree()
            version = re.sub(r'\s+alternate$', '', tt['bundleVersion'], flags=re.IGNORECASE)
            print('PlayerSettings.bundleVersion:', repr(tt['bundleVersion']), '->', repr(version))
            tt['bundleVersion'] = version
            obj.save_typetree(tt)
    with open(dst, 'wb') as f:
        f.write(sf.save())
    print('wrote', dst)


if __name__ == '__main__':
    main()
