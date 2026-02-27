import sys
import pathlib

# Add zig-out/lib to the Python path so we can import the extension
_ext_dir = str(pathlib.Path(__file__).resolve().parent.parent / "zig-out" / "lib")
if _ext_dir not in sys.path:
    sys.path.insert(0, _ext_dir)

import _framework_core


def test_import_succeeds() -> None:
    assert _framework_core is not None


def test_hello_returns_string() -> None:
    result = _framework_core.hello()
    assert result == "hello from Zig"
