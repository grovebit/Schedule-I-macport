"""Rewrites Unity 2022.3 Shader objects from D3D11-only to Metal-only.

For every D3D11 vertex/fragment subprogram the DXBC is translated to MSL (see convert.py),
the per-platform blob is rebuilt for platform 14 (Metal) and the parsed form is updated.
"""
import hashlib
import json
import struct

import lz4.block
from UnityPy.helpers import TypeTreeHelper

from convert import ConvertError, SubprogramConverter
from ushader import get_entry, read_table

PLATFORM_D3D11, PLATFORM_METAL = 4, 14
SEGMENT_LIMIT = 8 * 1024 * 1024
STAGES = (('progVertex', 'vs'), ('progFragment', 'fs'), ('progGeometry', 'gs'),
          ('progHull', 'hs'), ('progDomain', 'ds'), ('progRayTracing', 'rt'))
EMPTY_COMMON = {'m_VectorParams': [], 'm_MatrixParams': [], 'm_TextureParams': [], 'm_BufferParams': [],
                'm_ConstantBuffers': [], 'm_ConstantBufferBindings': [], 'm_UAVParams': [], 'm_Samplers': []}

_write_value = TypeTreeHelper.write_value


def _write_value_bulk(value, node, writer, config):
    """UnityPy writes byte arrays one element at a time, which made saving the converted shaders (their blobs) slow:
    bytes values of UInt8 arrays are written at once instead, with the same result."""
    if isinstance(value, (bytes, bytearray)) and node.m_Children and node.m_Children[0].m_Type == 'Array' \
            and node.m_Children[0].m_Children[1].m_Type == 'UInt8':
        writer.write_int(len(value))
        writer.write_bytes(bytes(value))
        if TypeTreeHelper.metaflag_is_aligned(node.m_MetaFlag) or \
                TypeTreeHelper.metaflag_is_aligned(node.m_Children[0].m_MetaFlag):
            writer.align_stream()
        return
    _write_value(value, node, writer, config)


TypeTreeHelper.write_value = _write_value_bulk


class BlobWriter:
    def __init__(self):
        self.entries = []
        self.index = {}

    def add(self, data):
        key = hashlib.sha1(data).digest()
        i = self.index.get(key)
        if i is None:
            i = len(self.entries)
            self.index[key] = i
            self.entries.append(data)
        return i

    def segments(self):
        """Unity layout: one segment when small, else table-only segment 0 + <=8MiB data segments."""
        n = len(self.entries)
        table_size = 4 + 12 * n
        total = table_size + sum(len(e) for e in self.entries)
        table = bytearray(table_size)
        struct.pack_into('<i', table, 0, n)
        if total <= SEGMENT_LIMIT:
            seg = bytearray(table)
            for i, e in enumerate(self.entries):
                struct.pack_into('<iii', seg, 4 + 12 * i, len(seg), len(e), 0)
                seg += e
            return [bytes(seg)]
        segs = [None]
        cur = bytearray()
        for i, e in enumerate(self.entries):
            if cur and len(cur) + len(e) > SEGMENT_LIMIT:
                segs.append(bytes(cur))
                cur = bytearray()
            struct.pack_into('<iii', table, 4 + 12 * i, len(cur), len(e), len(segs))
            cur += e
        if cur:
            segs.append(bytes(cur))
        segs[0] = bytes(table)
        return segs


def pass_has_unsupported_stage(ps):
    for key, stage in STAGES:
        if stage in ('gs', 'hs', 'ds', 'rt'):
            if any(len(g) for g in ps[key]['m_PlayerSubPrograms']):
                return stage
    return None


def d3d11_segments(tt):
    """The decompressed segments of a Shader's D3D11 blob and their entry table."""
    pi = tt['platforms'].index(PLATFORM_D3D11)
    blob = bytes(tt['compressedBlob'])
    segs = [lz4.block.decompress(blob[o:o + c], uncompressed_size=d)
            for o, c, d in zip(tt['offsets'][pi], tt['compressedLengths'][pi], tt['decompressedLengths'][pi])]
    return segs, read_table(segs[0])


def shader_items(tt, segs, tab):
    """The unique vertex/fragment subprograms of a Shader to convert, in traversal order:
    [(key, subprogram, params, stage, common params, names, shader name)]"""
    pf = tt['m_ParsedForm']
    items, keys = [], set()
    for ss in pf['m_SubShaders']:
        for ps in ss['m_Passes']:
            names = {idx: n for n, idx in ps['m_NameIndices']}
            if pass_has_unsupported_stage(ps):
                continue
            for key, stage in STAGES[:2]:
                prog = ps[key]
                for g in range(len(prog['m_PlayerSubPrograms'])):
                    pbi = prog['m_ParameterBlobIndices'][g] if g < len(prog['m_ParameterBlobIndices']) else []
                    for sp, pb in zip(prog['m_PlayerSubPrograms'][g], pbi):
                        if sp['m_GpuProgramType'] not in (15, 16, 17, 18):
                            continue
                        ck = (sp['m_BlobIndex'], pb, stage, id(prog))
                        if ck in keys:
                            continue
                        keys.add(ck)
                        items.append((ck, get_entry(segs, tab, sp['m_BlobIndex']), get_entry(segs, tab, pb), stage,
                                      prog['m_CommonParameters'], names, pf['m_Name']))
    return items


def msl_hash(stage, src):
    return hashlib.sha1(('%s\n%s' % (stage, src)).encode()).hexdigest()


_worker_conv = None
_worker_known = frozenset()


def worker_init(verdicts=None):
    """verdicts: JSON file of earlier validation results ({MSL hash: '' if it compiles, else the error}); convert_item()
    doesn't return the sources known to compile."""
    global _worker_conv, _worker_known
    _worker_conv = SubprogramConverter()
    try:
        with open(verdicts) as f:
            _worker_known = frozenset(h for h, msg in json.load(f).items() if not msg)
    except (TypeError, OSError, ValueError):
        pass


def convert_item(item):
    """Pool worker: an item of shader_items() -> (key, (metal subprogram, metal params, MSL hash, MSL or None) | None,
    error)."""
    key, sub, params, stage, common, names, _ = item
    try:
        msub, mpar, info = _worker_conv.convert(sub, params, stage, common, names)
        h = msl_hash(stage, info['src'])
        return key, (msub, mpar, h, None if h in _worker_known else info['src']), None
    except ConvertError as e:
        return key, None, str(e)[:200]


VALIDATE_BATCH = 50  # sources the Metal compiler checks at once (mtlcheck.compile_batch)


def validate_batch(items):
    """Pool worker: [(MSL hash, stage, MSL)] -> [(MSL hash, '' if it compiles, else the compiler's message)]."""
    import mtlcheck
    results = mtlcheck.compile_batch([(src, stage) for _, stage, src in items])
    return [(h, '' if rc == 0 else (msg[:3000] or 'unknown error')) for (h, _, _), (rc, msg) in zip(items, results)]


def compress_segment(segment):
    return lz4.block.compress(segment, store_size=False, mode='high_compression')


def convert_shader(tt, conv=None, validate=None, log=print, results=None, failures=None, map_=map):
    """Converts a Shader typetree dict in place. Returns stats dict.
    results: the subprograms of shader_items() converted (and validated) by the caller, in the same order:
    [(key, (metal subprogram, metal params) | None, error, failure)]; anything else is converted here.
    map_: used to compress the blob's segments, e.g. a thread pool's map (LZ4 releases the GIL)."""
    conv = conv or SubprogramConverter()
    pf = tt['m_ParsedForm']
    name = pf['m_Name']
    stats = dict(name=name, converted=0, failed=0, dropped_stage=0, errors={})
    if PLATFORM_D3D11 not in tt['platforms']:
        stats['skipped'] = 'no d3d11'
        return stats
    segs, tab = d3d11_segments(tt)
    out = BlobWriter()
    cache = {}
    for ck, res, err, fail in results or ():  # in traversal order so the blob layout is deterministic
        if res is None:
            cache[ck] = None
            stats['errors'][err] = stats['errors'].get(err, 0) + 1
            if fail is not None and failures is not None:
                failures.append(fail)
        else:
            cache[ck] = (out.add(res[0]), out.add(res[1]))

    for ss in pf['m_SubShaders']:
        for ps in ss['m_Passes']:
            names = {idx: n for n, idx in ps['m_NameIndices']}
            unsupported = pass_has_unsupported_stage(ps)
            for key, stage in STAGES:
                prog = ps[key]
                for g in range(len(prog['m_PlayerSubPrograms'])):
                    grp = prog['m_PlayerSubPrograms'][g]
                    pbi = prog['m_ParameterBlobIndices'][g] if g < len(prog['m_ParameterBlobIndices']) else []
                    new_grp, new_pbi = [], []
                    for sp, pb in zip(grp, pbi):
                        t = sp['m_GpuProgramType']
                        if t not in (15, 16, 17, 18, 19, 20, 21, 22):
                            continue
                        if unsupported or stage not in ('vs', 'fs'):
                            # Metal has no geometry shaders; tessellation needs compute-based emulation.
                            stats['dropped_stage'] += 1
                            continue
                        ck = (sp['m_BlobIndex'], pb, stage, id(prog))
                        if ck not in cache:
                            try:
                                msub, mpar, info = conv.convert(get_entry(segs, tab, sp['m_BlobIndex']),
                                                                get_entry(segs, tab, pb), stage,
                                                                prog['m_CommonParameters'], names)
                                if validate is not None:
                                    validate(info['src'], stage, name)
                                cache[ck] = (out.add(msub), out.add(mpar))
                            except ConvertError as e:
                                cache[ck] = None
                                msg = str(e)[:200]
                                stats['errors'][msg] = stats['errors'].get(msg, 0) + 1
                        r = cache[ck]
                        if r is None:
                            stats['failed'] += 1
                            continue
                        new_sp = dict(sp)
                        new_sp['m_BlobIndex'] = r[0]
                        new_sp['m_GpuProgramType'] = 23 if stage == 'vs' else 24
                        new_grp.append(new_sp)
                        new_pbi.append(r[1])
                        stats['converted'] += 1
                    prog['m_PlayerSubPrograms'][g] = new_grp
                    if g < len(prog['m_ParameterBlobIndices']):
                        prog['m_ParameterBlobIndices'][g] = new_pbi
                prog['m_CommonParameters'] = {k: [] for k in prog['m_CommonParameters']} if prog['m_CommonParameters'] else EMPTY_COMMON
            # passes needing geometry/tessellation stages are excluded so Unity falls back (e.g. Stylized Water 2 tessellation)
            ps['m_Platforms'] = [] if unsupported else [PLATFORM_METAL]

    segments = out.segments()
    comp = list(map_(compress_segment, segments))
    offsets, o = [], 0
    for c in comp:
        offsets.append(o)
        o += len(c)
    tt['platforms'] = [PLATFORM_METAL]
    tt['offsets'] = [offsets]
    tt['compressedLengths'] = [[len(c) for c in comp]]
    tt['decompressedLengths'] = [[len(sg) for sg in segments]]
    tt['stageCounts'] = [2]
    tt['compressedBlob'] = b''.join(comp)
    stats['entries'] = len(out.entries)
    stats['blob'] = len(tt['compressedBlob'])
    return stats
