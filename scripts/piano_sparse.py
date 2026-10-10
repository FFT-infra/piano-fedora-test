"""Android sparse 传输：零区使用 FILL，禁止不保证字节内容的 DONT_CARE。"""

import hashlib
import os
import struct
from pathlib import Path

BLOCK = 4096
MAGIC = 0xED26FF3A
RAW, FILL = 0xCAC1, 0xCAC2
BUFFER = 1024 * 1024


def require(value, message):
    if not value:
        raise ValueError(message)


def encode(source, output):
    source, output = Path(source), Path(output)
    size = source.stat().st_size
    require(source.is_file() and not source.is_symlink(), "expected a regular raw image")
    require(size > 0 and size % BLOCK == 0 and size // BLOCK <= 0xFFFFFFFF, "invalid raw image size")
    chunks = 0
    with source.open("rb", buffering=0) as src, output.open("x+b") as dest:
        dest.write(bytes(28))
        zeros = 0

        def flush_zeros():
            nonlocal zeros, chunks
            if zeros:
                dest.write(struct.pack("<HHII", FILL, 0, zeros, 16) + bytes(4))
                chunks += 1
                zeros = 0

        position = 0
        while position < size:
            # 镜像可能有数百 GiB 的洞；利用宿主文件系统的 extent 查询跳过它们。
            try:
                data_at = os.lseek(src.fileno(), position, os.SEEK_DATA)
            except OSError as error:
                if error.errno == 6:  # ENXIO: remaining bytes are a hole.
                    data_at = size
                elif error.errno in (22, 95):  # Unsupported SEEK_DATA: bounded reads.
                    data_at = position
                else:
                    raise
            data_at = min(size, data_at - data_at % BLOCK)
            if data_at > position:
                zeros += (data_at - position) // BLOCK
                position = data_at
                continue
            src.seek(position)
            data = src.read(min(BUFFER, size - position))
            require(data and len(data) % BLOCK == 0, "raw image changed or short read")
            blocks = len(data) // BLOCK
            if not any(data):
                zeros += blocks
            else:
                flush_zeros()
                dest.write(struct.pack("<HHII", RAW, 0, blocks, 12 + len(data)))
                dest.write(data)
                chunks += 1
            position += len(data)
        flush_zeros()
        require(source.stat().st_size == size, "raw image size changed during sparse encoding")
        dest.seek(0)
        dest.write(struct.pack("<IHHHHIIII", MAGIC, 1, 0, 28, 12, BLOCK, size // BLOCK, chunks, 0))
    return {"format": "android-sparse-f2fs", "expanded_bytes": size, "chunks": chunks,
            "dont_care_chunks": 0, "zero_policy": "explicit-zero-fill"}


def decode_chunks(path):
    """返回实际解码字节；严格校验所有长度，不接受 DONT_CARE 或尾随数据。"""
    with Path(path).open("rb") as stream:
        header = stream.read(28)
        require(len(header) == 28, "short sparse header")
        magic, major, minor, fh, ch, block, total, count, crc = struct.unpack("<IHHHHIIII", header)
        require((magic, major, minor, fh, ch, block, crc) == (MAGIC, 1, 0, 28, 12, BLOCK, 0),
                "unsupported sparse header")
        require(0 < total <= 0xFFFFFFFF and 0 < count <= total, "invalid sparse geometry")
        expanded = 0
        for _ in range(count):
            raw = stream.read(12)
            require(len(raw) == 12, "short sparse chunk")
            kind, reserved, blocks, length = struct.unpack("<HHII", raw)
            size = blocks * BLOCK
            require(reserved == 0 and blocks > 0 and expanded + size <= total * BLOCK, "sparse chunk exceeds image")
            if kind == RAW:
                require(length == 12 + size, "invalid RAW chunk length")
                while size:
                    data = stream.read(min(BUFFER, size))
                    require(data, "truncated RAW chunk")
                    size -= len(data)
                    yield data
            elif kind == FILL:
                require(length == 16, "invalid FILL chunk length")
                pattern = stream.read(4)
                require(len(pattern) == 4, "truncated FILL chunk")
                data = pattern * (BUFFER // 4)
                while size:
                    piece = data[:min(BUFFER, size)]
                    size -= len(piece)
                    yield piece
            else:
                raise ValueError("unsupported sparse chunk; DONT_CARE is forbidden")
            expanded += blocks * BLOCK
        require(expanded == total * BLOCK and not stream.read(1), "sparse length mismatch or trailing bytes")


def inspect(path):
    digest, size = hashlib.sha256(), 0
    for data in decode_chunks(path):
        digest.update(data)
        size += len(data)
    return {"expanded_sha256": digest.hexdigest(), "expanded_bytes": size,
            "dont_care_chunks": 0, "zero_policy": "explicit-zero-fill"}
