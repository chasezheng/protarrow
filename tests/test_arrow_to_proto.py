import datetime
import json
import pathlib
from typing import Type

import pyarrow as pa
import pytest
from google.protobuf.json_format import MessageToDict, Parse
from google.protobuf.reflection import GeneratedProtocolMessageType

from protarrow.arrow_to_proto import table_to_messages
from protarrow.common import M
from protarrow.proto_to_arrow import messages_to_table
from protarrow_protos.simple_pb2 import NestedTestMessage, TestMessage
from tests.random_generator import generate_messages, random_date

MESSAGES = [TestMessage, NestedTestMessage]


def read_proto_jsonl(path: pathlib.Path, message_type: Type[M]) -> list[M]:
    """
    Reads a jsonl file into a list of protobuf messages.
    Pass in "s3://tradewell-<env>/foo/bar" for files on S3.
    Empty lines and comment lines starting with '#' are ignored.
    """
    with path.open() as fp:
        return [
            Parse(line.strip(), message_type())
            for line in fp
            if line.strip() and not line.startswith("#")
        ]


@pytest.mark.parametrize("message_type", [TestMessage, NestedTestMessage])
def test_arrow_to_proto_empty(message_type: GeneratedProtocolMessageType):
    table = messages_to_table([], message_type)
    messages = table_to_messages(table, message_type)
    assert messages == []


@pytest.mark.parametrize("message_type", MESSAGES)
def test_with_random(message_type: GeneratedProtocolMessageType):
    source_messages = generate_messages(message_type, 10)
    table = messages_to_table(source_messages, message_type)
    messages_back = table_to_messages(table, message_type)
    assert source_messages == messages_back


@pytest.mark.parametrize("message_type", MESSAGES)
def test_with_sample_data(message_type: GeneratedProtocolMessageType):
    source_file = (
        pathlib.Path(__file__).parent / "data" / f"{message_type.DESCRIPTOR.name}.jsonl"
    )
    source_messages = read_proto_jsonl(source_file, message_type)
    table = messages_to_table(source_messages, message_type)
    messages_back = table_to_messages(table, message_type)
    assert source_messages == messages_back


def test_wrapped_type_nullable():
    expected_types = {
        "wrapped_double": pa.float64(),
        "wrapped_float": pa.float32(),
        "wrapped_int32": pa.int32(),
        "wrapped_int64": pa.int64(),
        "wrapped_uint32": pa.uint32(),
        "wrapped_uint64": pa.uint64(),
        "wrapped_bool": pa.bool_(),
        "wrapped_string": pa.string(),
        "wrapped_bytes": pa.binary(),
    }

    table = messages_to_table([], TestMessage)
    schema = table.schema
    for name, expected_type in expected_types.items():
        field = schema.field(name)
        assert field.type == expected_type
        assert field.nullable is True


def test_native_type_not_nullable():
    expected_types = {
        "double_value": pa.float64(),
        "float_value": pa.float32(),
        "int32_value": pa.int32(),
        "int64_value": pa.int64(),
        "uint32_value": pa.uint32(),
        "uint64_value": pa.uint64(),
        "bool_value": pa.bool_(),
        "string_value": pa.string(),
        "bytes_value": pa.binary(),
    }

    table = messages_to_table([], TestMessage)
    schema = table.schema
    for name, expected_type in expected_types.items():
        field = schema.field(name)
        assert field.type == expected_type
        assert field.nullable is False


def test_range():
    datetime.date.max - datetime.date.min
    random_date()


def test_arrow_bug_is_not_fixed():
    dtype = pa.time64("ns")
    time_array = pa.array([1, 2, 3], dtype)
    assert pa.types.is_time64(time_array.type) is True
    assert isinstance(dtype, pa.Time64Type) is True
    assert isinstance(time_array.type, pa.Time64Type) is False  # Wrong
    assert isinstance(time_array.type, pa.DataType) is True  # Wrong
    assert dtype == time_array.type
    assert dtype.unit == "ns"
    with pytest.raises(
        AttributeError, match=r"'pyarrow.lib.DataType' object has no attribute 'unit'"
    ):
        # Should be able to access unit:
        time_array.type.unit


def test_generate_random():
    for message_type in MESSAGES:
        messages = generate_messages(message_type, 20)
        file_name = message_type.DESCRIPTOR.name + ".jsonl"
        print(file_name)
        with open(file_name, "w") as fp:
            for message in messages:
                json.dump(MessageToDict(message, preserving_proto_field_name=True), fp)
