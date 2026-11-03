"""测试辅助：按 MaiBot 插件加载器的方式载入包。"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_PACKAGE = "ghsub_under_test"
_ROOT = Path(__file__).resolve().parents[1]


def load_pkg() -> ModuleType:
    """把仓库根目录作为一个包载入，返回包模块。"""

    if _PACKAGE in sys.modules:
        return sys.modules[_PACKAGE]
    spec = importlib.util.spec_from_file_location(
        _PACKAGE,
        _ROOT / "__init__.py",
        submodule_search_locations=[str(_ROOT)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PACKAGE] = module
    spec.loader.exec_module(module)
    return module


def load(name: str) -> ModuleType:
    load_pkg()
    return importlib.import_module(f"{_PACKAGE}.{name}")
