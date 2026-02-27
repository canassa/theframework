import ctypes
import pathlib
import sys

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
_LIB_PATH = _PROJECT_ROOT / "zig-out" / "lib" / "libframework.so"

# Add zig-out/lib to sys.path so `import _framework_core` works everywhere
_ext_dir = str(_PROJECT_ROOT / "zig-out" / "lib")
if _ext_dir not in sys.path:
    sys.path.insert(0, _ext_dir)

_lib = ctypes.CDLL(str(_LIB_PATH))
_lib.framework_version.restype = ctypes.c_int32
_lib.framework_version.argtypes = []


def version() -> int:
    result: int = _lib.framework_version()
    return result


from theframework.app import Framework as Framework
from theframework.request import Request as Request
from theframework.response import Response as Response

__all__ = ["Framework", "Request", "Response", "version"]
