"""对象原子写（EXEC §3.1）：临时文件 → 流式 SHA-256 → fsync → 同盘原子 rename。

崩溃留下的孤立对象先核对引用再处理，绝不扫目录直删。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


class ObjectStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # key 由服务端生成（audio_id/artifact_id 派生）；仍拒绝穿越
        if ".." in key or key.startswith("/"):
            raise ValueError(f"illegal object key: {key!r}")
        return self.root / key

    def put_bytes(self, key: str, data: bytes) -> str:
        return self.put_stream(key, io_bytes=data)

    def put_stream(self, key: str, *, io_bytes: bytes | None = None, chunk_source=None) -> str:
        """写入并返回 sha256。io_bytes 或 chunk_source(iterable[bytes]) 二选一。"""
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        hasher = hashlib.sha256()
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as tmp:
                if io_bytes is not None:
                    hasher.update(io_bytes)
                    tmp.write(io_bytes)
                else:
                    for chunk in chunk_source:
                        hasher.update(chunk)
                        tmp.write(chunk)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise
        return hasher.hexdigest()

    def read_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        p = self._path(key)
        try:
            p.unlink()
        except FileNotFoundError:
            pass
