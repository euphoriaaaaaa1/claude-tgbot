# -*- coding: utf-8 -*-
"""老接口 `/api/image_provider` 的写路径保真（集成针①，F2 同病根治）。

它和参数页写的是**同一个** `_global.yml`：原来整表回写，切一次生图 provider
就把注释、锚点、merge key、键序全展平。这里盯三件事：值改对了、
除那几个标量之外**逐字节没动**、以及那三个 scope 各自只动该动的键。
"""
import pytest
import yaml

SRC = """\
# 顶部注释：切 provider 不该把我弄丢
user_display_name: 哥哥
max_calls_per_5h: 100

_defaults: &base
  retry: 3

moments:
  enabled: true
  image_generation:
    enabled: true
    provider: novelai          # novelai | comfyui
    provider_moment: novelai
    provider_telegram: novelai
    comfyui:
      url: http://127.0.0.1:8188
jiwen:
  delta_llm:
    <<: *base
  prompt: |
    块标量原样保留
  thresholds:
    notice: 0.20
"""


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(tmp_path))
    p = tmp_path / "_global.yml"
    p.write_text(SRC, encoding="utf-8")
    return p


@pytest.fixture
def client(cfg, monkeypatch):
    from moments import web
    web.app.config["TESTING"] = True
    return web.app.test_client()


def _changed_lines(before, after):
    b, a = before.splitlines(), after.splitlines()
    return [(i + 1, b[i] if i < len(b) else None, a[i] if i < len(a) else None)
            for i in range(max(len(b), len(a)))
            if (b[i] if i < len(b) else None) != (a[i] if i < len(a) else None)]


@pytest.mark.parametrize("payload,keys", [
    ({"scope": "telegram", "provider": "comfyui"}, ["provider_telegram"]),
    ({"scope": "moment", "provider": "comfyui"}, ["provider_moment"]),
    ({"provider": "comfyui"}, ["provider", "provider_moment", "provider_telegram"]),
])
def test_只动该动的那几行_其余逐字节不变(client, cfg, payload, keys):
    before = cfg.read_text(encoding="utf-8")
    r = client.post("/api/image_provider", json=payload)
    assert r.status_code == 200, r.get_data(as_text=True)
    after = cfg.read_text(encoding="utf-8")
    touched = _changed_lines(before, after)
    assert len(touched) == len(keys), touched
    for _, old, new in touched:
        assert old.split(":")[0] == new.split(":")[0], (old, new)   # 只换了值
        assert "comfyui" in new
    got = yaml.safe_load(after)["moments"]["image_generation"]
    assert [k for k in ("provider", "provider_moment", "provider_telegram")
            if got[k] == "comfyui"] == sorted(keys)


def test_注释锚点mergekey块标量全保住(client, cfg):
    client.post("/api/image_provider", json={"provider": "comfyui"})
    after = cfg.read_text(encoding="utf-8")
    for keep in ("# 顶部注释：切 provider 不该把我弄丢", "&base", "<<: *base",
                 "块标量原样保留", "notice: 0.20", "# novelai | comfyui"):
        assert keep in after, keep


def test_非法provider与scope仍是400且一个字节都没写(client, cfg):
    before = cfg.read_bytes()
    assert client.post("/api/image_provider", json={"provider": "midjourney"}).status_code == 400
    assert client.post("/api/image_provider",
                       json={"provider": "comfyui", "scope": "群聊"}).status_code == 400
    assert cfg.read_bytes() == before
