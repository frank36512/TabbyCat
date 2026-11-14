"""配置加载测试。"""

from pathlib import Path

import pytest

from taggycat.config import AppConfig, find_config, load_config

ROOT = Path(__file__).resolve().parent.parent


def test_load_example_config():
    cfg = load_config(ROOT / "config.example.toml")
    assert isinstance(cfg, AppConfig)
    assert cfg.llm.model == "deepseek-chat"
    assert cfg.tts.engine == "edge"
    assert cfg.material.material_ratio == 30
    assert cfg.review.validate_script is True
    assert cfg.output.width == 1920
    assert cfg.root_dir == ROOT


def test_resolve_paths():
    cfg = load_config(ROOT / "config.example.toml")
    p = cfg.resolve("workspace")
    assert p.is_absolute()
    assert p == ROOT / "workspace"
    # 绝对路径原样返回
    abs_p = Path("D:/tmp/x")
    assert cfg.resolve(abs_p) == abs_p


def test_missing_config_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TAGGYCAT_CONFIG", raising=False)
    with pytest.raises(FileNotFoundError):
        find_config()


def test_env_config(tmp_path, monkeypatch):
    f = tmp_path / "my.toml"
    f.write_text('[llm]\nmodel = "test-model"\n', encoding="utf-8")
    monkeypatch.setenv("TAGGYCAT_CONFIG", str(f))
    cfg = load_config()
    assert cfg.llm.model == "test-model"


def test_defaults_when_sections_missing(tmp_path):
    f = tmp_path / "empty.toml"
    f.write_text("", encoding="utf-8")
    cfg = load_config(f)
    assert cfg.output.fps == 30
    assert cfg.queue.max_concurrent == 1
