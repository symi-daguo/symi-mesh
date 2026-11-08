"""实体类属性单测：确保传给实体注册表的枚举是实例而不是字符串。

背景：HACS 审核（frenck）指出 `_attr_entity_category = "diagnostic"` 会让
type 13/17 设备的实体在 async_add_entities 时抛 ValueError。

注意：HA 的 Entity 用描述符包装 `_attr_*`，类级访问拿到的是 property 对象，
因此这里从源码层面断言赋值用的是枚举而不是字符串字面量。
"""
from __future__ import annotations

import inspect

from homeassistant.const import EntityCategory

from custom_components.symi_mesh.entity import SymiDiagnosticSensor


def test_entity_category_enum_is_imported() -> None:
    assert EntityCategory.DIAGNOSTIC == "diagnostic"
    assert isinstance(EntityCategory.DIAGNOSTIC, EntityCategory)


def test_diagnostic_sensor_assigns_enum_not_string() -> None:
    source = inspect.getsource(SymiDiagnosticSensor)
    assert "EntityCategory.DIAGNOSTIC" in source
    assert '"diagnostic"' not in source
    assert "'diagnostic'" not in source
