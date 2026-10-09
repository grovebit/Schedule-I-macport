"""D3D11 -> Metal conversion for Unity 2022.3 ComputeShader objects."""
import struct

import dxbc2metal as d2m
from convert import (OP_DCL_CB, OP_DCL_RES_RAW, OP_DCL_RES_STRUCT, OP_DCL_RESOURCE, OP_DCL_SAMPLER,
                     OP_DCL_UAV_RAW, OP_DCL_UAV_STRUCT, OP_DCL_UAV_TYPED, DXBC_DIM_TO_SRV, HLSLCC_FLAGS,
                     SIT_BYTEADDRESS, SIT_CBUFFER, SIT_SAMPLER, SIT_STRUCTURED, SIT_TEXTURE,
                     SIT_UAV_RWBYTEADDRESS, SIT_UAV_RWSTRUCTURED, SIT_UAV_RWSTRUCTURED_WITH_COUNTER, SIT_UAV_RWTYPED,
                     SVC_SCALAR, SVT_INT, ConvertError, DEBUG_DUMP, RdefBuilder, dxbc_join, dxbc_split, inline_sampler_name,
                     param_type)

RENDERER_D3D11, RENDERER_METAL = 2, 16
METAL_COMPUTE_MAGIC = 0xDEADCAFE
METAL_COMPUTE_FLAG_NEW_BUFFERS = 0x1


def cb_param_to_unity(p):
    """ComputeShader cb param -> ushader-style param dict."""
    is_matrix = 1 if p['rowCount'] > 1 else 0
    return dict(name=p['name'], type=p['type'], rows=p['rowCount'], cols=p['colCount'], isMatrix=is_matrix,
                arraySize=p['arraySize'], index=p['offset'])


def build_rdef_compute(dec, uv, all_cbs):
    rb = RdefBuilder(dec['major'], dec['minor'], dec['shaderType'])
    # several constantBuffers entries can share a name; each kernel variant selects its own via cbVariantIndices
    cb_defs = {}
    for i, b in enumerate(uv['cbs']):
        idx = uv['cbVariantIndices'][i] if i < len(uv['cbVariantIndices']) else -1
        if 0 <= idx < len(all_cbs):
            cb_defs[b['name']] = all_cbs[idx]
    cbs = {b['bindPoint']: b['name'] for b in uv['cbs']}
    tex = {b['bindPoint']: b for b in uv['textures']}
    inb = {b['bindPoint']: b for b in uv['inBuffers']}
    outb = {b['bindPoint']: b for b in uv['outBuffers']}
    sampler_owner = {}
    for t in uv['textures']:
        if t['samplerBindPoint'] >= 0:
            sampler_owner.setdefault(t['samplerBindPoint'], t['name'])
    builtin = {s['bindPoint']: s['sampler'] for s in uv['builtinSamplers']}
    used_names = set()
    seen = set()
    # Append/Consume buffers: the counter flag only lived in the stripped RDEF; infer it from the instructions
    counter_uavs = {r for ph in dec['phases'] for r in ph.get('counterUAVs', [])}
    for d in [x for ph in dec['phases'] for x in ph['decls']]:
        op, reg = d['op'], d['idx'][0]
        if (op, reg) in seen:
            continue
        seen.add((op, reg))
        if op == OP_DCL_CB:
            name = cbs.get(reg)
            if name is None:
                raise ConvertError('compute: no cbuffer for cb%d' % reg)
            cdef = cb_defs.get(name)
            variables = []
            size = 16 * d['idx'][1]
            if cdef is not None:
                size = max(size, cdef['byteSize'])
                for p in cdef['params']:
                    up = cb_param_to_unity(p)
                    t, sz = param_type(up)
                    variables.append(dict(name=up['name'], offset=up['index'], size=sz, type=t))
                variables.sort(key=lambda v: v['offset'])
            rb.add_resource(name, SIT_CBUFFER, 0, 0, 0, reg)
            rb.add_cbuffer(name, size, variables)
        elif op == OP_DCL_SAMPLER:
            mode = (d['tok'] >> 11) & 0xf
            if reg in sampler_owner:
                name = 'sampler' + sampler_owner[reg]
            elif reg in builtin:
                name = inline_sampler_name(builtin[reg])
                if name in used_names:
                    name += '_s%d' % reg
            else:
                raise ConvertError('compute: no name for sampler s%d' % reg)
            used_names.add(name)
            rb.add_resource(name, SIT_SAMPLER, 0, 0, 0, reg, 1, 2 if mode == 1 else 0)
        elif op == OP_DCL_RESOURCE:
            dim = (d['tok'] >> 11) & 0x1f
            b = tex.get(reg) or inb.get(reg)
            if b is None:
                raise ConvertError('compute: no name for t%d' % reg)
            nsamples = (d['tok'] >> 16) & 0x7f
            rb.add_resource(b['name'], SIT_TEXTURE, d['rt'] & 0xf, DXBC_DIM_TO_SRV.get(dim, 4),
                            nsamples if dim in (4, 9) else 0xffffffff, reg, 1, 0xC)
        elif op in (OP_DCL_RES_RAW, OP_DCL_RES_STRUCT):
            b = inb.get(reg) or tex.get(reg)
            if b is None:
                raise ConvertError('compute: no name for buffer t%d' % reg)
            if op == OP_DCL_RES_STRUCT:
                stride = d['rt']
                rb.add_resource(b['name'], SIT_STRUCTURED, 6, 1, stride, reg)
                elem = dict(cls=SVC_SCALAR, vt=SVT_INT, rows=1, cols=1, elements=stride // 4 if stride > 4 else 0)
                rb.add_cbuffer(b['name'], stride, [dict(name='$Element', offset=0, size=stride, type=elem)], 3)
            else:
                rb.add_resource(b['name'], SIT_BYTEADDRESS, 6, 1, 0, reg)
        elif op in (OP_DCL_UAV_TYPED, OP_DCL_UAV_RAW, OP_DCL_UAV_STRUCT):
            b = outb.get(reg)
            if b is None:
                raise ConvertError('compute: no name for u%d' % reg)
            if op == OP_DCL_UAV_STRUCT:
                stride = d['rt']
                has_counter = d['counter'] or reg in counter_uavs
                rtype = SIT_UAV_RWSTRUCTURED_WITH_COUNTER if has_counter else SIT_UAV_RWSTRUCTURED
                rb.add_resource(b['name'], rtype, 6, 1, stride, reg)
                elem = dict(cls=SVC_SCALAR, vt=SVT_INT, rows=1, cols=1, elements=stride // 4 if stride > 4 else 0)
                rb.add_cbuffer(b['name'], stride, [dict(name='$Element', offset=0, size=stride, type=elem)], 3)
            elif op == OP_DCL_UAV_RAW:
                rb.add_resource(b['name'], SIT_UAV_RWBYTEADDRESS, 6, 1, 0, reg)
            else:
                dim = (d['tok'] >> 11) & 0x1f
                rb.add_resource(b['name'], SIT_UAV_RWTYPED, d['rt'] & 0xf, DXBC_DIM_TO_SRV.get(dim, 4), 0, reg)
    return rb.build()


def convert_kernel_variant(uv, all_cbs, validate=None, label=''):
    code = bytes(uv['code'])
    if code[:4] != b'DXBC':
        raise ConvertError('compute: code is not DXBC')
    dec = d2m.decls(code)
    if not dec.get('ok'):
        raise ConvertError('compute: DecodeDXBC failed')
    rdef = build_rdef_compute(dec, uv, all_cbs)
    hash16, chunks = dxbc_split(code)
    chunks = [c for c in chunks if c[0] != b'RDEF'] + [(b'RDEF', rdef)]
    patched = dxbc_join(hash16, chunks)
    if DEBUG_DUMP:
        with open(DEBUG_DUMP, 'wb') as f:
            f.write(patched)
    res = d2m.translate(patched, HLSLCC_FLAGS)
    errs = [d for d in res['diag'] if d[2]]
    if not res['ok'] or errs:
        raise ConvertError('compute HLSLcc failed: %s %s' % (res.get('exception', ''), errs[:3]))
    src = res['src']
    if validate is not None:
        validate(src, 'cs', label)
    cb_slot = dict((n, s) for n, s in res['cbBinds'])
    tex_slot = {t[0]: t for t in res['textures']}
    buf_slot = {b[0]: b for b in res['buffers']}

    def cbname(n):
        return 'Globals' if n == '$Globals' else n

    out = dict(uv)
    out['cbs'] = [dict(b, name=cbname(b['name']), bindPoint=cb_slot[cbname(b['name'])])
                  for b in uv['cbs'] if cbname(b['name']) in cb_slot]
    textures = []
    for b in uv['textures']:
        t = tex_slot.get(b['name'])
        if t is None:
            continue
        textures.append(dict(b, bindPoint=t[1], samplerBindPoint=t[2] if t[2] >= 0 else -1))
    out['textures'] = textures
    out['builtinSamplers'] = []          # inline samplers become constexpr samplers in MSL
    inb = []
    for b in uv['inBuffers']:
        if b['name'] in buf_slot:
            inb.append(dict(b, bindPoint=buf_slot[b['name']][1]))
        elif b['name'] in tex_slot:      # typed Buffer<> bound as texture
            inb.append(dict(b, bindPoint=tex_slot[b['name']][1]))
    out['inBuffers'] = inb
    outb = []
    for b in uv['outBuffers']:
        if b['name'] in buf_slot:
            outb.append(dict(b, bindPoint=buf_slot[b['name']][1]))
        elif b['name'] in tex_slot:
            outb.append(dict(b, bindPoint=tex_slot[b['name']][1]))
    out['outBuffers'] = outb
    tg = uv['threadGroupSize']
    body = src.encode('utf8')
    hdr = struct.pack('<IIIHHHH', METAL_COMPUTE_MAGIC, 2, 20, tg[0], tg[1], tg[2], METAL_COMPUTE_FLAG_NEW_BUFFERS)
    out['code'] = hdr + body
    return out


BUFSIZE_PREFIX = '_mp_BufSize_'
UNITY_PARAM_UINT = 5


def augment_buffer_size_params(variant):
    """Kernels using Buffer.GetDimensions read the element count from a hidden uint in $Globals
    (filled by the port's ComputeShader.SetBuffer hook). Returns the variant's constantBuffers with
    those params appended."""
    cbs = [dict(c, params=list(c['params'])) for c in variant['constantBuffers']]
    for k in variant['kernels']:
        for uv in k['uniqueVariants']:
            dec = d2m.decls(bytes(uv['code']))
            if not dec.get('ok'):
                continue
            regs = [tuple(x) for ph in dec['phases'] for x in ph.get('bufinfo', [])]
            if not regs:
                continue
            names = []
            for is_uav, reg in regs:
                pool = uv['outBuffers'] if is_uav else (uv['inBuffers'] + uv['textures'])
                b = next((b for b in pool if b['bindPoint'] == reg), None)
                if b is not None:
                    names.append(b['name'])
            for i, cb in enumerate(uv['cbs']):
                if cb['name'] != '$Globals' or i >= len(uv['cbVariantIndices']):
                    continue
                c = cbs[uv['cbVariantIndices'][i]]
                have = {p['name'] for p in c['params']}
                for n in names:
                    pname = BUFSIZE_PREFIX + n
                    if pname in have:
                        continue
                    off = (c['byteSize'] + 3) & ~3
                    c['params'].append(dict(name=pname, type=UNITY_PARAM_UINT, offset=off, arraySize=0, rowCount=1, colCount=1))
                    c['byteSize'] = (off + 4 + 15) & ~15
                    have.add(pname)
    return cbs


def convert_compute_shader(tt, validate=None, log=print):
    """Converts a ComputeShader typetree in place. Returns stats."""
    stats = dict(name=tt['m_Name'], kernels=0, failed=0, errors={})
    variants = tt['variants']
    d3d = [v for v in variants if v['targetRenderer'] == RENDERER_D3D11]
    if not d3d or any(v['targetRenderer'] == RENDERER_METAL for v in variants):
        stats['skipped'] = True
        return stats
    new_variants = []
    for v in d3d:
        cb_defs = augment_buffer_size_params(v)
        nv = dict(v)
        nv['targetRenderer'] = RENDERER_METAL
        kernels = []
        for k in v['kernels']:
            nk = dict(k)
            uvs = []
            for i, uv in enumerate(k['uniqueVariants']):
                try:
                    uvs.append(convert_kernel_variant(uv, cb_defs, validate, '%s/%s' % (tt['m_Name'], k['name'])))
                    stats['kernels'] += 1
                except ConvertError as e:
                    stats['failed'] += 1
                    msg = str(e)[:200]
                    stats['errors'][msg] = stats['errors'].get(msg, 0) + 1
                    uvs.append(None)
            if any(u is None for u in uvs):
                # keep the kernel present but unusable rather than shifting variant indices
                continue
            nk['uniqueVariants'] = uvs
            kernels.append(nk)
        nv['kernels'] = kernels
        cbs = []
        for c in cb_defs:
            params = [dict(p, type=(1 if p['type'] == 2 else p['type'])) for p in c['params']]
            cbs.append(dict(c, name='Globals' if c['name'] == '$Globals' else c['name'], params=params))
        nv['constantBuffers'] = cbs
        new_variants.append(nv)
    tt['variants'] = new_variants
    return stats
