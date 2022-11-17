"""
Utilities for working with pyarrow.
https://arrow.apache.org/docs/python/
"""
import collections.abc
import dataclasses
import datetime
import operator
from typing import (
    Any,
    Callable,
    Iterable,
    Iterator,
    List,
    Optional,
    Sequence,
    Type,
    Union,
)

import pyarrow as pa
import pyarrow.compute as pc
from google.protobuf.descriptor import Descriptor, EnumDescriptor, FieldDescriptor
from google.protobuf.descriptor_pb2 import FieldDescriptorProto
from google.protobuf.message import Message
from google.protobuf.pyext._message import RepeatedScalarContainer, ScalarMapContainer
from google.protobuf.timestamp_pb2 import Timestamp
from google.protobuf.wrappers_pb2 import (
    BoolValue,
    BytesValue,
    DoubleValue,
    FloatValue,
    Int32Value,
    Int64Value,
    StringValue,
    UInt32Value,
    UInt64Value,
)
from google.type.date_pb2 import Date
from google.type.timeofday_pb2 import TimeOfDay

from protarrow.common import M, ProtarrowConfig

_PA_TIMESTAMP_TYPE = pa.timestamp("ns", "UTC")
_PA_TIME_TYPE = pa.time64("ns")
# Mapping from protobuf message type descriptors to pyarrow types.
# Any message types that do not appear here explicitly will be
# converted to pyarrow structs
_PROTO_DESCRIPTOR_TO_PYARROW = {
    Date.DESCRIPTOR: pa.date32(),
    Timestamp.DESCRIPTOR: _PA_TIMESTAMP_TYPE,
    TimeOfDay.DESCRIPTOR: _PA_TIME_TYPE,
    BoolValue.DESCRIPTOR: pa.bool_(),
    BytesValue.DESCRIPTOR: pa.binary(),
    DoubleValue.DESCRIPTOR: pa.float64(),
    FloatValue.DESCRIPTOR: pa.float32(),
    Int32Value.DESCRIPTOR: pa.int32(),
    Int64Value.DESCRIPTOR: pa.int64(),
    StringValue.DESCRIPTOR: pa.string(),
    UInt32Value.DESCRIPTOR: pa.uint32(),
    UInt64Value.DESCRIPTOR: pa.uint64(),
}

# Mapping from protobuf primitive field types to pyarrow types.
# Not all available protobuf primitives are mapped. If you require a
# primitive type that is missing then you should add it here.
_PROTO_PRIMITIVE_TYPE_TO_PYARROW = {
    FieldDescriptorProto.TYPE_DOUBLE: pa.float64(),
    FieldDescriptorProto.TYPE_FLOAT: pa.float32(),
    FieldDescriptorProto.TYPE_INT64: pa.int64(),
    FieldDescriptorProto.TYPE_UINT64: pa.uint64(),
    FieldDescriptorProto.TYPE_INT32: pa.int32(),
    FieldDescriptorProto.TYPE_FIXED64: pa.uint64(),
    FieldDescriptorProto.TYPE_FIXED32: pa.uint32(),
    FieldDescriptorProto.TYPE_BOOL: pa.bool_(),
    FieldDescriptorProto.TYPE_STRING: pa.string(),
    FieldDescriptorProto.TYPE_BYTES: pa.binary(),
    FieldDescriptorProto.TYPE_UINT32: pa.uint32(),
    FieldDescriptorProto.TYPE_ENUM: pa.binary(),
    FieldDescriptorProto.TYPE_SFIXED32: pa.int32(),
    FieldDescriptorProto.TYPE_SFIXED64: pa.int64(),
    FieldDescriptorProto.TYPE_SINT32: pa.int32(),
    FieldDescriptorProto.TYPE_SINT64: pa.int64(),
}


def _time_of_day_to_nanos(time_of_day: TimeOfDay) -> int:
    return (
        (time_of_day.hours * 60 + time_of_day.minutes) * 60 + time_of_day.seconds
    ) * 1_000_000_000 + time_of_day.nanos


# Mapping from protobuf message descriptors to conversion functions used
# for translating protobuf messages to types suitable for pyarrow.
_PROTO_DESCRIPTOR_TO_ARROW_CONVERTER = {
    # TODO: remove this special case
    Date.DESCRIPTOR: lambda x: None
    if x.year == 0
    else datetime.date(x.year, x.month, x.day),
    Timestamp.DESCRIPTOR: lambda x: x.ToNanoseconds(),
    TimeOfDay.DESCRIPTOR: _time_of_day_to_nanos,
    BoolValue.DESCRIPTOR: lambda x: x.value,
    BytesValue.DESCRIPTOR: lambda x: x.value,
    DoubleValue.DESCRIPTOR: lambda x: x.value,
    FloatValue.DESCRIPTOR: lambda x: x.value,
    Int32Value.DESCRIPTOR: lambda x: x.value,
    Int64Value.DESCRIPTOR: lambda x: x.value,
    StringValue.DESCRIPTOR: lambda x: x.value,
    UInt32Value.DESCRIPTOR: lambda x: x.value,
    UInt64Value.DESCRIPTOR: lambda x: x.value,
}


@dataclasses.dataclass(frozen=True)
class FlattenedIterable(collections.abc.Iterable):
    parents: Iterable[Iterable[Any]]

    def __iter__(self) -> Iterator[Any]:
        for parent in self.parents:
            for child in parent:
                yield child


@dataclasses.dataclass(frozen=True)
class NestedIterable(collections.abc.Iterable):
    parents: Iterable[Message]
    getter: Callable[[Message], Any]

    def __iter__(self) -> Iterator[Any]:
        for parent in self.parents:
            yield self.getter(parent)


@dataclasses.dataclass(frozen=True)
class MapKeyIterable(collections.abc.Iterable):
    scalar_map: Iterable[ScalarMapContainer]

    def __iter__(self) -> Iterator[Any]:
        for scalar_map in self.scalar_map:
            for key in scalar_map.keys():
                yield key


@dataclasses.dataclass(frozen=True)
class MapValueIterable(collections.abc.Iterable):
    scalar_map: Iterable[ScalarMapContainer]

    def __iter__(self) -> Iterator[Any]:
        for scalar_map in self.scalar_map:
            for value in scalar_map.values():
                yield value


def get_enum_converter(
    data_type: pa.DataType, enum_descriptor: EnumDescriptor
) -> Callable[[int], Any]:
    if data_type == pa.int32():
        return lambda x: x

    elif data_type == pa.binary() or data_type == pa.dictionary(
        pa.int32(), pa.binary()
    ):
        values_by_number = {
            k: bytes(v.name, encoding="UTF-8")
            for k, v in enum_descriptor.values_by_number.items()
        }

        def converter(x: int) -> bytes:
            return values_by_number[x]

        return converter
    elif data_type == pa.string() or data_type == pa.dictionary(
        pa.int32(), pa.string()
    ):
        values_by_number = {
            k: v.name for k, v in enum_descriptor.values_by_number.items()
        }

        def converter(x: int) -> bytes:
            return values_by_number[x]

        return converter
    else:
        raise TypeError(data_type)


def _proto_field_to_array(
    records: Iterable[Message],
    field: FieldDescriptor,
    validity_mask: Optional[Iterable[bool]],
    config: ProtarrowConfig,
) -> pa.Array:
    if field.type == FieldDescriptorProto.TYPE_MESSAGE:
        converter = _PROTO_DESCRIPTOR_TO_ARROW_CONVERTER.get(field.message_type)
        if converter is None:
            return _message_to_array(
                records, field.message_type, validity_mask=validity_mask, config=config
            )
        else:
            pa_type = _PROTO_DESCRIPTOR_TO_PYARROW[field.message_type]

    elif field.type == FieldDescriptorProto.TYPE_ENUM:
        pa_type = config.enum_type
        converter = get_enum_converter(config.enum_type, field.enum_type)

    else:
        pa_type = _PROTO_PRIMITIVE_TYPE_TO_PYARROW.get(field.type)
        if pa_type is None:
            raise RuntimeError(
                f"Unsupported field type {FieldDescriptorProto.Type.Name(field.type)} "
                f"for {field.name}"
            )

        def converter(x: Any) -> Any:
            return x

    null_value = (
        None if field.type == FieldDescriptor.TYPE_MESSAGE else field.default_value
    )
    array = []
    for i, record in enumerate(records):
        if record is None or not (validity_mask is None or validity_mask[i]):
            value = null_value
        else:
            value = converter(record)
        array.append(value)
    return pa.array(array, pa_type)


def _get_offsets(
    records: Iterable[Union[RepeatedScalarContainer, ScalarMapContainer]]
) -> List[int]:
    last_offset = 0
    offsets = []
    for record in records:
        if record is None:
            offsets.append(None)
        else:
            offsets.append(last_offset)
            last_offset += len(record)
    offsets.append(last_offset)
    return offsets


def _repeated_proto_to_array(
    records: Iterable[RepeatedScalarContainer],
    field: FieldDescriptor,
    config: ProtarrowConfig,
) -> pa.ListArray:
    """
    Convert Protobuf embedded lists to a 1-dimensional PyArrow ListArray with offsets
    See PyArrow Layout format documentation on how to calculate offsets.
    """
    offsets = _get_offsets(records)
    array = _proto_field_to_array(FlattenedIterable(records), field, None, config)
    return pa.ListArray.from_arrays(
        offsets,
        array,
        pa.list_(
            pa.field(
                "item", array.type, nullable=field.type == FieldDescriptor.TYPE_MESSAGE
            )
        ),
    )


def _proto_map_to_array(
    records: Iterable[ScalarMapContainer],
    field: FieldDescriptor,
    config: ProtarrowConfig = ProtarrowConfig(),
) -> pa.MapArray:
    """
    Convert Protobuf maps to a 1-dimensional PyArrow MapArray with offsets
    See PyArrow Layout format documentation on how to calculate offsets.
    """
    key_field = field.message_type.fields_by_name["key"]
    value_field = field.message_type.fields_by_name["value"]
    offsets = _get_offsets(records)
    keys = _proto_field_to_array(
        MapKeyIterable(records),
        key_field,
        validity_mask=None,
        config=config,
    )
    values = _proto_field_to_array(
        MapValueIterable(records),
        value_field,
        validity_mask=None,
        config=config,
    )

    return pa.MapArray.from_arrays(offsets, keys, values)


def _proto_field_nullable(field: FieldDescriptor, is_struct: bool) -> bool:
    return field.type == FieldDescriptorProto.TYPE_MESSAGE or (
        is_struct and field.label == FieldDescriptorProto.LABEL_REPEATED
    )


def _proto_field_validity_mask(
    records: Iterable[Message], field: FieldDescriptor
) -> Optional[List[bool]]:
    if (
        field.type != FieldDescriptorProto.TYPE_MESSAGE
        or field.label == FieldDescriptorProto.LABEL_REPEATED
    ):
        return None
    mask = []
    field_name = field.name
    for record in records:
        if record is None:
            mask.append(False)
        else:
            mask.append(record.HasField(field_name))
    return mask


def _message_to_array(
    records: Iterable[Message],
    descriptor: Descriptor,
    validity_mask: Optional[Sequence[bool]],
    config: ProtarrowConfig,
) -> pa.StructArray:
    arrays = []
    fields = []

    for field in descriptor.fields:
        field_values = NestedIterable(records, operator.attrgetter(field.name))
        if field.message_type and field.message_type.GetOptions().map_entry:
            array = _proto_map_to_array(field_values, field, config)
        elif field.label == FieldDescriptorProto.LABEL_REPEATED:
            array = _repeated_proto_to_array(field_values, field, config)
        else:
            mask = _proto_field_validity_mask(records, field)
            array = _proto_field_to_array(
                field_values, field, validity_mask=mask, config=config
            )

        arrays.append(array)
        fields.append(
            pa.field(
                field.name,
                array.type,
                nullable=_proto_field_nullable(field, is_struct=True),
            )
        )
    return pa.StructArray.from_arrays(
        arrays=arrays,
        fields=fields,
        mask=pc.invert(pa.array(validity_mask, pa.bool_())) if validity_mask else None,
    )


def messages_to_record_batch(
    records: Iterable[M],
    message_type: Type[M],
    config: ProtarrowConfig = ProtarrowConfig(),
):
    return pa.RecordBatch.from_struct_array(
        _message_to_array(
            records, message_type.DESCRIPTOR, validity_mask=None, config=config
        )
    )


def messages_to_table(
    records: Iterable[M],
    message_type: Type[M],
    config: ProtarrowConfig = ProtarrowConfig(),
) -> pa.Table:
    """Converts a list of protobuf messages to a `pa.Table`"""
    assert isinstance(config, ProtarrowConfig), config
    record_batch = messages_to_record_batch(records, message_type, config=config)
    return pa.Table.from_batches([record_batch])
