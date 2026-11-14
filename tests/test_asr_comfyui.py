"""ComfyUI 语音转录工作流（asr.engine=comfyui）测试。"""

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from taggycat.asr import (  # noqa: E402
    Transcript,
    _allocate_timings,
    _split_sentences,
    transcribe_comfyui,
    transcribe_video,
)
from taggycat.comfy import ComfyError, load_workflow  # noqa: E402
from taggycat.config import load_config, set_config_values  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

ASR_WF = {
    "1": {"class_type": "LoadAudio", "inputs": {"audio": "old.wav"}},
    "2": {"class_type": "WhisperNode",
          "inputs": {"audio": ["1", 0], "model": "large-v3"}},
    "3": {"class_type": "ShowText", "inputs": {"text": ["2", 0]}},
}

SEGMENTS_JSON = json.dumps({
    "segments": [
        {"start": 0.5, "end": 2.0, "text": " 你好，世界 "},
        {"start": 2.0, "end": 4.2, "text": "第二句台词"},
    ],
}, ensure_ascii=False)


@pytest.fixture
def asr_cfg(tmp_path):
    cfg = load_config(ROOT / "config.example.toml")
    wf = tmp_path / "asr_workflow.json"
    wf.write_text(json.dumps(ASR_WF), encoding="utf-8")
    wav = tmp_path / "audio_16k.wav"
    wav.write_bytes(b"RIFFxxxxWAVEfmt ")
    cfg.asr.enabled = True
    cfg.asr.engine = "comfyui"
    cfg.asr.comfyui.workflow = str(wf)
    cfg.asr.comfyui.poll_interval = 0.0
    return cfg


@pytest.fixture(scope="module")
def app():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    @property
    def ok(self) -> bool:
        return self.status_code == 200

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _mock_comfy(monkeypatch, history_text: str, calls: list | None = None):
    """mock /upload/image + /prompt + /history。"""
    import taggycat.comfy as C

    calls = calls if calls is not None else []

    def fake_post(url, **kw):
        calls.append(("POST", url, kw))
        if url.endswith("/upload/image"):
            return _Resp({"name": "audio_16k.wav", "subfolder": "", "type": "input"})
        if url.endswith("/prompt"):
            return _Resp({"prompt_id": "pid-1"})
        raise AssertionError(f"意外请求: {url}")

    def fake_get(url, **kw):
        calls.append(("GET", url, kw))
        if "/history/" in url:
            return _Resp({"pid-1": {
                "status": {"status_str": "success"},
                "outputs": {"3": {"ui": {"text": [history_text]}}},
            }})
        raise AssertionError(f"意外请求: {url}")

    from types import SimpleNamespace

    monkeypatch.setattr(
        C, "get_session",
        lambda: SimpleNamespace(post=fake_post, get=fake_get),
    )
    return calls


# ---------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------
def test_example_config_has_asr_comfyui_section():
    cfg = load_config(ROOT / "config.example.toml")
    assert cfg.asr.engine == "faster_whisper"
    assert cfg.asr.comfyui.base_url.startswith("http")
    assert cfg.asr.comfyui.workflow.endswith("asr_workflow.json")
    assert cfg.asr.comfyui.timeout == 1800
    assert cfg.asr.comfyui.max_upload_mb == 90


def test_asr_comfyui_write_roundtrip(tmp_path):
    p = tmp_path / "config.toml"
    p.write_bytes((ROOT / "config.example.toml").read_bytes())
    set_config_values(p, [
        ("asr.enabled", True),
        ("asr.engine", "comfyui"),
        ("asr.comfyui.base_url", "http://192.168.2.2:8188"),
        ("asr.comfyui.workflow", "resources/workflows/my_asr.json"),
        ("asr.comfyui.audio_input", "audio"),
        ("asr.comfyui.max_upload_mb", 120.0),
    ])
    cfg = load_config(p)
    assert cfg.asr.engine == "comfyui"
    assert cfg.asr.comfyui.base_url == "http://192.168.2.2:8188"
    assert cfg.asr.comfyui.workflow == "resources/workflows/my_asr.json"
    assert cfg.asr.comfyui.audio_input == "audio"
    assert cfg.asr.comfyui.max_upload_mb == 120.0


# ---------------------------------------------------------------------
# 工作流加载 / 打补丁
# ---------------------------------------------------------------------
def test_load_workflow_rejects_ui_format(tmp_path):
    ui = tmp_path / "ui.json"
    ui.write_text(json.dumps({"nodes": [{"id": 1}], "links": []}), encoding="utf-8")
    with pytest.raises(ComfyError, match="API"):
        load_workflow(ui)


def test_load_workflow_missing_file(tmp_path):
    with pytest.raises(ComfyError, match="不存在"):
        load_workflow(tmp_path / "nope.json")


def test_patch_audio_auto(tmp_path):
    from taggycat.asr import _patch_audio_prompt

    out = _patch_audio_prompt(dict(ASR_WF), "new.wav", audio_key="")
    assert out["1"]["inputs"]["audio"] == "new.wav"


def test_patch_audio_explicit_key(tmp_path):
    from taggycat.asr import _patch_audio_prompt

    wf = {"9": {"class_type": "CustomASR", "inputs": {"my_audio": "x.wav"}}}
    out = _patch_audio_prompt(wf, "new.wav", audio_key="my_audio")
    assert out["9"]["inputs"]["my_audio"] == "new.wav"


def test_patch_audio_not_found_raises():
    from taggycat.asr import _patch_audio_prompt

    wf = {"9": {"class_type": "TextOnly", "inputs": {"text": "hi"}}}
    with pytest.raises(ValueError, match="音频输入"):
        _patch_audio_prompt(wf, "new.wav", audio_key="")


# ---------------------------------------------------------------------
# transcribe_comfyui（mock HTTP）
# ---------------------------------------------------------------------
def test_comfyui_json_segments(asr_cfg, tmp_path, monkeypatch):
    calls = _mock_comfy(monkeypatch, SEGMENTS_JSON)
    tr = transcribe_comfyui(tmp_path / "audio_16k.wav", asr_cfg.asr)
    assert isinstance(tr, Transcript)
    assert [i.text for i in tr.items] == ["你好，世界", "第二句台词"]
    assert tr.items[0].start == 0.5 and tr.items[1].end == 4.2
    # /prompt 提交体：LoadAudio 已换成上传后的音频名
    prompt_payloads = [kw["json"]["prompt"]
                       for m, u, kw in calls
                       if m == "POST" and u.endswith("/prompt")]
    assert prompt_payloads and prompt_payloads[0]["1"]["inputs"]["audio"] == "audio_16k.wav"


def test_comfyui_plain_text_fallback(asr_cfg, tmp_path, monkeypatch):
    _mock_comfy(monkeypatch, "昏暗的房间里，男子坐在桌前。他翻阅着文件！")
    monkeypatch.setattr(
        "taggycat.media.ffmpeg.media_duration", lambda p: 10.0
    )
    tr = transcribe_comfyui(tmp_path / "audio_16k.wav", asr_cfg.asr)
    assert len(tr.items) == 2
    assert tr.items[0].start == 0.0
    assert tr.items[-1].end == 10.0
    assert tr.items[0].end <= tr.items[1].start + 1e-6


def test_comfyui_missing_workflow_config(asr_cfg, tmp_path):
    asr_cfg.asr.comfyui.workflow = ""
    with pytest.raises(ComfyError, match="asr.comfyui.workflow"):
        transcribe_comfyui(tmp_path / "audio_16k.wav", asr_cfg.asr)


def test_comfyui_no_text_output(asr_cfg, tmp_path, monkeypatch):
    _mock_comfy(monkeypatch, "audio_16k.wav")   # 只有文件名，无台词
    with pytest.raises(ComfyError, match="无文本"):
        transcribe_comfyui(tmp_path / "audio_16k.wav", asr_cfg.asr)


def test_comfyui_ui_format_rejected(asr_cfg, tmp_path):
    ui = tmp_path / "ui.json"
    ui.write_text(json.dumps({"nodes": [{"id": 1}]}), encoding="utf-8")
    asr_cfg.asr.comfyui.workflow = str(ui)
    with pytest.raises(ComfyError, match="API"):
        transcribe_comfyui(tmp_path / "audio_16k.wav", asr_cfg.asr)


# ---------------------------------------------------------------------
# 分段上传（asr.comfyui.max_upload_mb，解决长视频 413）
# ---------------------------------------------------------------------
def _make_wav(path: Path, seconds: float = 2.0, rate: int = 16000) -> Path:
    import wave

    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


def test_split_wav_under_limit_returns_original(tmp_path):
    from taggycat.asr import _split_wav_for_upload

    fake = tmp_path / "audio_16k.wav"
    fake.write_bytes(b"RIFFxxxxWAVEfmt ")  # 非真实 wav：未超限时不得解析
    chunks, durs = _split_wav_for_upload(fake, 1048576)
    assert chunks == [fake]
    assert durs == []


def test_split_wav_over_limit_chunks(tmp_path):
    import wave

    from taggycat.asr import _split_wav_for_upload

    wav = _make_wav(tmp_path / "real_16k.wav", seconds=2.0)
    max_bytes = 40192  # (40192-8192)//2 = 16000 帧/段 → 恰好 2 段
    chunks, durs = _split_wav_for_upload(wav, max_bytes)
    assert len(chunks) == 2
    assert all(c.exists() for c in chunks)
    assert chunks[0].name == "real_16k.part0.wav"
    assert abs(sum(durs) - 2.0) < 0.01
    for c in chunks:
        assert c.stat().st_size <= max_bytes
        with wave.open(str(c), "rb") as w:
            assert w.getnframes() == 16000
        c.unlink()


def test_comfyui_multichunk_json_offsets(asr_cfg, tmp_path, monkeypatch):
    calls = _mock_comfy(monkeypatch, SEGMENTS_JSON)
    wav = _make_wav(tmp_path / "real_16k.wav", seconds=2.0)
    asr_cfg.asr.comfyui.max_upload_mb = 40192 / 1048576  # → 2 段×1s
    tr = transcribe_comfyui(wav, asr_cfg.asr)
    # 每段返回同一 JSON（0.5-4.2s 两句），第 2 段整体 +1s 偏移
    assert [round(i.start, 2) for i in tr.items] == [0.5, 1.5, 2.0, 3.0]
    assert [round(i.end, 2) for i in tr.items] == [2.0, 3.0, 4.2, 5.2]
    uploads = [c for c in calls
               if c[0] == "POST" and c[1].endswith("/upload/image")]
    assert len(uploads) == 2
    assert not list(tmp_path.glob("*.part*.wav"))  # 分段临时文件已清理


def test_comfyui_multichunk_plain_text(asr_cfg, tmp_path, monkeypatch):
    _mock_comfy(monkeypatch, "昏暗的房间里，男子坐在桌前。他翻阅着文件！")
    wav = _make_wav(tmp_path / "real_16k.wav", seconds=2.0)
    asr_cfg.asr.comfyui.max_upload_mb = 40192 / 1048576
    tr = transcribe_comfyui(wav, asr_cfg.asr)
    assert len(tr.items) == 4  # 2 分段 × 2 句
    assert tr.items[0].start == 0.0
    assert tr.items[2].start >= 1.0  # 第 2 段按段起点偏移
    assert tr.items[-1].end == 2.0


# ---------------------------------------------------------------------
# 纯函数 / 路由
# ---------------------------------------------------------------------
def test_split_and_allocate():
    sents = _split_sentences("第一句。第二句！第三句")
    assert len(sents) == 3
    items = _allocate_timings(sents, 30.0)
    assert items[-1].end == 30.0
    assert abs(sum(i.end - i.start for i in items) - 30.0) < 0.01


def test_transcribe_video_routes_to_comfyui(tmp_path, monkeypatch):
    cfg = load_config(ROOT / "config.example.toml")
    cfg.asr.engine = "comfyui"
    seen = {}

    monkeypatch.setattr("taggycat.asr.extract_audio", lambda *a, **k: None)

    def fake_comfy(audio, c, root_dir=None):
        seen["audio"] = audio
        seen["root_dir"] = root_dir
        return Transcript(language="zh", items=[])

    monkeypatch.setattr("taggycat.asr.transcribe_comfyui", fake_comfy)
    tr = transcribe_video(
        tmp_path / "v.mp4", cfg.asr,
        work_dir=tmp_path / "w", root_dir=tmp_path,
    )
    assert tr.items == []
    assert seen["root_dir"] == tmp_path
    assert Path(seen["audio"]).name == "audio_16k.wav"


def test_transcribe_video_routes_to_local(tmp_path, monkeypatch):
    cfg = load_config(ROOT / "config.example.toml")
    cfg.asr.engine = "faster_whisper"
    called = {}
    monkeypatch.setattr("taggycat.asr.extract_audio", lambda *a, **k: None)

    def fake_local(audio, c):
        called["local"] = audio
        return Transcript(language="zh", items=[])

    monkeypatch.setattr("taggycat.asr.transcribe_audio", fake_local)
    tr = transcribe_video(tmp_path / "v.mp4", cfg.asr, work_dir=tmp_path / "w")
    assert tr.items == [] and "local" in called


# ---------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------
def test_doctor_asr_comfyui_requires_workflow():
    import taggycat.doctor as D

    cfg = load_config(ROOT / "config.example.toml")
    cfg.asr.engine = "comfyui"
    cfg.asr.comfyui.workflow = ""
    chk = D._asr_check(cfg)
    assert chk.level == D.FAIL
    assert "asr.comfyui.workflow" in chk.message


def test_doctor_asr_comfyui_workflow_missing_file(tmp_path):
    import taggycat.doctor as D

    cfg = load_config(ROOT / "config.example.toml")
    cfg.asr.engine = "comfyui"
    cfg.asr.comfyui.workflow = str(tmp_path / "nope.json")
    chk = D._asr_check(cfg)
    assert chk.level == D.FAIL
    assert "不存在" in chk.message


def test_doctor_asr_comfyui_unreachable_warns(tmp_path):
    import taggycat.doctor as D

    cfg = load_config(ROOT / "config.example.toml")
    wf = tmp_path / "asr.json"
    wf.write_text(json.dumps(ASR_WF), encoding="utf-8")
    cfg.asr.engine = "comfyui"
    cfg.asr.comfyui.workflow = str(wf)
    cfg.asr.comfyui.base_url = "http://127.0.0.1:9"
    chk = D._asr_check(cfg)
    assert chk.level == D.WARN
    assert "不可达" in chk.message


def test_doctor_asr_comfyui_ok(tmp_path, monkeypatch):
    import taggycat.doctor as D

    cfg = load_config(ROOT / "config.example.toml")
    wf = tmp_path / "asr.json"
    wf.write_text(json.dumps(ASR_WF), encoding="utf-8")
    cfg.asr.engine = "comfyui"
    cfg.asr.comfyui.workflow = str(wf)
    monkeypatch.setattr(D, "_has_module", lambda n: True)
    monkeypatch.setattr("requests.get", lambda url, **kw: _Resp({"system": {}}))
    chk = D._asr_check(cfg)
    assert chk.level == D.OK
    assert "ComfyUI" in chk.message


def test_doctor_faster_whisper_cublas_warn(monkeypatch):
    import taggycat.doctor as D

    cfg = load_config(ROOT / "config.example.toml")
    cfg.asr.engine = "faster_whisper"
    cfg.asr.device = "cuda"
    monkeypatch.setattr(D, "_has_module", lambda n: True)
    monkeypatch.setattr(D, "_cublas_available", lambda: False)
    chk = D._asr_check(cfg)
    assert chk.level == D.WARN
    assert "comfyui" in chk.message
    monkeypatch.setattr(D, "_cublas_available", lambda: True)
    chk2 = D._asr_check(cfg)
    assert chk2.level == D.OK


def test_doctor_unknown_engine():
    import taggycat.doctor as D

    cfg = load_config(ROOT / "config.example.toml")
    cfg.asr.engine = "whisper.cpp"
    chk = D._asr_check(cfg)
    assert chk.level == D.FAIL
    assert "未知 ASR 引擎" in chk.message


# ---------------------------------------------------------------------
# 设置界面
# ---------------------------------------------------------------------
def test_settings_dialog_asr_tab(app):
    from taggycat.ui.settings_dialog import SettingsDialog

    cfg = load_config(ROOT / "config.example.toml")
    dlg = SettingsDialog(cfg)
    # 默认本地页
    assert dlg.cb_asr_engine.currentData() == "faster_whisper"
    assert dlg.asr_stack.currentIndex() == 0
    # 切到 ComfyUI 页
    dlg.cb_asr_engine.setCurrentIndex(1)
    assert dlg.asr_stack.currentIndex() == 1
    dlg.ed_asr_comfy_url.setText("http://192.168.2.2:8188")
    dlg.ed_asr_comfy_workflow.setText("resources/workflows/my_asr.json")
    dlg.ed_asr_audio_input.setText("audio")
    dlg.sp_asr_maxmb.setValue(120)
    values = dict(dlg._collect())
    assert values["asr.engine"] == "comfyui"
    assert values["asr.comfyui.base_url"] == "http://192.168.2.2:8188"
    assert values["asr.comfyui.workflow"] == "resources/workflows/my_asr.json"
    assert values["asr.comfyui.audio_input"] == "audio"
    assert values["asr.comfyui.max_upload_mb"] == 120.0
    assert values["asr.enabled"] is True
    dlg.close()
