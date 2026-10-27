"""需求⑤ 公开版白盒：config_loader 的配置目录与 bots_registry.configs_dir() 同源（每次现读 HUB_CONFIGS_DIR）。

导入期快照会让"注册表判停用"与"voicecall/moments 读 bot 配置"指向两份目录；
既有测试直接 monkeypatch config_loader.CONFIGS_DIR 的写法仍须生效。
"""
import config_loader


def _yml(d, bot_id):
    d.mkdir(exist_ok=True)
    (d / f"{bot_id}.yml").write_text(f"id: {bot_id}\n", encoding="utf-8")


def test_未改CONFIGS_DIR_每次现读HUB_CONFIGS_DIR(tmp_path, monkeypatch):
    _yml(tmp_path / "a", "a")
    _yml(tmp_path / "b", "b")
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(tmp_path / "a"))
    assert [c["_bot_id"] for c in config_loader.list_enabled_bots()] == ["a"]
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(tmp_path / "b"))
    assert [c["_bot_id"] for c in config_loader.list_enabled_bots()] == ["b"]
    assert config_loader.load_bot("b")["id"] == "b"


def test_显式改过CONFIGS_DIR_以它为准(tmp_path, monkeypatch):
    _yml(tmp_path / "x", "x")
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(tmp_path / "nope"))
    monkeypatch.setattr(config_loader, "CONFIGS_DIR", str(tmp_path / "x"))
    assert config_loader.load_bot("x")["id"] == "x"
