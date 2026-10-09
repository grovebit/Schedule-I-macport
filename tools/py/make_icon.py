"""Creates the app icon (.icns) from the icon resource of the game's Windows executable.

usage: python -I make_icon.py <Schedule I.exe> <out.icns>
"""
import io
import os
import struct
import subprocess
import sys
import tempfile

import pefile
from PIL import Image

RT_ICON, RT_GROUP_ICON = 3, 14
ICONSET_SIZES = (16, 32, 128, 256, 512)  # each also at @2x


def largest_icon(exe):
    """The largest image of the executable's first icon group."""
    pe = pefile.PE(exe, fast_load=True)
    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_RESOURCE']])
    resources = {e.id: e for e in pe.DIRECTORY_ENTRY_RESOURCE.entries}

    def data(entry):
        s = entry.directory.entries[0].data.struct  # first language
        return pe.get_data(s.OffsetToData, s.Size)

    group = data(resources[RT_GROUP_ICON].directory.entries[0])
    count = struct.unpack_from('<H', group, 4)[0]
    # GRPICONDIRENTRY: width, height, colors, reserved, planes, bit count, bytes in resource, icon id (0 = 256 px)
    entries = [struct.unpack_from('<BBBBHHIH', group, 6 + 14 * i) for i in range(count)]
    best = max(entries, key=lambda e: (e[0] or 256, e[5]))
    image = data(next(e for e in resources[RT_ICON].directory.entries if e.id == best[7]))
    # a single-image .ico around it, which Pillow decodes (PNG, or BMP with alpha)
    ico = struct.pack('<HHH', 0, 1, 1) + struct.pack('<BBBBHHII', *best[:6], len(image), 22) + image
    return Image.open(io.BytesIO(ico)).convert('RGBA')


def main():
    exe, icns = sys.argv[1], sys.argv[2]
    img = largest_icon(exe)
    with tempfile.TemporaryDirectory() as tmp:
        iconset = os.path.join(tmp, 'icon.iconset')
        os.mkdir(iconset)
        for size in ICONSET_SIZES:
            for scale, suffix in ((1, ''), (2, '@2x')):
                img.resize((size * scale, size * scale), Image.Resampling.LANCZOS).save(
                    os.path.join(iconset, 'icon_%dx%d%s.png' % (size, size, suffix)))
        os.makedirs(os.path.dirname(icns), exist_ok=True)
        subprocess.run(['iconutil', '-c', 'icns', '-o', icns, iconset], check=True)
    print('wrote %s (from a %dx%d icon)' % (icns, img.width, img.height))


if __name__ == '__main__':
    main()
