"""Strategy package — auto-import all strategy modules to trigger registration."""

import importlib
import pkgutil
from pathlib import Path

_SKIP = {"base", "registry"}
_pkg_dir = str(Path(__file__).parent)

for _info in pkgutil.iter_modules([_pkg_dir]):
    if _info.name not in _SKIP:
        importlib.import_module(f".{_info.name}", __name__)
