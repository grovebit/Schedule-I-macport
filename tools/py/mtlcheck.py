"""Validate MSL with the system Metal compiler (same options as Unity's runtime)."""
import ctypes, os
_lib = ctypes.CDLL(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'mtlcheck', 'libmtlcheck.dylib'))
_lib.mtlcheck_compile.restype = ctypes.c_int
_lib.mtlcheck_compile.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
_lib.mtlcheck_free.argtypes = [ctypes.c_void_p]
KIND = {'vs': 0, 'fs': 1, 'cs': 2}

def compile_msl(src: str, kind: str, fast_math=True):
    b = src.encode('utf8')
    msg = ctypes.c_void_p()
    rc = _lib.mtlcheck_compile(b, len(b), KIND[kind], 1 if fast_math else 0, ctypes.byref(msg))
    text = ctypes.string_at(msg).decode('utf8', 'replace') if msg.value else ''
    _lib.mtlcheck_free(msg)
    return rc, text
