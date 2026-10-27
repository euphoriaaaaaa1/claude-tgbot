# -*- coding: utf-8 -*-
"""T10 旧 provider 一次性迁移单测。

黑盒跑 CLI：临时 HOME + 三个接缝全指 tmp，不读真实 ~/.claude-tgbot，
不启动旧 cliproxy/Hub，不连上游。
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "migrate_legacy_providers.py"

FAKE_ANT = "sk-ant-legacy-secret-111"
FAKE_DS = "sk-ds-legacy-secret-222"
FAKE_DOWN = "sk-downstream-secret-000"
FAKE_HUB = "sk-hub-legacy-secret-333"

CLIPROXY = """\
host: "127.0.0.1"
port: 8317
api-keys:
  - "%s"
claude-api-key:
  - api-key: "%s"
    base-url: "https://anthropic.example.com"
    prefix: ""
    models:
      - name: "claude-sonnet-4-5-20250929"
        alias: "claude-sonnet-4-5-20250929"
openai-compatibility:
  - name: "DeepSeek"
    base-url: "https://api.deepseek.com/v1"
    api-key-entries:
      - api-key: "%s"
    models:
      - name: "deepseek-v4-flash"
        alias: "claude-sonnet-4-5-20250929"
""" % (FAKE_DOWN, FAKE_ANT, FAKE_DS)


class Sandbox:
    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home"
        self.configs = root / "configs"
        self.channels = root / "channels"
        self.proxy = root / "proxy"
        for directory in (self.home, self.configs, self.channels, self.proxy):
            directory.mkdir(parents=True)
        self.target = self.configs / "providers.json"

    def env(self):
        env = os.environ.copy()
        for key in (
            "MOMENTS_WEB_MODE", "MOMENTS_WEB_HOST", "MOMENTS_WEB_PORT",
            "CLAUDEBOT_CONFIG_DIR", "CLAUDEBOT_CHANNELS_ROOT", "CLAUDEBOT_PROVIDER_PATH",
            "CLAUDEBOT_PROXY_DIR", "CLAUDEBOT_PROXY_PORT", "CLAUDEBOT_PROXY_BIN",
            "CLAUDEBOT_PROXY_LAUNCHCTL", "CLAUDEBOT_TEST_MODE",
        ):
            env.pop(key, None)
        env.update({
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "PYTHONPATH": str(REPO),
            "PYTHONDONTWRITEBYTECODE": "1",
            "CLAUDE_TGBOT_HOME": str(self.root / "tgbot-home"),
            "CLAUDEBOT_CONFIG_DIR": str(self.configs),
            "CLAUDEBOT_CHANNELS_ROOT": str(self.channels),
            "CLAUDEBOT_PROVIDER_PATH": str(self.target),
            "CLAUDEBOT_PROXY_DIR": str(self.proxy),
            "CLAUDEBOT_PROXY_PORT": "0",
            "CLAUDEBOT_PROXY_LAUNCHCTL": "off",
        })
        return env

    def run(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT)] + [str(a) for a in args],
            cwd=str(REPO), env=self.env(), text=True, capture_output=True,
            timeout=120)

    def write_cliproxy(self, text=CLIPROXY):
        path = self.root / "cliproxy-config.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def write_hub(self, text=""):
        path = self.root / "hub.env"
        path.write_text(text, encoding="utf-8")
        return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _combined(result) -> str:
    return (result.stdout or "") + (result.stderr or "")


def test_dry_run_zero_write_and_no_secret_output(tmp_path):
    box = Sandbox(tmp_path)
    cliproxy = box.write_cliproxy()
    hub = box.write_hub("CLIPROXY_PORT=8317\nCLIPROXY_API_KEY=%s\n" % FAKE_HUB)
    before = (_sha(cliproxy), _sha(hub))

    result = box.run("--dry-run", "--from-cliproxy", cliproxy, "--from-hub", hub)

    assert result.returncode == 0, _combined(result)
    assert not box.target.exists(), "dry-run 不得写 providers.json"
    assert not (box.configs / "providers.lock").exists(), "dry-run 不得创建清单锁文件"
    assert (_sha(cliproxy), _sha(hub)) == before, "dry-run 不得修改旧文件"
    output = _combined(result)
    assert "供应商 2 个" in output and "模型 2 个" in output, output
    assert "sk-" not in output, "迁移输出不得包含密钥形状"
    assert "api_key" not in output.lower(), "迁移输出不得包含密钥字段名"


def test_apply_imports_providers_models_and_preserves_bindings(tmp_path):
    box = Sandbox(tmp_path)
    cliproxy = box.write_cliproxy()
    hub = box.write_hub("CLIPROXY_PORT=8317\n")
    before = (_sha(cliproxy), _sha(hub))
    binding = {
        "provider_id": "p_old", "model": "old-model",
        "base_url": "https://old.example.com", "key_fp": "fp",
        "applied_at": "2026-10-01T00:00:00Z", "pending_restart": False,
        "pending_since": None, "forced_at": None,
    }
    box.target.write_text(json.dumps(
        {"providers": [], "bindings": {"mybot": binding}}), encoding="utf-8")

    result = box.run("--apply", "--from-cliproxy", cliproxy, "--from-hub", hub)

    assert result.returncode == 0, _combined(result)
    assert (_sha(cliproxy), _sha(hub)) == before, "apply 不得修改旧文件"
    data = json.loads(box.target.read_text(encoding="utf-8"))
    assert len(data["providers"]) == 2
    assert data["bindings"] == {"mybot": binding}, "既有绑定必须原样保留"
    by_protocol = {p["protocol"]: p for p in data["providers"]}
    assert set(by_protocol) == {"anthropic", "openai"}
    assert by_protocol["anthropic"]["api_key"] == FAKE_ANT
    assert by_protocol["openai"]["api_key"] == FAKE_DS
    assert by_protocol["anthropic"]["models"] == [
        {"id": "claude-sonnet-4-5-20250929", "label": "claude-sonnet-4-5-20250929"}]
    assert by_protocol["openai"]["models"] == [
        {"id": "deepseek-v4-flash", "label": "deepseek-v4-flash"}]
    assert by_protocol["openai"]["default_model"] == "deepseek-v4-flash"
    assert all(p["proxy"] is False for p in data["providers"])
    assert "api_key" not in _combined(result).lower()
    assert "sk-" not in _combined(result)


def test_apply_is_idempotent(tmp_path):
    box = Sandbox(tmp_path)
    cliproxy = box.write_cliproxy()
    hub = box.write_hub("")

    first = box.run("--apply", "--from-cliproxy", cliproxy, "--from-hub", hub)
    assert first.returncode == 0, _combined(first)
    snapshot = _sha(box.target)

    second = box.run("--apply", "--from-cliproxy", cliproxy, "--from-hub", hub)
    assert second.returncode == 0, _combined(second)
    assert _sha(box.target) == snapshot, "重复执行不得改动 providers.json"
    data = json.loads(box.target.read_text(encoding="utf-8"))
    assert len(data["providers"]) == 2, "重复执行不得产生重复供应商"
    assert "跳过: 2 个" in second.stdout, second.stdout


def test_same_name_is_skipped_and_existing_entry_untouched(tmp_path):
    box = Sandbox(tmp_path)
    cliproxy = box.write_cliproxy()
    hub = box.write_hub("")
    existing = {
        "id": "p_existing", "name": "DeepSeek", "base_url": "https://keep.example.com",
        "api_key": "sk-existing-keep", "models": [{"id": "kept", "label": "kept"}],
        "default_model": "kept", "created_at": 1, "updated_at": 1,
        "proxy": False, "protocol": "openai",
        "anthropic_base_url": "", "openai_base_url": "",
    }
    box.target.write_text(json.dumps(
        {"providers": [existing], "bindings": {}}), encoding="utf-8")

    result = box.run("--apply", "--from-cliproxy", cliproxy, "--from-hub", hub)

    assert result.returncode == 0, _combined(result)
    data = json.loads(box.target.read_text(encoding="utf-8"))
    assert len(data["providers"]) == 2, "同名条目应跳过而不是覆盖"
    kept = [p for p in data["providers"] if p["name"] == "DeepSeek"]
    assert len(kept) == 1 and kept[0]["base_url"] == "https://keep.example.com"
    assert "同名已存在，跳过" in result.stdout, result.stdout
    assert "sk-existing-keep" not in _combined(result)


def test_corrupt_yaml_exits_1_without_target_or_secret_leak(tmp_path):
    box = Sandbox(tmp_path)
    bad = box.write_cliproxy(
        'claude-api-key:\n  - api-key: "%s"\n    base-url: [unclosed\n' % FAKE_ANT)

    result = box.run("--apply", "--from-cliproxy", bad)

    assert result.returncode == 1, _combined(result)
    assert not box.target.exists(), "解析失败不得写 providers.json"
    output = _combined(result)
    assert "sk-" not in output, "YAML 错误信息不得带出密钥"
    assert "api_key" not in output.lower()


def test_empty_sources_exit_0_and_do_not_create_target(tmp_path):
    box = Sandbox(tmp_path)
    cliproxy = box.write_cliproxy("")
    hub = box.write_hub("")

    dry = box.run("--dry-run", "--from-cliproxy", cliproxy, "--from-hub", hub)
    assert dry.returncode == 0, _combined(dry)
    assert not box.target.exists()

    apply_result = box.run("--apply", "--from-cliproxy", cliproxy, "--from-hub", hub)
    assert apply_result.returncode == 0, _combined(apply_result)
    assert not box.target.exists(), "无迁移项时 apply 不得创建 providers.json"


def test_missing_sources_exit_0(tmp_path):
    box = Sandbox(tmp_path)
    result = box.run("--apply",
                     "--from-cliproxy", tmp_path / "nope.yaml",
                     "--from-hub", tmp_path / "nope.env")
    assert result.returncode == 0, _combined(result)
    assert not box.target.exists()


def test_hub_env_flat_provider_is_imported(tmp_path):
    box = Sandbox(tmp_path)
    cliproxy = box.write_cliproxy("")
    hub = box.write_hub(
        "ANTHROPIC_BASE_URL=https://gw.example.com\n"
        "ANTHROPIC_AUTH_TOKEN=%s\n"
        "ANTHROPIC_MODEL=m1,m2\n" % FAKE_HUB)

    result = box.run("--apply", "--from-cliproxy", cliproxy, "--from-hub", hub)

    assert result.returncode == 0, _combined(result)
    data = json.loads(box.target.read_text(encoding="utf-8"))
    assert len(data["providers"]) == 1
    provider = data["providers"][0]
    assert provider["protocol"] == "anthropic"
    assert provider["base_url"] == "https://gw.example.com"
    assert [m["id"] for m in provider["models"]] == ["m1", "m2"]
    assert "sk-" not in _combined(result)


def test_missing_optional_fields_degrade_instead_of_failing(tmp_path):
    box = Sandbox(tmp_path)
    cliproxy = box.write_cliproxy(
        'claude-api-key:\n  - api-key: "%s"\n    base-url: "https://gw.example.com"\n' % FAKE_ANT)
    hub = box.write_hub("")

    result = box.run("--apply", "--from-cliproxy", cliproxy, "--from-hub", hub)

    assert result.returncode == 0, _combined(result)
    data = json.loads(box.target.read_text(encoding="utf-8"))
    assert len(data["providers"]) == 1
    provider = data["providers"][0]
    assert provider["name"] == "gw.example.com", "缺名字应降级为地址主机名"
    assert provider["models"] == [] and provider["default_model"] == ""
    assert "降级: 1 个" in result.stdout, result.stdout


def test_corrupt_existing_providers_json_exits_1_and_is_unchanged(tmp_path):
    box = Sandbox(tmp_path)
    cliproxy = box.write_cliproxy()
    box.target.write_text("{not json", encoding="utf-8")
    before = _sha(box.target)

    result = box.run("--apply", "--from-cliproxy", cliproxy)

    assert result.returncode == 1, _combined(result)
    assert _sha(box.target) == before, "现有清单解析失败时不得覆盖"


def test_argument_errors_exit_2(tmp_path):
    box = Sandbox(tmp_path)
    both = box.run("--apply", "--dry-run")
    assert both.returncode == 2, _combined(both)
    unknown = box.run("--bogus")
    assert unknown.returncode == 2, _combined(unknown)
