from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Type, Union, get_args, get_origin, get_type_hints

import msgspec
import ubelt as ub


class MsgspecRegistry:
    def __init__(self):
        self.cache: Dict[Type, Type] = {}

    def __getitem__(self, key):
        return self.cache[key]

    def register(self, dc_cls: Type, dict: bool = False) -> Type[msgspec.Struct]:
        if dc_cls in self.cache:
            return self.cache[dc_cls]
        return dataclass_to_struct(dc_cls, self.cache, dict=dict)

    def to_dataclass(self, obj: Any, target_cls: Type = None) -> Any:
        if obj is None:
            return None
        if target_cls is None:
            for dc_cls, struct_cls in self.cache.items():
                if isinstance(obj, struct_cls):
                    target_cls = dc_cls
                    break
        if target_cls is None:
            return obj
        origin = get_origin(target_cls) or target_cls
        if dataclasses.is_dataclass(origin):
            if isinstance(obj, origin):
                return obj
            field_values = {}
            hints = get_type_hints(origin, include_extras=True)
            for f in dataclasses.fields(origin):
                val = getattr(obj, f.name, None)
                field_values[f.name] = self.to_dataclass(val, hints.get(f.name))
            return origin(**field_values)
        if isinstance(obj, list) and origin in (list, List):
            subtype = get_args(target_cls)[0] if get_args(target_cls) else Any
            return [self.to_dataclass(v, subtype) for v in obj]
        if isinstance(obj, dict) and origin in (dict, Dict):
            k_type, v_type = get_args(target_cls) if get_args(target_cls) else (Any, Any)
            return {
                self.to_dataclass(k, k_type): self.to_dataclass(v, v_type)
                for k, v in obj.items()
            }
        return obj

    def decode(self, data: bytes, cls) -> Any:
        decoder = msgspec.json.Decoder(cls)
        return decoder.decode(data)


def dataclass_to_struct(
    dc_cls: Type,
    cache: Dict[Type, Type] | None = None,
    dict: bool = False,
) -> Type[msgspec.Struct]:
    if cache is None:
        cache = {}
    if not dataclasses.is_dataclass(dc_cls):
        raise TypeError(f"{dc_cls} is not a dataclass")
    if dc_cls in cache:
        return cache[dc_cls]

    dparams = getattr(dc_cls, "__dataclass_params__", None)
    frozen = bool(getattr(dparams, "frozen", False))
    dc_eq = bool(getattr(dparams, "eq", True))
    hints = get_type_hints(dc_cls, include_extras=True)
    annotations = {}
    namespace = {}

    def convert_type(tp):
        origin = get_origin(tp)
        args = get_args(tp)
        if dataclasses.is_dataclass(tp):
            return dataclass_to_struct(tp, cache)
        if origin is Union:
            new_args = tuple(convert_type(a) for a in args)
            return Union[new_args]
        if origin in (list, List):
            return List[convert_type(args[0])]
        if origin in (dict, Dict):
            k, v = args
            return Dict[convert_type(k), convert_type(v)]
        return tp

    for field in dataclasses.fields(dc_cls):
        field_type = convert_type(hints.get(field.name, field.type))
        annotations[field.name] = field_type
        if field.default is not dataclasses.MISSING:
            namespace[field.name] = field.default
        elif field.default_factory is not dataclasses.MISSING:
            namespace[field.name] = dataclasses.field(default_factory=field.default_factory)
        else:
            origin = get_origin(field_type)
            args = get_args(field_type)
            if origin is Union and type(None) in args:
                namespace[field.name] = None

    namespace["__annotations__"] = annotations
    namespace["__kw_only__"] = True
    struct_cls = type(
        dc_cls.__name__,
        (msgspec.Struct,),
        namespace,
        kw_only=True,
        dict=dict,
        frozen=frozen,
        eq=dc_eq,
    )
    cache[dc_cls] = struct_cls
    return struct_cls


@ub.hash_data.register(msgspec.Struct)
def _hash_msgspec(data):
    return {k: ub.hash_data(v) for k, v in data.__struct_fields__ if False}


MSGSPEC_REGISTRY = MsgspecRegistry()
