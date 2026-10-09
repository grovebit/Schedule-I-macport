"""Unity 2022.3 (blob version 202012090) shader blob parsing/writing helpers."""
import struct

BLOB_VERSION = 202012090

class R:
    def __init__(s, b, p=0): s.b = b; s.p = p
    def i(s): v = struct.unpack_from('<i', s.b, s.p)[0]; s.p += 4; return v
    def u(s): v = struct.unpack_from('<I', s.b, s.p)[0]; s.p += 4; return v
    def raw(s, n): v = s.b[s.p:s.p+n]; s.p += n; return v
    def align(s): s.p = (s.p + 3) & ~3
    def str_(s):
        n = s.i(); v = s.b[s.p:s.p+n].decode('utf8'); s.p += n; s.align(); return v

class W:
    def __init__(s): s.b = bytearray()
    def i(s, v): s.b += struct.pack('<i', v)
    def u(s, v): s.b += struct.pack('<I', v & 0xffffffff)
    def raw(s, v): s.b += v
    def align(s):
        while len(s.b) % 4: s.b.append(0)
    def str_(s, v):
        e = v.encode('utf8'); s.i(len(e)); s.b += e; s.align()

# ---------------- blob table ----------------
def read_table(data):
    cnt = struct.unpack_from('<i', data, 0)[0]
    return [struct.unpack_from('<iii', data, 4 + 12 * i) for i in range(cnt)]

def get_entry(segs, table, idx):
    o, l, s = table[idx]
    return segs[s][o:o+l]

def build_segment(entries):
    """entries: list of bytes. Returns single segment data with table (all segment 0)."""
    hdr = 4 + 12 * len(entries)
    out = bytearray(hdr)
    struct.pack_into('<i', out, 0, len(entries))
    off = hdr
    body = bytearray()
    for i, e in enumerate(entries):
        struct.pack_into('<iii', out, 4 + 12 * i, off + len(body), len(e), 0)
        body += e
    return bytes(out + body)

# ---------------- subprogram ----------------
def parse_subprog(b):
    r = R(b)
    d = dict(version=r.i(), type=r.i(), alu=r.i(), tex=r.i(), flow=r.i(), temp=r.i())
    d['keywords'] = [r.str_() for _ in range(r.i())]
    n = r.i(); d['code'] = r.raw(n); r.align()
    d['sourcemap'] = r.i()
    d['channels'] = [(r.i(), r.i()) for _ in range(r.i())]
    if r.p != len(b):
        d['trailing'] = b[r.p:]
    return d

def write_subprog(d):
    w = W()
    for k in ('version', 'type', 'alu', 'tex', 'flow', 'temp'): w.i(d[k])
    w.i(len(d['keywords']))
    for k in d['keywords']: w.str_(k)
    w.i(len(d['code'])); w.raw(d['code']); w.align()
    w.i(d['sourcemap']); w.i(len(d['channels']))
    for s, t in d['channels']: w.i(s); w.i(t)
    if d.get('trailing'): w.raw(d['trailing'])
    return bytes(w.b)

# ---------------- parameter blob ----------------
def _read_cbparam(r):
    return dict(name=r.str_(), type=r.i(), rows=r.i(), cols=r.i(), isMatrix=r.i(), arraySize=r.i(), index=r.i())

def _write_cbparam(w, p):
    w.str_(p['name']); w.i(p['type']); w.i(p['rows']); w.i(p['cols']); w.i(p['isMatrix']); w.i(p['arraySize']); w.i(p['index'])

def parse_params(b):
    r = R(b)
    d = dict(version=r.i(), cbs=[], binds=[])
    for _ in range(r.i()):
        cb = dict(name=r.str_(), size=r.i(), params=[], structs=[])
        cb['params'] = [_read_cbparam(r) for _ in range(r.i())]
        for _ in range(r.i()):
            st = dict(name=r.str_(), index=r.i(), arraySize=r.i(), size=r.i())
            st['params'] = [_read_cbparam(r) for _ in range(r.i())]
            cb['structs'].append(st)
        d['cbs'].append(cb)
    for _ in range(r.i()):
        name = r.str_(); t = r.i()
        if t == 0:   e = dict(kind='tex', name=name, index=r.i(), sampler=r.i(), extra=r.u())
        elif t == 1: e = dict(kind='cb', name=name, index=r.i(), arraySize=r.i())
        elif t == 2: e = dict(kind='buf', name=name, index=r.i(), arraySize=r.i())
        elif t == 3: e = dict(kind='uav', name=name, index=r.i(), origIndex=r.i())
        elif t == 4: e = dict(kind='sampler', name=name, bindPoint=r.i(), sampler=r.u())
        else: raise ValueError('unknown bind type %d' % t)
        d['binds'].append(e)
    if r.p != len(b):
        d['trailing'] = b[r.p:]
    return d

KIND_ID = dict(tex=0, cb=1, buf=2, uav=3, sampler=4)

def write_params(d):
    w = W(); w.i(d['version']); w.i(len(d['cbs']))
    for cb in d['cbs']:
        w.str_(cb['name']); w.i(cb['size']); w.i(len(cb['params']))
        for p in cb['params']: _write_cbparam(w, p)
        w.i(len(cb['structs']))
        for st in cb['structs']:
            w.str_(st['name']); w.i(st['index']); w.i(st['arraySize']); w.i(st['size']); w.i(len(st['params']))
            for p in st['params']: _write_cbparam(w, p)
    w.i(len(d['binds']))
    for e in d['binds']:
        w.str_(e['name']); w.i(KIND_ID[e['kind']])
        k = e['kind']
        if k == 'tex': w.i(e['index']); w.i(e['sampler']); w.u(e['extra'])
        elif k in ('cb', 'buf'): w.i(e['index']); w.i(e['arraySize'])
        elif k == 'uav': w.i(e['index']); w.i(e['origIndex'])
        elif k == 'sampler': w.i(e['bindPoint']); w.u(e['sampler'])
    if d.get('trailing'): w.raw(d['trailing'])
    return bytes(w.b)

def fmt_params(d):
    out = []
    for cb in d['cbs']:
        out.append('  cb %r size=%d' % (cb['name'], cb['size']))
        for p in cb['params']:
            out.append('     %-40s t%d %dx%d m%d arr%d @%d' % (p['name'], p['type'], p['rows'], p['cols'], p['isMatrix'], p['arraySize'], p['index']))
        for st in cb['structs']:
            out.append('     struct %s idx%d arr%d size%d nparams%d' % (st['name'], st['index'], st['arraySize'], st['size'], len(st['params'])))
    for e in d['binds']:
        out.append('  bind ' + ' '.join('%s=%s' % (k, (hex(v) if k in ('extra', 'sampler') and isinstance(v, int) and v > 255 else v)) for k, v in e.items()))
    if d.get('trailing'): out.append('  TRAILING %d bytes' % len(d['trailing']))
    return '\n'.join(out)
