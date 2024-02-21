# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import json
import os
import tempfile

from dataclasses import dataclass
from typing import ClassVar

# pyre-ignore[21]: Could not find module `executorch.exir._serialize._bindings`.
import executorch.exir._serialize._bindings as bindings  # @manual=//executorch/exir/_serialize:_bindings

import pkg_resources

from executorch.backends.vulkan.serialization.vulkan_graph_schema import VkGraph
from executorch.exir._serialize._dataclass import _DataclassEncoder


def convert_to_flatbuffer(vk_graph: VkGraph) -> bytes:
    vk_graph_json = json.dumps(vk_graph, cls=_DataclassEncoder)

    with tempfile.TemporaryDirectory() as d:
        schema_path = os.path.join(d, "schema.fbs")
        with open(schema_path, "wb") as schema_file:
            schema_file.write(pkg_resources.resource_string(__name__, "schema.fbs"))
        json_path = os.path.join(d, "schema.json")
        with open(json_path, "wb") as json_file:
            json_file.write(vk_graph_json.encode("ascii"))
        # pyre-ignore
        bindings.flatc_compile(d, schema_path, json_path)
        output_path = os.path.join(d, "schema.bin")
        with open(output_path, "rb") as output_file:
            return output_file.read()


@dataclass
class VulkanDelegateHeader:
    # Defines the byte region that each component of the header corresponds to
    MAGIC_IX: ClassVar[slice] = slice(4, 8)
    HEADER_SIZE_IX: ClassVar[slice] = slice(8, 10)
    FLATBUFFER_OFFSET_IX: ClassVar[slice] = slice(10, 14)
    FLATBUFFER_SIZE_IX: ClassVar[slice] = slice(14, 18)
    BYTES_OFFSET_IX: ClassVar[slice] = slice(18, 22)
    BYTES_SIZE_IX: ClassVar[slice] = slice(22, 30)

    # magic bytes that should be at the beginning of the header
    EXPECTED_MAGIC: ClassVar[bytes] = b"VH00"
    # The length of the header in bytes
    EXPECTED_LENGTH: ClassVar[int] = 30

    # Instance attributes, @dataclass will turn these into constructor args
    flatbuffer_offset: int
    flatbuffer_size: int
    bytes_offset: int
    bytes_size: int

    @staticmethod
    def from_bytes(data: bytes) -> "VulkanDelegateHeader":
        if len(data) > VulkanDelegateHeader.EXPECTED_LENGTH:
            raise ValueError(
                f"Expected header to be {VulkanDelegateHeader.EXPECTED_LENGTH} bytes, "
                f"but got {len(data)} bytes."
            )

        magic_b: bytes = data[VulkanDelegateHeader.MAGIC_IX]

        if magic_b != VulkanDelegateHeader.EXPECTED_MAGIC:
            raise ValueError(
                f"Expected magic bytes to be {VulkanDelegateHeader.EXPECTED_MAGIC}, "
                f"but got {magic_b}."
            )

        length: int = int.from_bytes(
            data[VulkanDelegateHeader.HEADER_SIZE_IX], byteorder="little"
        )

        if length != VulkanDelegateHeader.EXPECTED_LENGTH:
            raise ValueError(
                f"Expected header to be {VulkanDelegateHeader.EXPECTED_LENGTH} bytes, "
                f"but got {length} bytes."
            )

        flatbuffer_offset_b: bytes = data[VulkanDelegateHeader.FLATBUFFER_OFFSET_IX]
        flatbuffer_size_b: bytes = data[VulkanDelegateHeader.FLATBUFFER_SIZE_IX]
        bytes_offset_b: bytes = data[VulkanDelegateHeader.BYTES_OFFSET_IX]
        bytes_size_b: bytes = data[VulkanDelegateHeader.BYTES_SIZE_IX]

        return VulkanDelegateHeader(
            flatbuffer_offset=int.from_bytes(flatbuffer_offset_b, byteorder="little"),
            flatbuffer_size=int.from_bytes(flatbuffer_size_b, byteorder="little"),
            bytes_offset=int.from_bytes(bytes_offset_b, byteorder="little"),
            bytes_size=int.from_bytes(bytes_size_b, byteorder="little"),
        )

    def is_valid(self) -> bool:
        if self.flatbuffer_size <= 0:
            return False

        expected_offset = self.flatbuffer_offset + self.flatbuffer_size
        if self.bytes_offset < expected_offset:
            return False

        if self.bytes_size < 0:
            return False

        return True

    def to_bytes(self) -> bytes:
        if not self.is_valid():
            raise ValueError("VulkanDelegateHeader instance contains invalid values")

        data: bytes = (
            # 4 bytes of padding for magic bytes, this is so that the header magic
            # bytes is in the same position as the flatbuffer header magic bytes
            b"\x00\x00\x00\x00"
            + self.EXPECTED_MAGIC
            + self.EXPECTED_LENGTH.to_bytes(2, byteorder="little")
            + self.flatbuffer_offset.to_bytes(4, byteorder="little")
            + self.flatbuffer_size.to_bytes(4, byteorder="little")
            + self.bytes_offset.to_bytes(4, byteorder="little")
            + self.bytes_size.to_bytes(8, byteorder="little")
        )

        assert len(data) == VulkanDelegateHeader.EXPECTED_LENGTH

        return data
