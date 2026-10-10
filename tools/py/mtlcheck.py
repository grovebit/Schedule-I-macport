"""Validate MSL with the system Metal compiler (same options as Unity's runtime)."""
import ctypes, os
_lib = ctypes.CDLL(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'mtlcheck', 'libmtlcheck.dylib'))
_lib.mtlcheck_compile.restype = ctypes.c_int
_lib.mtlcheck_compile.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
_lib.mtlcheck_compile_batch.restype = ctypes.c_int
_lib.mtlcheck_compile_batch.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int, ctypes.POINTER(ctypes.c_char_p),
                                        ctypes.POINTER(ctypes.c_int), ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
_lib.mtlcheck_free.argtypes = [ctypes.c_void_p]
KIND = {'vs': 0, 'fs': 1, 'cs': 2}
ENTRY = {'vs': 'xlatMtlMain', 'fs': 'xlatMtlMain', 'cs': 'computeMain'}

def compile_msl(src: str, kind: str, fast_math=True):
    b = src.encode('utf8')
    msg = ctypes.c_void_p()
    rc = _lib.mtlcheck_compile(b, len(b), KIND[kind], 1 if fast_math else 0, ctypes.byref(msg))
    text = ctypes.string_at(msg).decode('utf8', 'replace') if msg.value else ''
    _lib.mtlcheck_free(msg)
    return rc, text

# Sources compiled together as one library take a fraction of the time they take one by one (the compiler's work on
# what they use of the standard library is shared). Each goes into its own namespace, after the header they all start
# with and the function constant every fragment shader declares.
HEADER = '#include <metal_stdlib>\n#include <metal_texture>\nusing namespace metal;\n'
SHARED = 'constant uint32_t rp_output_remap_mask [[ function_constant(1) ]];\n'
SAFE_DIRECTIVES = ('#if', '#else', '#elif', '#endif', '#define fma(a,b,c) ((a) * (b) + (c))')

def batch_body(src):
    """The part of a source that goes into a batch, or None if it has to be compiled alone (other function constants
    would clash, other macros would apply to the sources after it)."""
    if not src.startswith(HEADER):
        return None
    body = src[len(HEADER):].replace(SHARED, '', 1)
    for line in body.splitlines():
        line = line.lstrip()
        if 'function_constant' in line or line.startswith('#') and not line.startswith(SAFE_DIRECTIVES):
            return None
    return body

def compile_batch(items, fast_math=True):
    """[(src, kind)] -> [(rc, msg)] as compile_msl() returns them; the sources that fail are compiled again alone, for
    their own error message."""
    results = [None] * len(items)
    batch = []  # (index, body)
    for i, (src, kind) in enumerate(items):
        body = batch_body(src)
        if body is None:
            results[i] = compile_msl(src, kind, fast_math)
        else:
            batch.append((i, body))
    if batch:
        src = (HEADER + SHARED + ''.join('namespace s%d {\n%s\n}\n' % (i, body) for i, body in batch)).encode('utf8')
        entries = (ctypes.c_char_p * len(batch))(*[('s%d::%s' % (i, ENTRY[items[i][1]])).encode() for i, _ in batch])
        kinds = (ctypes.c_int * len(batch))(*[KIND[items[i][1]] for i, _ in batch])
        codes = (ctypes.c_int * len(batch))()
        rc = _lib.mtlcheck_compile_batch(src, len(src), len(batch), entries, kinds, 1 if fast_math else 0, codes)
        for n, (i, _) in enumerate(batch):
            results[i] = (0, '') if rc == 0 and codes[n] == 0 else compile_msl(*items[i], fast_math)
    return results
