"""测试基座：让 tests/ 直接 import custom_components.symi_mesh，并加载 HA 测试插件。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest_plugins = "pytest_homeassistant_custom_component"
