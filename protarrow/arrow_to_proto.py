import collections.abc
import dataclasses
from typing import Any, Callable, Iterable, Iterator, List, Optional, Type, Union

import pyarrow as pa
import pyarrow.compute as pc
from google.protobuf.descriptor import Descriptor, EnumDescriptor, FieldDescriptor
from google.protobuf.message import Message
from google.protobuf.pyext._message import ScalarMapContainer
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

from protarrow.common import M

_TIMESTAMP_CONVERTER = {"ns": 1, "us": 1_000, "ms": 1_000_000, "s": 1_000_000_000}
_TIME_CONVERTER = {pa.time64("ns"): 1, pa.time64("us"): 1_000}


def _timestamp_scalar_to_proto(scalar: pa.Scalar) -> Timestamp:
    timestamp = Timestamp()
    value = scalar.value * _TIMESTAMP_CONVERTER[scalar.type.unit]
    timestamp.FromNanoseconds(value)
    return timestamp


def _prepare_array(array: pa.Array) -> pa.Array:
    if isinstance(array, pa.Time64Array):
        # TODO: handle units correctly when this is fixed
        #  https://issues.apache.org/jira/browse/ARROW-18257
        ratio = _TIME_CONVERTER[array.type]
        return pc.multiply(array.cast(pa.int64()), pa.scalar(ratio, pa.int64()))
    else:
        return array


def _date_scalar_to_proto(scalar: pa.Scalar) -> Date:
    date = scalar.as_py()
    return Date(year=date.year, month=date.month, day=date.day)


def _time_of_day_scalar_to_proto(scalar: pa.Scalar) -> TimeOfDay:
    total_nanos = scalar.as_py()
    return TimeOfDay(
        nanos=total_nanos % 1_000_000_000,
        seconds=(total_nanos // 1_000_000_000) % 60,
        minutes=(total_nanos // 60_000_000_000) % 60,
        hours=(total_nanos // 3600_000_000_000),
    )


SPECIAL_TYPES = {
    Timestamp.DESCRIPTOR: _timestamp_scalar_to_proto,
    Date.DESCRIPTOR: _date_scalar_to_proto,
    TimeOfDay.DESCRIPTOR: _time_of_day_scalar_to_proto,
}

NULLABLE_TYPES = (
    BoolValue.DESCRIPTOR,
    BytesValue.DESCRIPTOR,
    DoubleValue.DESCRIPTOR,
    FloatValue.DESCRIPTOR,
    Int32Value.DESCRIPTOR,
    Int64Value.DESCRIPTOR,
    StringValue.DESCRIPTOR,
    UInt32Value.DESCRIPTOR,
    UInt64Value.DESCRIPTOR,
)


def is_custom_field(field_descriptor: FieldDescriptor):
    return (
        field_descriptor.type == FieldDescriptor.TYPE_MESSAGE
        and field_descriptor.message_type not in SPECIAL_TYPES
        and field_descriptor.message_type not in NULLABLE_TYPES
    )


@dataclasses.dataclass(frozen=True)
class OffsetToSize(collections.abc.Iterable):
    array: Union[pa.ListArray, pa.MapArray]

    def __post_init__(self):
        assert pa.types.is_integer(self.array.type)

    def __iter__(self) -> Iterator[int]:
        current_offset = self.array[0].as_py()
        for item in self.array[1:]:
            offset = item.as_py()
            yield offset - current_offset
            current_offset = offset


@dataclasses.dataclass(frozen=True)
class OptionalNestedIterable(collections.abc.Iterable):
    parents: Iterable[Message]
    field_descriptor: FieldDescriptor
    validity_mask: Iterable[pa.BooleanScalar]

    def __iter__(self) -> Iterator[Any]:
        fake_message = self.field_descriptor.message_type._concrete_class()
        for parent, valid in zip(self.parents, self.validity_mask):
            if valid.is_valid and valid.as_py():
                yield getattr(parent, self.field_descriptor.name)
            else:
                yield fake_message


@dataclasses.dataclass(frozen=True)
class RepeatedNestedIterable(collections.abc.Iterable):
    parents: Iterable[Message]
    field_descriptor: FieldDescriptor

    def __post_init__(self):
        assert self.field_descriptor.label == FieldDescriptor.LABEL_REPEATED
        assert self.field_descriptor.type == FieldDescriptor.TYPE_MESSAGE

    def __iter__(self) -> Iterator[Any]:
        for parent in self.parents:
            for child in getattr(parent, self.field_descriptor.name):
                yield child


def convert_scalar(scalar: pa.Scalar) -> Any:
    return scalar.as_py()


def get_converter(field_descriptor: FieldDescriptor) -> Callable[[pa.Scalar], Any]:
    if field_descriptor.type == FieldDescriptor.TYPE_ENUM:
        enum_descriptor: EnumDescriptor = field_descriptor.enum_type

        def convert_enum(scalar: pa.Scalar) -> Optional[int]:
            enum_value = enum_descriptor.values_by_name.get(scalar.as_py(), None)
            return enum_value.number if enum_value else None

        return convert_enum
    elif field_descriptor.type == FieldDescriptor.TYPE_MESSAGE:
        if field_descriptor.message_type in NULLABLE_TYPES:
            return convert_scalar
        else:
            try:
                return SPECIAL_TYPES[field_descriptor.message_type]
            except KeyError:
                raise KeyError(field_descriptor.full_name)
    else:
        return convert_scalar


class PlainAssigner(collections.abc.Iterable):
    def __init__(self, messages: Iterable[Message], field_descriptor: FieldDescriptor):
        self.messages = messages
        self.field_descriptor = field_descriptor
        self.converter = get_converter(field_descriptor)
        self.nullable = self.field_descriptor.message_type in NULLABLE_TYPES
        self.message = None

    def __iter__(self) -> Iterator[Callable[[pa.Scalar], None]]:
        assert self.message is None
        for message in self.messages:
            self.message = message
            yield self
        self.message = None

    def __call__(self, scalar: pa.Scalar) -> None:
        if scalar.is_valid:
            value = self.converter(scalar)
            if value is not None:
                if self.nullable:
                    getattr(self.message, self.field_descriptor.name).value = value
                else:
                    setattr(self.message, self.field_descriptor.name, value)


class AppendAssigner(collections.abc.Iterable):
    def __init__(
        self,
        messages: Iterable[Message],
        field_descriptor: FieldDescriptor,
        sizes: Iterable[int],
        converter: Callable[[Any], Any],
    ):
        self.messages = messages
        self.field_descriptor = field_descriptor
        assert self.field_descriptor.label == FieldDescriptor.LABEL_REPEATED
        self.sizes = sizes
        self.converter = converter
        self.attribute = None

    def __iter__(self) -> Iterator[Callable[[pa.Scalar], None]]:
        assert self.attribute is None
        for message, size in zip(self.messages, self.sizes):
            self.attribute = getattr(message, self.field_descriptor.name)
            for _ in range(size):
                yield self
        self.attribute = None

    def __call__(self, scalar: pa.Scalar) -> None:
        self.attribute.append(self.converter(scalar))


@dataclasses.dataclass
class MapKeyAssigner(collections.abc.Iterable):
    messages: Iterable[Message]
    field_descriptor: FieldDescriptor
    offsets: Iterable[int]
    converter: Callable[[pa.Scalar], Any] = dataclasses.field(init=False)
    attribute: Any = None

    def __post_init__(self):
        assert self.field_descriptor.label == FieldDescriptor.LABEL_REPEATED
        assert self.field_descriptor.message_type.GetOptions().map_entry
        self.converter = get_converter(
            self.field_descriptor.message_type.fields_by_name["key"]
        )

    def __iter__(self) -> Iterator[Callable[[pa.Scalar], Message]]:
        assert self.attribute is None
        for message, offset in zip(self.messages, self.offsets):
            self.attribute = getattr(message, self.field_descriptor.name)
            for _ in range(offset):
                yield self
        self.attribute = None

    def __call__(self, scalar: pa.Scalar) -> Message:
        return self.attribute[self.converter(scalar)]


def _direct_assign(attribute: ScalarMapContainer, key: Any, value: Any):
    attribute[key] = value


def _merge_assign(attribute: ScalarMapContainer, key: Any, value: Any):
    if value is None:
        attribute[key]
    else:
        attribute[key].MergeFrom(value)


@dataclasses.dataclass
class MapItemAssigner(collections.abc.Iterable):
    messages: Iterable[Message]
    field_descriptor: FieldDescriptor
    offsets: Iterable[int]
    key_converter: Callable[[pa.Scalar], Any] = dataclasses.field(init=False)
    value_converter: Callable[[pa.Scalar], Any] = dataclasses.field(init=False)
    assigner: Callable[[ScalarMapContainer, Any, Any], None] = dataclasses.field(
        init=False
    )
    attribute: Optional[ScalarMapContainer] = None

    def __post_init__(self):
        assert self.field_descriptor.label == FieldDescriptor.LABEL_REPEATED
        assert self.field_descriptor.message_type.GetOptions().map_entry
        self.key_converter = get_converter(
            self.field_descriptor.message_type.fields_by_name["key"]
        )
        value_descriptor = self.field_descriptor.message_type.fields_by_name["value"]
        self.value_converter = get_converter(value_descriptor)
        self.assigner = (
            _merge_assign
            if (value_descriptor.type == FieldDescriptor.TYPE_MESSAGE)
            else _direct_assign
        )

    def __iter__(self) -> Iterator[Callable[[pa.Scalar, pa.Scalar], Message]]:
        assert self.attribute is None
        for message, offset in zip(self.messages, self.offsets):
            self.attribute = getattr(message, self.field_descriptor.name)
            for _ in range(offset):
                yield self
        self.attribute = None

    def __call__(self, key: pa.Scalar, value: pa.Scalar):
        self.assigner(
            self.attribute,
            self.key_converter(key),
            self.value_converter(value) if value.is_valid else None,
        )


def _extract_struct_field(
    array: pa.StructArray,
    field_descriptor: FieldDescriptor,
    messages: Iterable[Message],
) -> None:
    nested_list = OptionalNestedIterable(messages, field_descriptor, array.is_valid())
    _extract_array_messages(array, field_descriptor.message_type, nested_list)


def _extract_map_field(
    array: pa.MapArray,
    field_descriptor: FieldDescriptor,
    messages: Iterable[Message],
) -> None:
    assert pa.types.is_map(array.type), array.type
    value_type = field_descriptor.message_type.fields_by_name["value"]

    if is_custom_field(value_type):
        # Because protobuf doesn't warranty orders of map,
        # we have to make a copy of the list of values here
        values = []
        for assigner, key in zip(
            MapKeyAssigner(messages, field_descriptor, OffsetToSize(array.offsets)),
            array.keys,
        ):
            values.append(assigner(key))

        assert pa.types.is_struct(array.type.item_type), array.type
        item_type: pa.StructType = array.type.item_type
        assert isinstance(item_type, pa.StructType)

        for field_descriptor in value_type.message_type.fields:
            field_index = item_type.get_field_index(field_descriptor.name)
            if field_index != -1:
                _extract_field(
                    array.values.field(1).field(field_index),
                    field_descriptor,
                    values,
                )

    else:
        for assigner, key, value in zip(
            MapItemAssigner(messages, field_descriptor, OffsetToSize(array.offsets)),
            array.keys,
            _prepare_array(array.values.field(1)),
        ):
            assigner(key, value)


def _extract_repeated_field(
    array: pa.Array,
    field_descriptor: FieldDescriptor,
    messages: Iterable[Message],
) -> None:
    if is_custom_field(field_descriptor):
        if field_descriptor.message_type.GetOptions().map_entry:
            _extract_map_field(array, field_descriptor, messages)
        else:
            _extract_repeated_message(array, field_descriptor, messages)
    else:
        _extract_repeated_primitive_assigner(array, field_descriptor, messages)


def _extract_repeated_primitive_assigner(
    array: pa.Array, field_descriptor: FieldDescriptor, messages: Iterable[Message]
) -> None:
    assigner = AppendAssigner(
        messages=messages,
        field_descriptor=field_descriptor,
        sizes=OffsetToSize(array.offsets),
        converter=get_converter(field_descriptor),
    )

    for each_assigner, value in zip(assigner, _prepare_array(array.values)):
        each_assigner(value)


def _extract_repeated_message(
    array: pa.Array, field_descriptor: FieldDescriptor, messages: Iterable[Message]
):
    assert pa.types.is_list(array.type)
    child = field_descriptor.message_type._concrete_class()
    assigner = AppendAssigner(
        messages,
        field_descriptor,
        OffsetToSize(array.offsets),
        lambda x: x,
    )
    for each_assigner, value in zip(assigner, array.values):
        each_assigner(child)
    _extract_array_messages(
        array.values,
        field_descriptor.message_type,
        RepeatedNestedIterable(messages, field_descriptor),
    )


def _extract_field(
    array: pa.Array, field_descriptor: FieldDescriptor, messages: Iterable[Message]
) -> None:
    array = _prepare_array(array)
    if field_descriptor.label == FieldDescriptor.LABEL_REPEATED:
        _extract_repeated_field(array, field_descriptor, messages)
    elif field_descriptor.message_type in SPECIAL_TYPES:
        extractor = SPECIAL_TYPES[field_descriptor.message_type]
        for message, value in zip(messages, array):
            if value.is_valid:
                getattr(
                    message,
                    field_descriptor.name,
                ).MergeFrom(extractor(value))
    elif (
        field_descriptor.type == FieldDescriptor.TYPE_MESSAGE
        and field_descriptor.message_type not in NULLABLE_TYPES
    ):
        _extract_struct_field(array, field_descriptor, messages)
    else:
        plain_assigner = PlainAssigner(messages, field_descriptor)
        for plain_assigner, value in zip(plain_assigner, array):
            if value.is_valid:
                plain_assigner(value)


def _extract_record_batch_messages(
    record_batch: pa.RecordBatch,
    message_descriptor: Descriptor,
    messages: Iterable[Message],
) -> None:
    for field_descriptor in message_descriptor.fields:
        if field_descriptor.name in record_batch.schema.names:
            _extract_field(
                record_batch[field_descriptor.name], field_descriptor, messages
            )


def _extract_array_messages(
    array: pa.StructArray,
    message_descriptor: Descriptor,
    messages: Iterable[Message],
) -> None:
    assert pa.types.is_struct(array.type), array.type
    assert isinstance(array, pa.StructArray)
    struct_type: pa.StructType = array.type
    for field_descriptor in message_descriptor.fields:
        index = struct_type.get_field_index(field_descriptor.name)
        if index != -1:
            _extract_field(array.field(index), field_descriptor, messages)


def record_batch_to_messages(
    record_batch: pa.RecordBatch, message_type: Type[M]
) -> List[M]:
    messages = [message_type() for _ in range(record_batch.num_rows)]
    _extract_record_batch_messages(record_batch, message_type.DESCRIPTOR, messages)
    return messages


def table_to_messages(table: pa.Table, message_type: Type[M]) -> List[M]:
    messages = []
    for batch in table.to_reader():
        messages.extend(record_batch_to_messages(batch, message_type))
    return messages
