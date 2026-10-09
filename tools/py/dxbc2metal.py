"""ctypes binding for libdxbc2metal (HLSLcc DXBC -> Metal)."""
import ctypes, json, os
_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'dxbc2metal', 'build', 'libdxbc2metal.dylib')
_lib = ctypes.CDLL(_LIB)
_lib.dxbc2metal.restype = ctypes.c_void_p
_lib.dxbc2metal.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint]
_lib.dxbc2metal_free.argtypes = [ctypes.c_void_p]

# HLSLcc flags
UNIFORM_BUFFER_OBJECT = 0x1
GLOBAL_CONSTS_NEVER_IN_UBO = 0x8
GS_ENABLED = 0x10
TESS_ENABLED = 0x20
REMOVE_UNUSED_GLOBALS = 0x10000
TRANSLATE_MATRICES = 0x20000
METAL_SHADOW_SAMPLER_LINEAR = 0x80000
METAL_TESSELLATION = 0x2000000
DISABLE_FASTMATH = 0x4000000
METAL_PRESERVE_DX_CB_LAYOUT = 0x80000000

def translate(dxbc: bytes, flags: int) -> dict:
    p = _lib.dxbc2metal(dxbc, len(dxbc), flags)
    try:
        s = ctypes.string_at(p).decode('utf8', 'replace')
    finally:
        _lib.dxbc2metal_free(p)
    return json.loads(s)

_lib.dxbc_decls.restype = ctypes.c_void_p
_lib.dxbc_decls.argtypes = [ctypes.c_char_p, ctypes.c_size_t]

def decls(dxbc: bytes) -> dict:
    p = _lib.dxbc_decls(dxbc, len(dxbc))
    try:
        s = ctypes.string_at(p).decode('utf8', 'replace')
    finally:
        _lib.dxbc2metal_free(p)
    return json.loads(s)
