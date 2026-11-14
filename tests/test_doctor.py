"""doctor 自检测试。"""

from pathlib import Path

from taggycat.config import load_config
from taggycat.doctor import FAIL, OK, WARN, format_report, run_checks

ROOT = Path(__file__).resolve().parent.parent


def _cfg(tmp_path):
    cfg = load_config(ROOT / "config.example.toml")
    cfg.root_dir = tmp_path          # 避免污染仓库目录
    return cfg


def test_checks_with_example_config(tmp_path):
    cfg = _cfg(tmp_path)
    checks = {c.name: c for c in run_checks(cfg)}

    assert checks["ffmpeg"].level == OK
    assert checks["paths"].level == OK
    assert checks["llm"].level == WARN        # sk-REPLACE → 回退本地
    assert checks["vlm"].level == WARN        # 未配 Key → 降级台词轨
    assert checks["tts"].level == OK          # edge 引擎已安装
    assert not [c for c in checks.values() if c.level == FAIL]


def test_llm_fail_when_totally_unconfigured(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.llm.api_key = ""
    cfg.llm_local.base_url = ""
    checks = {c.name: c for c in run_checks(cfg)}
    assert checks["llm"].level == FAIL


def test_tts_comfy_missing_workflow(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.tts.engine = "comfyui"
    cfg.tts.comfyui.workflow = "no/such/workflow.json"
    checks = {c.name: c for c in run_checks(cfg)}
    assert checks["tts"].level == FAIL
    assert "不存在" in checks["tts"].message


def test_tts_unknown_engine(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.tts.engine = "bogus"
    checks = {c.name: c for c in run_checks(cfg)}
    assert checks["tts"].level == FAIL


def test_format_report(tmp_path):
    checks = run_checks(_cfg(tmp_path))
    text = format_report(checks)
    assert "自检报告" in text
    assert "ffmpeg" in text
    assert "失败 0" in text


def test_cli_doctor(tmp_path, capsys):
    from taggycat.cli import build_parser, cmd_doctor

    args = build_parser().parse_args(["doctor"])
    code = cmd_doctor(args)
    out = capsys.readouterr().out
    assert code == 0            # 示例配置仅警告、无失败
    assert "自检报告" in out


def test_config_template_copies_identical():
    """仓库根与包内模板必须一致（wheel 安装靠包内模板 init）。"""
    root = ROOT / "config.example.toml"
    pkg = ROOT / "taggycat" / "config.example.toml"
    assert pkg.exists(), "包内缺少 config.example.toml"
    assert root.read_bytes() == pkg.read_bytes(), (
        "两份 config.example.toml 不一致，请重新复制到 taggycat/"
    )


def test_unknown_config_keys_warn(tmp_path):
    cfg = _cfg(tmp_path)
    bad = tmp_path / "bad.toml"
    bad.write_text(
        'bogus_top = 1\n[llm]\napi_key2 = "x"\n',
        encoding="utf-8",
    )
    checks = {c.name: c for c in run_checks(cfg, config_path=bad)}
    assert "config" in checks
    assert checks["config"].level == WARN
    assert "bogus_top" in checks["config"].message
    assert "llm.api_key2" in checks["config"].message


def test_known_config_keys_ok(tmp_path):
    cfg = _cfg(tmp_path)
    checks = {c.name: c for c in run_checks(cfg, config_path=ROOT / "config.example.toml")}
    assert checks["config"].level == OK
