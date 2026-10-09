"""Patches globalgamemanagers for the macOS player (graphics API list: D3D11 -> Metal).

usage: python -I patch_settings.py <input globalgamemanagers> <output>
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import UnityPy  # noqa: E402

GFX_RENDERER_METAL = 16


def main():
    src, dst = sys.argv[1], sys.argv[2]
    env = UnityPy.load(src)
    sf = next(iter(env.files.values()))
    for obj in env.objects:
        if obj.type.name == 'BuildSettings':
            tt = obj.read_typetree()
            print('BuildSettings.m_GraphicsAPIs:', tt['m_GraphicsAPIs'], '->', [GFX_RENDERER_METAL])
            tt['m_GraphicsAPIs'] = [GFX_RENDERER_METAL]
            obj.save_typetree(tt)
    with open(dst, 'wb') as f:
        f.write(sf.save())
    print('wrote', dst)


if __name__ == '__main__':
    main()
