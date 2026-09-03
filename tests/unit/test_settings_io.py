# -*- coding: utf-8 -*-
"""§3.6 C 阶段：tmp + rename 的原子写、权限沿用、崩溃安全。全部落在 tmp_path 下。"""
import json
import os
import stat

import pytest

from moments.provider_model import HubError
from moments.settings_io import dir_writable, read_text, write_json_atomic


@pytest.fixture
def path(tmp_path):
    return tmp_path / "cfg" / "settings.json"


def test_读不存在的文件返回None(path):
    assert read_text(path) is None


def test_写了能读回来(path):
    path.parent.mkdir()
    write_json_atomic(path, {"env": {"K": "值"}})
    assert json.loads(read_text(path)) == {"env": {"K": "值"}}
    assert "值" in path.read_text(encoding="utf-8"), "中文被转义成 \\u，人没法手改了"


def test_新建文件权限是600(path):
    """文件里有 ANTHROPIC_AUTH_TOKEN 明文，644 等于对同机其他用户可读。"""
    path.parent.mkdir()
    write_json_atomic(path, {})
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_已有文件权限被沿用(path):
    path.parent.mkdir()
    path.write_text("{}", encoding="utf-8")
    os.chmod(path, 0o644)
    write_json_atomic(path, {"a": 1})
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o644, "把用户的 644 悄悄改成 600 了"


def test_覆写走的是替换不是截断(path):
    """rename 语义：中途崩溃时旧文件仍完整，不会读到半截 JSON。"""
    path.parent.mkdir()
    write_json_atomic(path, {"v": 1})
    ino = os.stat(path).st_ino
    write_json_atomic(path, {"v": 2})
    assert json.loads(read_text(path)) == {"v": 2}
    assert os.stat(path).st_ino != ino, "原地改写了同一个 inode → 不是原子替换"


def test_落盘失败不留临时文件_里面有明文令牌(path, monkeypatch):
    path.parent.mkdir()
    path.write_text("{}", encoding="utf-8")
    before = set(os.listdir(path.parent))

    def boom(*a, **kw):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(os, "replace", boom)     # 实现必须按属性调 os.replace，否则打不中
    with pytest.raises(HubError) as e:
        write_json_atomic(path, {"env": {"ANTHROPIC_AUTH_TOKEN": "sk-secret-9f3a"}})
    assert (e.value.status, e.value.error) == (500, "settings_write_failed")
    assert set(os.listdir(path.parent)) == before, "残留临时文件（含明文令牌）"
    assert path.read_text(encoding="utf-8") == "{}", "失败却动了原文件"


def test_临时文件名不可预测且在同一目录(path, monkeypatch):
    """[R4D] I9：固定名 settings.json.hubtmp 会被并发 activate 互相覆盖，且可预测。"""
    path.parent.mkdir()
    seen = []
    real = os.replace
    monkeypatch.setattr(os, "replace",
                        lambda a, b, *x, **k: (seen.append(str(a)), real(a, b))[1])
    write_json_atomic(path, {"a": 1})
    write_json_atomic(path, {"a": 2})
    assert len(seen) == 2 and seen[0] != seen[1], "两次用了同一个临时名"
    assert not any(s.endswith("settings.json.hubtmp") for s in seen)
    assert all(os.path.dirname(s) == str(path.parent) for s in seen), \
        "临时文件不在目标目录 → os.replace 可能跨设备失败"


def test_错误信息不带明文令牌(path, monkeypatch):
    path.parent.mkdir()
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError(5, "x")))
    with pytest.raises(HubError) as e:
        write_json_atomic(path, {"env": {"ANTHROPIC_AUTH_TOKEN": "sk-secret-9f3a"}})
    assert "sk-secret-9f3a" not in (e.value.detail + str(e.value))


def test_目录不存在时写失败给500(path):
    with pytest.raises(HubError) as e:
        write_json_atomic(path, {})              # cfg/ 还没建
    assert (e.value.status, e.value.error) == (500, "settings_write_failed")


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root 无视目录权限")
def test_目录不可写时探测为False且写失败给500(path):
    path.parent.mkdir()
    os.chmod(path.parent, 0o500)
    try:
        assert dir_writable(path) is False
        with pytest.raises(HubError) as e:
            write_json_atomic(path, {})
        assert (e.value.status, e.value.error) == (500, "settings_write_failed")
    finally:
        os.chmod(path.parent, 0o700)


def test_目录可写探测(path, tmp_path):
    path.parent.mkdir()
    assert dir_writable(path) is True
    assert dir_writable(tmp_path / "还不存在" / "settings.json") is True  # 探父目录


def test_读到损坏内容也照样回原文_解析是上层的事(path):
    """settings_io 只负责搬字节；解析与 409 判定在 provider_model.parse_settings。"""
    path.parent.mkdir()
    path.write_text("{ 坏掉的", encoding="utf-8")
    assert read_text(path) == "{ 坏掉的"


def test_读目录当文件时不冒充成不存在(tmp_path):
    """IsADirectoryError 是真故障，不能被吞成 None（那会让 activate 直接覆写掉目录路径）。"""
    with pytest.raises(HubError) as e:
        read_text(tmp_path)
    assert e.value.error == "config_unreadable"


# ---------------- Windows 文件被占用：重试 3×200ms → 409 settings_write_locked ----------------

def _fake_nt(monkeypatch, fail_times):
    """把 os.name 伪装成 nt，并让 os.replace 前 fail_times 次抛 PermissionError。"""
    import moments.settings_io as sio
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(sio.time, "sleep", lambda _s: None)   # 别真睡 200ms
    calls = []
    real = os.replace

    def flaky(a, b, *x, **k):
        calls.append(1)
        if len(calls) <= fail_times:
            raise PermissionError(13, "being used by another process")
        return real(a, b)
    monkeypatch.setattr(os, "replace", flaky)
    return calls


def test_windows被占用时重试后成功(path, monkeypatch):
    path.parent.mkdir()
    calls = _fake_nt(monkeypatch, fail_times=2)
    write_json_atomic(path, {"v": 1})
    assert len(calls) == 3
    assert json.loads(read_text(path)) == {"v": 1}


def test_windows重试三次仍被占用给409(path, monkeypatch):
    path.parent.mkdir()
    path.write_text("{}", encoding="utf-8")
    before = set(os.listdir(path.parent))
    calls = _fake_nt(monkeypatch, fail_times=99)
    with pytest.raises(HubError) as e:
        write_json_atomic(path, {"v": 1})
    assert (e.value.status, e.value.error) == (409, "settings_write_locked")
    assert len(calls) == 3, "重试次数不是 3"
    assert set(os.listdir(path.parent)) == before, "409 也不许留临时文件"


def test_posix上的PermissionError不当成占用(path, monkeypatch):
    """POSIX 的 PermissionError 是真没权限，重试无意义，走 500。"""
    path.parent.mkdir()
    monkeypatch.setattr(os, "replace",
                        lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "no")))
    with pytest.raises(HubError) as e:
        write_json_atomic(path, {})
    assert (e.value.status, e.value.error) == (500, "settings_write_failed")


def test_非UTF8的settings给409而不是裸UnicodeDecodeError(path):
    """BUG-06：GBK 存的 settings.json（Windows 记事本另存）。解码失败也是"解析不了"。"""
    path.parent.mkdir()
    path.write_bytes(b'{"env": {"X": "\xd6\xd0\xce\xc4"}}')
    with pytest.raises(HubError) as e:
        read_text(path)
    assert (e.value.status, e.value.error) == (409, "settings_unparsable")
