"""原子对象写与孤立 tmp 清理（EXEC §3.1）。"""

from pathlib import Path

from oido.storage.objects import ObjectStore


def test_put_and_hash(tmp_path: Path):
    store = ObjectStore(tmp_path / "obj")
    sha = store.put_bytes("originals/aud_x", b"hello oido")
    import hashlib
    assert sha == hashlib.sha256(b"hello oido").hexdigest()
    assert store.read_bytes("originals/aud_x") == b"hello oido"
    assert not list((tmp_path / "obj" / "originals").glob(".tmp-*")), "tmp 文件必须被 rename 掉"


def test_no_tmp_left_on_error(tmp_path: Path):
    store = ObjectStore(tmp_path / "obj")
    def exploding_chunks():
        yield b"part1"
        raise RuntimeError("disk went away")
    try:
        store.put_stream("originals/aud_y", chunk_source=exploding_chunks())
    except RuntimeError:
        pass
    assert not list((tmp_path / "obj" / "originals").glob(".tmp-*")), "失败路径不留 tmp"
    assert not store.exists("originals/aud_y")


def test_path_traversal_rejected(tmp_path: Path):
    store = ObjectStore(tmp_path / "obj")
    for bad in ("../escape", "/abs/path", "a/../../b"):
        try:
            store.put_bytes(bad, b"x")
            raised = False
        except ValueError:
            raised = True
        assert raised, f"key {bad!r} must be rejected"
    assert not (tmp_path / "escape").exists()


def test_delete_idempotent(tmp_path: Path):
    store = ObjectStore(tmp_path / "obj")
    store.put_bytes("k", b"v")
    store.delete("k")
    store.delete("k")  # 不抛
    assert not store.exists("k")
