"""D3D11 (DXBC) -> Metal conversion of Unity 2022.3 shader subprograms.

Unity strips the RDEF reflection chunk from shipped DXBC, so we rebuild one from
Unity's own parameter blobs (names, cbuffer layouts, binding slots), run it through
HLSLcc (the same translator Unity uses for Metal), and emit Unity's Metal subprogram
and parameter-blob formats.
"""
import re
import struct

import dxbc2metal as d2m
from ushader import BLOB_VERSION, parse_params, parse_subprog, write_params, write_subprog

# ---------------------------------------------------------------- constants
OP_DCL_RESOURCE, OP_DCL_CB, OP_DCL_SAMPLER = 88, 89, 90
OP_DCL_UAV_TYPED, OP_DCL_UAV_RAW, OP_DCL_UAV_STRUCT = 156, 157, 158
OP_DCL_RES_RAW, OP_DCL_RES_STRUCT = 161, 162

# ShaderGpuProgramType
GPU_DX11_VS = (15, 16)
GPU_DX11_PS = (17, 18)
GPU_DX11_GS = (19, 20)
GPU_DX11_HS, GPU_DX11_DS = 21, 22
GPU_METAL_VS, GPU_METAL_FS = 23, 24

# D3D_SHADER_VARIABLE_CLASS / TYPE
SVC_SCALAR, SVC_VECTOR, SVC_MATRIX_ROWS, SVC_MATRIX_COLUMNS, SVC_OBJECT, SVC_STRUCT = range(6)
SVT_VOID, SVT_BOOL, SVT_INT, SVT_FLOAT = 0, 1, 2, 3
SVT_UINT = 19
UNITY_PARAM_TO_SVT = {0: SVT_FLOAT, 1: SVT_INT, 2: SVT_BOOL, 5: SVT_UINT}

# D3D_SHADER_INPUT_TYPE
SIT_CBUFFER, SIT_TBUFFER, SIT_TEXTURE, SIT_SAMPLER, SIT_UAV_RWTYPED, SIT_STRUCTURED = 0, 1, 2, 3, 4, 5
SIT_UAV_RWSTRUCTURED, SIT_BYTEADDRESS, SIT_UAV_RWBYTEADDRESS = 6, 7, 8
SIT_UAV_RWSTRUCTURED_WITH_COUNTER = 11
# D3D_SRV_DIMENSION from the DXBC resource dimension token
DXBC_DIM_TO_SRV = {1: 1, 2: 2, 3: 4, 4: 6, 5: 8, 6: 9, 7: 3, 8: 5, 9: 7, 10: 10, 11: 11, 12: 11}

HLSLCC_FLAGS = d2m.TRANSLATE_MATRICES | d2m.REMOVE_UNUSED_GLOBALS | d2m.METAL_PRESERVE_DX_CB_LAYOUT

# Unity VertexComponent: Vertex, Normal, Tangent, Color, TexCoord, TexCoord0..7, Attrib0..15
D3D_TARGET_SEMANTIC = {0: ('POSITION', 0), 1: ('NORMAL', 0), 2: ('TANGENT', 0), 3: ('COLOR', 0)}
for _i in range(8):
    D3D_TARGET_SEMANTIC[5 + _i] = ('TEXCOORD', _i)
METAL_ATTRIB0 = 13

# Metal program header flags (GpuProgramMetal +0x5c)
MTL_FLAG_DEFAULT = 0x1 | 0x4 | 0x20   # matches Unity 2022.3 output (37)
MTL_FLAG_LAYERED = 0x2                # vertex shader writes render_target_array_index


import os
DEBUG_DUMP = os.environ.get('MACPORT_DUMP_DXBC')


class ConvertError(Exception):
    pass


# ---------------------------------------------------------------- DXBC container
def dxbc_split(dxbc):
    if dxbc[:4] != b'DXBC':
        raise ConvertError('not a DXBC container')
    count = struct.unpack_from('<I', dxbc, 28)[0]
    offs = struct.unpack_from('<%dI' % count, dxbc, 32)
    chunks = []
    for o in offs:
        fourcc = dxbc[o:o + 4]
        size = struct.unpack_from('<I', dxbc, o + 4)[0]
        chunks.append((fourcc, dxbc[o + 8:o + 8 + size]))
    return dxbc[4:20], chunks


def dxbc_join(hash16, chunks):
    count = len(chunks)
    off = 32 + 4 * count
    offsets, body = [], bytearray()
    for fourcc, data in chunks:
        offsets.append(off + len(body))
        body += fourcc + struct.pack('<I', len(data)) + data
        while len(body) % 4:
            body.append(0)
    total = off + len(body)
    out = bytearray(b'DXBC' + hash16 + struct.pack('<III', 1, total, count))
    out += struct.pack('<%dI' % count, *offsets)
    out += body
    return bytes(out)


# ---------------------------------------------------------------- RDEF synthesis
class RdefBuilder:
    """Builds an SM5 RDEF chunk understood by HLSLcc's reflect.cpp."""

    def __init__(self, major, minor, prog_type):
        self.major, self.minor, self.prog_type = major, minor, prog_type
        self.resources = []   # (name, type, ret, dim, nsamples, bindpoint, bindcount, flags)
        self.cbuffers = []    # (name, size, cbtype, [vars])  var = dict(name, offset, size, type)

    def add_resource(self, name, rtype, ret, dim, nsamples, bind, count=1, flags=0):
        self.resources.append((name, rtype, ret, dim, nsamples, bind, count, flags))

    def add_cbuffer(self, name, size, variables, cbtype=0):
        self.cbuffers.append((name, size, cbtype, variables))

    def build(self):
        strings = bytearray()
        str_off = {}

        def s(x):
            if x not in str_off:
                str_off[x] = len(strings)
                strings.extend(x.encode('utf8') + b'\0')
            return str_off[x]

        # flatten types (each type: 36 bytes) and members (12 bytes each)
        types, members = [], []

        def add_type(t):
            idx = len(types)
            types.append(None)
            mem_idx = None
            if t.get('members'):
                mem_idx = len(members)
                members.extend([None] * len(t['members']))
                for j, m in enumerate(t['members']):
                    members[mem_idx + j] = (m['name'], add_type(m['type']), m['offset'])
            types[idx] = (t['cls'], t['vt'], t['rows'], t['cols'], t['elements'], len(t.get('members') or ()), mem_idx)
            return idx

        cb_vars = []
        for name, size, cbtype, variables in self.cbuffers:
            cb_vars.append([(v['name'], v['offset'], v['size'], add_type(v['type'])) for v in variables])

        header_size = 60
        res_off = header_size
        cb_off = res_off + 32 * len(self.resources)
        var_off = cb_off + 24 * len(self.cbuffers)
        nvars = sum(len(v) for v in cb_vars)
        var_size = 40 if self.major >= 5 else 24   # SM5 adds texture/sampler ranges to variable records
        type_off = var_off + var_size * nvars
        mem_off = type_off + 36 * len(types)
        str_base = mem_off + 12 * len(members)

        # header
        shader_kind = {0: 0xFFFF, 1: 0xFFFE, 2: 0x4753, 3: 0x4853, 4: 0x4453, 5: 0x4353}.get(self.prog_type, 0xFFFE)
        target = (shader_kind << 16) | (self.major << 8) | self.minor
        out = bytearray()
        out += struct.pack('<7I', len(self.cbuffers), cb_off if self.cbuffers else 0, len(self.resources),
                           res_off if self.resources else 0, target, 0x100, str_base + s('Unity macport RDEF'))
        out += b'RD11' + struct.pack('<7I', 60, 24, 32, 40, 36, 12, 0)
        assert len(out) == header_size
        for (name, rtype, ret, dim, ns, bind, count, flags) in self.resources:
            out += struct.pack('<8I', str_base + s(name), rtype, ret, dim, ns & 0xffffffff, bind, count, flags)
        vi = 0
        for (name, size, cbtype, _), vars_ in zip(self.cbuffers, cb_vars):
            out += struct.pack('<6I', str_base + s(name), len(vars_), var_off + var_size * vi if vars_ else 0, size, 0, cbtype)
            vi += len(vars_)
        for vars_ in cb_vars:
            for (vname, voff, vsize, tidx) in vars_:
                out += struct.pack('<6I', str_base + s(vname), voff, vsize, 2, type_off + 36 * tidx, 0)
                if var_size == 40:
                    out += struct.pack('<4I', 0xffffffff, 0, 0xffffffff, 0)
        for (cls, vt, rows, cols, elements, nmem, mem_idx) in types:
            out += struct.pack('<6H', cls, vt, rows, cols, elements, nmem)
            out += struct.pack('<I', mem_off + 12 * mem_idx if nmem else 0)
            out += struct.pack('<4I', 0, 0, 0, 0) + struct.pack('<I', 0)
        for (mname, tidx, moff) in members:
            out += struct.pack('<3I', str_base + s(mname), type_off + 36 * tidx, moff)
        assert len(out) == str_base
        out += strings
        while len(out) % 4:
            out.append(0)
        return bytes(out)


def param_type(p):
    """RDEF type for a Unity cbuffer param (dict from ushader.parse_params)."""
    vt = UNITY_PARAM_TO_SVT.get(p['type'], SVT_FLOAT)
    if p['isMatrix']:
        t = dict(cls=SVC_MATRIX_COLUMNS, vt=vt, rows=p['rows'], cols=p['cols'], elements=p['arraySize'])
        size = 16 * p['cols'] * max(1, p['arraySize'])
        if p['arraySize']:
            size = 16 * p['cols'] * p['arraySize']
        else:
            size = 16 * (p['cols'] - 1) + 4 * p['rows']
    else:
        cls = SVC_SCALAR if p['cols'] == 1 else SVC_VECTOR
        t = dict(cls=cls, vt=vt, rows=1, cols=p['cols'], elements=p['arraySize'])
        elem = 4 * p['cols']
        size = 16 * (p['arraySize'] - 1) + elem if p['arraySize'] else elem
    return t, size


def struct_type(st):
    members = []
    end = 0
    for p in sorted(st['params'], key=lambda q: q['index']):
        t, size = param_type(p)
        members.append(dict(name=p['name'].split('.')[-1], type=t, offset=p['index']))
        end = max(end, p['index'] + size)
    t = dict(cls=SVC_STRUCT, vt=SVT_VOID, rows=1, cols=max(1, st['size'] // 4), elements=st['arraySize'], members=members)
    size = st['size'] * max(1, st['arraySize'])
    return t, size


INLINE_FILTER = {0: 'Point', 1: 'Linear', 2: 'Trilinear'}
INLINE_WRAP = {0: 'Repeat', 1: 'Clamp', 2: 'Mirror', 3: 'MirrorOnce'}


def inline_sampler_name(state):
    """Name that HLSLcc's EmitInlineSampler parses back into the same state."""
    filt = INLINE_FILTER.get(state & 3, 'Linear')
    wu, wv, ww = (state >> 2) & 3, (state >> 4) & 3, (state >> 6) & 3
    if wu == wv == ww:
        wrap = INLINE_WRAP[wu]
    else:
        wrap = '%sU_%sV_%sW' % (INLINE_WRAP[wu], INLINE_WRAP[wv], INLINE_WRAP[ww])
    name = 'sampler_%s%s' % (filt, wrap)
    if state & 0x100:
        name += 'Compare'
    return name


# ---------------------------------------------------------------- common params
def merge_common(params, common, names):
    """Merges SerializedProgram.m_CommonParameters (typetree dict) into a parsed param blob."""
    if not common:
        return params
    nm = lambda i: names.get(i, '__name%d' % i)

    def vec(v):
        return dict(name=nm(v['m_NameIndex']), type=v['m_Type'], rows=1, cols=v['m_Dim'], isMatrix=0,
                    arraySize=v['m_ArraySize'], index=v['m_Index'])

    def mat(m):
        rows = m['m_RowCount']
        cols = m.get('m_ColumnCount', rows)
        return dict(name=nm(m['m_NameIndex']), type=m['m_Type'], rows=rows, cols=cols, isMatrix=1,
                    arraySize=m['m_ArraySize'], index=m['m_Index'])

    cbs = {cb['name']: cb for cb in params['cbs']}
    for ccb in common.get('m_ConstantBuffers', []):
        name = nm(ccb['m_NameIndex'])
        cb = cbs.get(name)
        if cb is None:
            cb = dict(name=name, size=ccb['m_Size'], params=[], structs=[])
            params['cbs'].append(cb)
            cbs[name] = cb
        have = {p['name'] for p in cb['params']}
        for v in ccb.get('m_VectorParams', []):
            p = vec(v)
            if p['name'] not in have:
                cb['params'].append(p)
        for m in ccb.get('m_MatrixParams', []):
            p = mat(m)
            if p['name'] not in have:
                cb['params'].append(p)
        for sp in ccb.get('m_StructParams', []):
            st = dict(name=nm(sp['m_NameIndex']), index=sp['m_Index'], arraySize=sp['m_ArraySize'],
                      size=sp['m_StructSize'], params=[vec(v) for v in sp.get('m_VectorMembers', [])] +
                      [mat(m) for m in sp.get('m_MatrixMembers', [])])
            cb['structs'].append(st)
    binds = params['binds']
    for b in common.get('m_ConstantBufferBindings', []):
        binds.append(dict(kind='cb', name=nm(b['m_NameIndex']), index=b['m_Index'], arraySize=0))
    for t in common.get('m_TextureParams', []):
        extra = (t['m_Dim'] << 1) | (1 if t['m_MultiSampled'] else 0)
        binds.append(dict(kind='tex', name=nm(t['m_NameIndex']), index=t['m_Index'], sampler=t['m_SamplerIndex'], extra=extra))
    for b in common.get('m_BufferParams', []):
        binds.append(dict(kind='buf', name=nm(b['m_NameIndex']), index=b['m_Index'], arraySize=0))
    for u in common.get('m_UAVParams', []):
        binds.append(dict(kind='uav', name=nm(u['m_NameIndex']), index=u['m_Index'], origIndex=u['m_OriginalIndex']))
    for smp in common.get('m_Samplers', []):
        binds.append(dict(kind='sampler', name='', bindPoint=smp['bindPoint'], sampler=smp['sampler']))
    # common vector/matrix params outside of cbuffers (old style, unused on DX11) are ignored
    return params


# ---------------------------------------------------------------- MSL post-processing
_REMAP_DECL = re.compile(r'#ifndef XLT_REMAP_O\n\t#define XLT_REMAP_O \{0, 1, 2, 3, 4, 5, 6, 7\}\n#endif\nconstexpr constant uint xlt_remap_o\[\] = XLT_REMAP_O;\n')
_REMAP_USE = re.compile(r'xlt_remap_o\[(\d+)\]')


def postprocess_msl(src):
    uses_remap_fc = False
    if 'xlt_remap_o[' in src:
        idx = sorted({int(m) for m in _REMAP_USE.findall(src)})
        decl = 'constant uint32_t rp_output_remap_mask [[ function_constant(1) ]];\n'
        for i in idx:
            decl += 'constant const uint rp_output_remap_%d = (rp_output_remap_mask >> %d) & 0xF;\n' % (i, 4 * i)
        src, n = _REMAP_DECL.subn(decl, src)
        if n != 1:
            raise ConvertError('unexpected xlt_remap_o declaration')
        src = _REMAP_USE.sub(lambda m: 'rp_output_remap_%s' % m.group(1), src)
        uses_remap_fc = True
    if 'xlt_remap_i[' in src:
        raise ConvertError('framebuffer fetch inputs are not supported')
    # Unity compiles with preserveInvariance and marks clip positions invariant (needed for depth prepass + ZTest Equal)
    src = src.replace('float4 mtl_Position [[ position ]];', 'float4 mtl_Position [[ position, invariant ]];')
    src = widen_fragment_outputs(src)
    return src, uses_remap_fc


_FRAG_OUT_STRUCT = re.compile(r'struct Mtl_FragmentOut\n\{\n(.*?)\n\};\n', re.S)
_COLOR_MEMBER = re.compile(r'^(\s*)(float|half|int|uint|short|ushort)([23]?) (\w+) \[\[ color\((.*?)\) \]\];$')


def widen_fragment_outputs(src):
    """Metal rejects pipelines whose fragment color output has fewer components than the attachment
    format (D3D11 allows it). Return 4-component colors, filling missing rgb with 0 and alpha with 1."""
    m = _FRAG_OUT_STRUCT.search(src)
    if not m:
        return src
    members = m.group(1).split('\n')
    narrow = []
    for line in members:
        cm = _COLOR_MEMBER.match(line)
        if cm and cm.group(3) != '4':
            narrow.append((cm.group(2), int(cm.group(3) or 1), cm.group(4)))
    if not narrow or 'Mtl_FragmentOut output;' not in src:
        return src
    internal = 'struct Mtl_FragmentOut_Narrow\n{\n' + '\n'.join(
        re.sub(r' \[\[.*?\]\];$', ';', l) for l in members) + '\n};\n'
    wide_members = []
    for line in members:
        cm = _COLOR_MEMBER.match(line)
        if cm and cm.group(3) != '4':
            line = '%s%s4 %s [[ color(%s) ]];' % (cm.group(1), cm.group(2), cm.group(4), cm.group(5))
        wide_members.append(line)
    wide = 'struct Mtl_FragmentOut\n{\n' + '\n'.join(wide_members) + '\n};\n'
    conv = ['static inline Mtl_FragmentOut mtl_widen_output(thread const Mtl_FragmentOut_Narrow &o)', '{',
            '    Mtl_FragmentOut r;']
    narrow_names = {n for _, _, n in narrow}
    for line in members:
        name = line.split('[[')[0].split()[-1] if line.strip() else None
        if not name:
            continue
        if name in narrow_names:
            t, n, _ = next(x for x in narrow if x[2] == name)
            fill = {1: '0, 0, 1', 2: '0, 1', 3: '1'}[n]
            conv.append('    r.%s = %s4(o.%s, %s);' % (name, t, name, fill))
        else:
            conv.append('    r.%s = o.%s;' % (name, name))
    conv += ['    return r;', '}', '']
    src = src[:m.start()] + internal + '\n' + wide + '\n' + '\n'.join(conv) + src[m.end():]
    src = src.replace('Mtl_FragmentOut output;', 'Mtl_FragmentOut_Narrow output;')
    src = src.replace('return output;', 'return mtl_widen_output(output);')
    return src


# ---------------------------------------------------------------- main conversion
class SubprogramConverter:
    def __init__(self, log=None):
        self.log = log or (lambda *a: None)

    def decls(self, dxbc):
        d = d2m.decls(dxbc)
        if not d.get('ok'):
            raise ConvertError('DecodeDXBC failed: %s' % d.get('exception'))
        return d

    def build_rdef(self, dec, params, prog_type):
        rb = RdefBuilder(dec['major'], dec['minor'], prog_type)
        binds = params['binds']
        cb_by_index = {b['index']: b['name'] for b in binds if b['kind'] == 'cb'}
        tex_by_index = {b['index']: b for b in binds if b['kind'] == 'tex'}
        buf_by_index = {b['index']: b for b in binds if b['kind'] == 'buf'}
        uav_by_index = {b['index']: b for b in binds if b['kind'] == 'uav'}
        inline_by_bp = {b['bindPoint']: b['sampler'] for b in binds if b['kind'] == 'sampler'}
        sampler_owner = {}
        for b in binds:
            if b['kind'] == 'tex' and b['sampler'] >= 0:
                sampler_owner.setdefault(b['sampler'], b['name'])
        cbs = {cb['name']: cb for cb in params['cbs']}
        names = {}   # (group, reg) -> name, for later reflection matching
        used_sampler_names = set()
        decls = dec['phases'][0]['decls'] if dec['phases'] else []
        # hull shaders keep global decls in phase 1
        for ph in dec['phases'][1:]:
            decls = decls + ph['decls']
        seen = set()
        for d in decls:
            op, reg = d['op'], d['idx'][0]
            key = (op, reg)
            if key in seen:
                continue
            seen.add(key)
            if op == OP_DCL_CB:
                name = cb_by_index.get(reg)
                if name is None:
                    raise ConvertError('no cbuffer name for cb%d' % reg)
                cb = cbs.get(name)
                if cb is None:
                    raise ConvertError('no cbuffer layout for %s' % name)
                variables = []
                for p in cb['params']:
                    t, size = param_type(p)
                    variables.append(dict(name=p['name'], offset=p['index'], size=size, type=t))
                for st in cb['structs']:
                    t, size = struct_type(st)
                    variables.append(dict(name=st['name'], offset=st['index'], size=size, type=t))
                variables.sort(key=lambda v: v['offset'])
                rb.add_resource(name, SIT_CBUFFER, 0, 0, 0, reg)
                rb.add_cbuffer(name, max(cb['size'], 16 * d['idx'][1]), variables)
                names[('cb', reg)] = name
            elif op == OP_DCL_SAMPLER:
                mode = (d['tok'] >> 11) & 0xf
                if reg in sampler_owner:
                    name = 'sampler' + sampler_owner[reg]
                elif reg in inline_by_bp:
                    name = inline_sampler_name(inline_by_bp[reg])
                    if name in used_sampler_names:   # same inline state on two registers
                        name += '_s%d' % reg
                    used_sampler_names.add(name)
                else:
                    raise ConvertError('no name for sampler s%d' % reg)
                rb.add_resource(name, SIT_SAMPLER, 0, 0, 0, reg, 1, 2 if mode == 1 else 0)
                names[('s', reg)] = name
            elif op == OP_DCL_RESOURCE:
                dim = (d['tok'] >> 11) & 0x1f
                nsamples = (d['tok'] >> 16) & 0x7f
                ret = d['rt'] & 0xf
                if dim == 1:   # typed Buffer<>
                    b = buf_by_index.get(reg) or tex_by_index.get(reg)
                    if b is None:
                        raise ConvertError('no name for buffer t%d' % reg)
                    rb.add_resource(b['name'], SIT_TBUFFER if False else SIT_TEXTURE, ret, 1, 0xffffffff, reg, 1, 0xC)
                    names[('t', reg)] = b['name']
                    continue
                t = tex_by_index.get(reg)
                if t is None:
                    raise ConvertError('no name for texture t%d' % reg)
                srv = DXBC_DIM_TO_SRV.get(dim, 4)
                rb.add_resource(t['name'], SIT_TEXTURE, ret, srv, nsamples if dim in (4, 9) else 0xffffffff, reg, 1, 0xC)
                names[('t', reg)] = t['name']
            elif op in (OP_DCL_RES_RAW, OP_DCL_RES_STRUCT):
                b = buf_by_index.get(reg) or tex_by_index.get(reg)
                if b is None:
                    raise ConvertError('no name for buffer t%d' % reg)
                if op == OP_DCL_RES_STRUCT:
                    stride = d['rt']
                    rb.add_resource(b['name'], SIT_STRUCTURED, 6, 1, stride, reg)
                    elem = dict(cls=SVC_SCALAR, vt=SVT_INT, rows=1, cols=1, elements=stride // 4 if stride > 4 else 0)
                    rb.add_cbuffer(b['name'], stride, [dict(name='$Element', offset=0, size=stride, type=elem)], 3)
                else:
                    rb.add_resource(b['name'], SIT_BYTEADDRESS, 6, 1, 0, reg)
                names[('t', reg)] = b['name']
            elif op in (OP_DCL_UAV_TYPED, OP_DCL_UAV_RAW, OP_DCL_UAV_STRUCT):
                u = uav_by_index.get(reg)
                if u is None:
                    # fragment shader UAVs bound by slot (Graphics.SetRandomWriteTarget) carry no name
                    u = dict(kind='uav', name='_RandomWriteTarget%d' % reg, index=reg, origIndex=reg)
                    params['binds'].append(u)
                    uav_by_index[reg] = u
                if op == OP_DCL_UAV_STRUCT:
                    stride = d['rt']
                    counter_uavs = {r for ph in dec['phases'] for r in ph.get('counterUAVs', [])}
                    rtype = SIT_UAV_RWSTRUCTURED_WITH_COUNTER if (d['counter'] or reg in counter_uavs) else SIT_UAV_RWSTRUCTURED
                    rb.add_resource(u['name'], rtype, 6, 1, stride, reg)
                    elem = dict(cls=SVC_SCALAR, vt=SVT_INT, rows=1, cols=1, elements=stride // 4 if stride > 4 else 0)
                    rb.add_cbuffer(u['name'], stride, [dict(name='$Element', offset=0, size=stride, type=elem)], 3)
                elif op == OP_DCL_UAV_RAW:
                    rb.add_resource(u['name'], SIT_UAV_RWBYTEADDRESS, 6, 1, 0, reg)
                else:
                    dim = (d['tok'] >> 11) & 0x1f
                    rb.add_resource(u['name'], SIT_UAV_RWTYPED, d['rt'] & 0xf, DXBC_DIM_TO_SRV.get(dim, 4), 0, reg)
                names[('u', reg)] = u['name']
        return rb.build(), names

    def convert(self, d3d_sub, d3d_params, stage, common=None, names_table=None):
        """stage: 'vs' or 'fs'. Returns (metal_sub_bytes, metal_param_bytes, info)."""
        sub = parse_subprog(d3d_sub)
        params = merge_common(parse_params(d3d_params), common, names_table or {})
        code = sub['code']
        i = code.find(b'DXBC')
        if i < 0:
            raise ConvertError('no DXBC in program data')
        dxbc = code[i:]
        dec = self.decls(dxbc)
        prog_type = dec['shaderType']
        rdef, names = self.build_rdef(dec, params, prog_type)
        hash16, chunks = dxbc_split(dxbc)
        chunks = [c for c in chunks if c[0] != b'RDEF'] + [(b'RDEF', rdef)]
        patched = dxbc_join(hash16, chunks)
        if DEBUG_DUMP:
            with open(DEBUG_DUMP, 'wb') as f:
                f.write(patched)
        res = d2m.translate(patched, HLSLCC_FLAGS)
        errs = [d for d in res['diag'] if d[2]]
        if not res['ok'] or errs:
            raise ConvertError('HLSLcc failed: %s %s' % (res.get('exception', ''), errs[:3]))
        src, _ = postprocess_msl(res['src'])
        # ---- Metal param blob
        mparams = self.metal_params(params, res, names, stage)
        # ---- Metal subprogram
        flags = MTL_FLAG_DEFAULT
        if stage == 'vs' and 'render_target_array_index' in src:
            flags |= MTL_FLAG_LAYERED
        entry = b'xlatMtlMain'
        body = src.encode('utf8')
        name_off = 48
        src_off = name_off + len(entry) + 1
        hdr = struct.pack('<Iiiiiiiiiiii', 0xF00DCAFE, name_off, 6, src_off, 0, 0, 0, 0, 0, flags, -1, 0)
        mcode = hdr + entry + b'\0' + body
        channels = []
        if stage == 'vs':
            attr = {}
            for name, loc in res['inputs']:
                attr[name] = loc
            for src_ch, tgt in sub['channels']:
                sem = D3D_TARGET_SEMANTIC.get(tgt)
                if sem is None:
                    raise ConvertError('unknown D3D vertex target %d' % tgt)
                key = '%s%d' % sem
                if key not in attr:
                    alt = {'POSITION0': ['SV_POSITION0', 'POS0', 'SV_Position0'], 'COLOR0': ['COLOR0']}.get(key, [])
                    found = [a for a in alt if a in attr]
                    if not found:
                        # input declared but unused by the shader -> not bound on Metal
                        continue
                    key = found[0]
                channels.append((src_ch, METAL_ATTRIB0 + attr[key]))
        msub = dict(version=sub['version'], type=GPU_METAL_VS if stage == 'vs' else GPU_METAL_FS,
                    alu=sub['alu'], tex=sub['tex'], flow=sub['flow'], temp=sub['temp'], keywords=sub['keywords'],
                    code=mcode, sourcemap=sum(1 << c for c, _ in channels) if stage == 'vs' else 0, channels=channels)
        return write_subprog(msub), write_params(mparams), dict(src=src, res=res)

    def metal_params(self, params, res, names, stage):
        cb_slot = dict((n, s) for n, s in res['cbBinds'])
        tex = {t[0]: t for t in res['textures']}
        bufs = {b[0]: b for b in res['buffers']}
        globals_name = {'vs': 'VGlobals', 'fs': 'FGlobals'}.get(stage, 'Globals')
        out = dict(version=BLOB_VERSION, cbs=[], binds=[])
        used_cb_names = set()
        for n in cb_slot:
            used_cb_names.add(n)
        for cb in params['cbs']:
            name = cb['name']
            mname = globals_name if name == '$Globals' else name
            if name and mname not in used_cb_names:
                continue
            ps = []
            for p in cb['params']:
                q = dict(p)
                if q['type'] == 2:      # bool -> int (HLSLcc declares bools as int)
                    q['type'] = 1
                ps.append(q)
            out['cbs'].append(dict(name=mname if name else '', size=cb['size'], params=ps,
                                   structs=[dict(st, params=[dict(q, type=(1 if q['type'] == 2 else q['type'])) for q in st['params']])
                                            for st in cb['structs']]))
        if not out['cbs'] or out['cbs'][0]['name'] != '':
            out['cbs'].insert(0, dict(name='', size=0, params=[], structs=[]))
        for b in params['binds']:
            if b['kind'] == 'tex':
                t = tex.get(b['name'])
                if t is None:
                    continue
                out['binds'].append(dict(kind='tex', name=b['name'], index=t[1], sampler=t[2] if t[2] >= 0 else -1, extra=b['extra']))
            elif b['kind'] == 'buf':
                bb = bufs.get(b['name'])
                if bb is None:
                    continue
                out['binds'].append(dict(kind='buf', name=b['name'], index=bb[1], arraySize=0))
            elif b['kind'] == 'uav':
                bb = bufs.get(b['name'])
                tt = tex.get(b['name'])
                if bb is not None:
                    out['binds'].append(dict(kind='uav', name=b['name'], index=bb[1], origIndex=b['origIndex']))
                elif tt is not None:
                    out['binds'].append(dict(kind='uav', name=b['name'], index=tt[1], origIndex=b['origIndex']))
        for n, s in res['cbBinds']:
            out['binds'].append(dict(kind='cb', name=n, index=s, arraySize=0))
        return out
