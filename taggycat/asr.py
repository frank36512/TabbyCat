"""ASR：台词转录（faster-whisper 本地 / ComfyUI 工作流）。"""

from __future__ import annotations

import copy
import re
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, Field

from taggycat.config import ASRConfig
from taggycat.media import extract_audio
from taggycat.paths import resolve_path


class TranscriptItem(BaseModel):
    start: float
    end: float
    text: str


class Transcript(BaseModel):
    """带时间戳的台词表。"""

    language: str = ""
    items: list[TranscriptItem] = Field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(i.text for i in self.items)

    @property
    def duration(self) -> float:
        return self.items[-1].end if self.items else 0.0

    def plain_text(self, max_chars: int = 0) -> str:
        s = " ".join(i.text.strip() for i in self.items)
        return s if not max_chars else s[:max_chars]

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> Transcript:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


_model_cache: dict[str, object] = {}


def _get_model(cfg: ASRConfig):
    """按 (model, device, compute_type) 缓存 WhisperModel。"""
    key = f"{cfg.model}|{cfg.device}|{cfg.compute_type}"
    if key not in _model_cache:
        from faster_whisper import WhisperModel

        logger.info(
            "加载 faster-whisper 模型: {} ({} / {})", cfg.model, cfg.device, cfg.compute_type
        )
        try:
            model = WhisperModel(cfg.model, device=cfg.device, compute_type=cfg.compute_type)
        except Exception as e:  # noqa: BLE001
            logger.warning("CUDA 加载失败({})，回退 CPU/int8", e)
            model = WhisperModel(cfg.model, device="cpu", compute_type="int8")
        _model_cache[key] = model
    return _model_cache[key]


def transcribe_audio(
    audio_path: str | Path,
    cfg: ASRConfig,
    *,
    vad_filter: bool | None = None,
) -> Transcript:
    """wav → Transcript（本地 faster-whisper）。"""
    model = _get_model(cfg)
    segments, info = model.transcribe(
        str(audio_path),
        language=cfg.language or None,
        beam_size=cfg.beam_size,
        vad_filter=cfg.vad_filter if vad_filter is None else vad_filter,
        vad_parameters={"min_silence_duration_ms": 500},
        word_timestamps=False,
    )
    items: list[TranscriptItem] = []
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        items.append(TranscriptItem(start=float(seg.start), end=float(seg.end), text=text))
    tr = Transcript(language=getattr(info, "language", "") or cfg.language, items=items)
    logger.info("ASR 完成: {} 段台词, 语言={}", len(tr.items), tr.language)
    return tr


# ---------------------------------------------------------------------
# ComfyUI 工作流转录
# ---------------------------------------------------------------------
_AUDIO_KEYS = {"audio", "audio_file", "audio_path", "input_audio", "wav",
               "file", "filename"}
_AUDIO_NODE_TYPES = {"LoadAudio", "LoadAudioUpload", "LoadAudioNode"}


def _patch_audio_prompt(prompt: dict, audio_name: str, *, audio_key: str) -> dict:
    """把上传的音频写入工作流（LoadAudio 节点或显式 audio_input 键）。"""
    patched_audio = False
    for _nid, node in prompt.items():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        is_audio_node = node.get("class_type") in _AUDIO_NODE_TYPES
        for key, val in inputs.items():
            if patched_audio:
                break
            lk = key.lower()
            if audio_key:
                if key == audio_key:
                    inputs[key] = audio_name
                    patched_audio = True
            elif (
                (is_audio_node and lk in ("audio", "file"))
                or (lk in _AUDIO_KEYS and isinstance(val, (str, type(None))) and val)
            ):
                inputs[key] = audio_name
                patched_audio = True
    if not patched_audio:
        raise ValueError(
            f"工作流中未找到音频输入（audio_input='{audio_key or '自动'}'），"
            "请在配置 asr.comfyui.audio_input 指定输入名"
        )
    return prompt


def _items_from_json(data) -> list[TranscriptItem] | None:
    """从工作流 JSON 输出提取带时间戳的段落；不支持则返回 None。"""
    segs = None
    if isinstance(data, dict):
        if isinstance(data.get("segments"), list):
            segs = data["segments"]
        elif isinstance(data.get("text"), str) and not data.get("segments"):
            return None                      # 仅整段文本 → 走纯文本兜底
    elif isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
        segs = data
    if segs is None:
        return None
    items: list[TranscriptItem] = []
    for s in segs:
        if not isinstance(s, dict):
            continue
        text = str(s.get("text") or s.get("caption") or s.get("content") or "").strip()
        if not text:
            continue
        try:
            start = float(s.get("start", s.get("begin", s.get("from", 0))))
            end = float(s.get("end", s.get("stop", s.get("to", start))))
        except (TypeError, ValueError):
            continue
        items.append(TranscriptItem(start=start, end=max(end, start), text=text))
    return items or None


_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?；;\n])")
_FILELIKE_RE = re.compile(r"\.(wav|mp3|flac|m4a|png|jpg|jpeg|json|mp4)$", re.I)


def _split_sentences(text: str) -> list[str]:
    parts = _SENT_SPLIT_RE.split(text)
    out = [p.strip() for p in parts if p.strip()]
    return out or ([text.strip()] if text.strip() else [])


def _allocate_timings(sentences: list[str], duration: float) -> list[TranscriptItem]:
    """工作流无时间戳时：按字数比例把整轨时长分配给各句（字幕/对齐可用）。"""
    total = sum(len(s) for s in sentences) or 1
    items: list[TranscriptItem] = []
    t = 0.0
    for s in sentences:
        span = duration * len(s) / total
        items.append(TranscriptItem(start=round(t, 3), end=round(t + span, 3), text=s))
        t += span
    if items:
        items[-1].end = round(duration, 3)
    return items


def _try_json(raw: str) -> list[TranscriptItem] | None:
    """从可能带围栏/噪声的文本中抽取转录项；无有效结构返回 None。"""
    from taggycat.llm import LLMApiError, extract_json

    try:
        data = extract_json(raw)
    except LLMApiError:
        return None
    items = _items_from_json(data)
    return items or None


def _split_wav_for_upload(path: Path, max_bytes: int) -> tuple[list[Path], list[float]]:
    """按单次上传上限切分 16bit PCM wav，返回 (分段文件, 每段时长秒)。

    未超过上限时返回 ([原文件], [])，不生成任何分段文件；超限时在同目录
    生成 <name>.partN.wav，由调用方用完后删除。
    """
    if max_bytes <= 0 or not path.exists() or path.stat().st_size <= max_bytes:
        return [path], []
    import wave

    from taggycat.comfy import ComfyError

    margin = 8192  # RIFF 头 + 附属块余量
    outs: list[Path] = []
    durs: list[float] = []
    try:
        with wave.open(str(path), "rb") as src:
            ch = src.getnchannels()
            sw = src.getsampwidth()
            rate = src.getframerate() or 16000
            fsize = ch * sw
            frames_per = max(1, (max_bytes - margin) // fsize)
            idx = 0
            while True:
                data = src.readframes(frames_per)
                if not data:
                    break
                out = path.with_name(f"{path.stem}.part{idx}{path.suffix or '.wav'}")
                with wave.open(str(out), "wb") as dst:
                    dst.setnchannels(ch)
                    dst.setsampwidth(sw)
                    dst.setframerate(rate)
                    dst.writeframes(data)
                outs.append(out)
                durs.append(len(data) / fsize / rate)
                idx += 1
    except (wave.Error, EOFError, OSError) as e:
        for p in outs:
            p.unlink(missing_ok=True)
        raise ComfyError(f"分段前解析 WAV 失败 {path.name}: {e}") from e
    if len(outs) <= 1:
        for p in outs:
            p.unlink(missing_ok=True)
        return [path], []
    logger.info(
        "音频 {} 超过 {}MB 上传上限，切分为 {} 段（每段约 {}s）",
        path.name, round(max_bytes / 1048576, 1), len(outs), round(durs[0], 1),
    )
    return outs, durs


def _transcribe_chunk(
    wav: Path,
    cfg: ASRConfig,
    prompt: dict,
    *,
    offset: float,
    duration: float | None,
) -> tuple[list[TranscriptItem], list[str], bool]:
    """上传一段音频 → 提交工作流 → 解析台词（相对本段起点，未加 offset）。

    返回 (台词项, 收集到的文本列表, 是否走 JSON 时间戳路径)。
    duration=None 时纯文本兜底按 media_duration(本文件) 分配时长。
    """
    from taggycat.comfy import collect_strings, poll, submit, upload

    cc = cfg.comfyui
    name = upload(cc.base_url, wav)
    p = _patch_audio_prompt(
        copy.deepcopy(prompt), name, audio_key=(cc.audio_input or "").strip(),
    )
    pid = submit(cc.base_url, p)
    outputs = poll(cc.base_url, pid, timeout=cc.timeout, interval=cc.poll_interval)

    strs: list[str] = []
    collect_strings(outputs, strs)

    # 1) 优先带时间戳的 JSON 输出
    for cand in sorted(strs, key=len, reverse=True):
        items = _try_json(cand)
        if items:
            for it in items:
                it.start += offset
                it.end += offset
            return items, strs, True

    # 2) 纯文本输出 → 按字数比例分配本段时长
    plain = " ".join(
        s.strip() for s in strs
        if len(s.strip()) >= 4 and not _FILELIKE_RE.search(s.strip())
    ).strip()
    if not plain:
        return [], strs, False
    if duration is None:
        from taggycat.media.ffmpeg import media_duration

        duration = media_duration(wav)
    items = _allocate_timings(_split_sentences(plain), duration)
    for it in items:
        it.start += offset
        it.end += offset
    logger.warning(
        "ComfyUI ASR 工作流未返回时间戳，已按字数比例分配 {}s 时长（{} 段）",
        round(duration, 1), len(items),
    )
    return items, strs, False


def transcribe_comfyui(
    audio_path: str | Path,
    cfg: ASRConfig,
    *,
    root_dir: str | Path | None = None,
) -> Transcript:
    """wav → 提交 ComfyUI 转录工作流 → Transcript。

    音频超过 asr.comfyui.max_upload_mb（ComfyUI 默认 100MB 请求体上限）时
    自动切分为多段依次转录，再按时间偏移合并 —— 视频/音轨过长也能跑。
    """
    from taggycat.comfy import ComfyError, load_workflow

    cc = cfg.comfyui
    if not cc.workflow:
        raise ComfyError(
            "asr.engine=comfyui 但未配置 asr.comfyui.workflow"
            "（在设置界面「台词转录 ASR」选择工作流 JSON）"
        )
    wf_path = resolve_path(cc.workflow, root_dir)

    # 先加载校验（UI 格式/不存在时先于上传失败）
    prompt = load_workflow(wf_path)

    wav = Path(audio_path)
    max_bytes = int((cc.max_upload_mb or 90.0) * 1024 * 1024)
    chunks, durs = _split_wav_for_upload(wav, max_bytes)

    items: list[TranscriptItem] = []
    outputs: list[str] = []
    offset = 0.0
    try:
        for i, ch in enumerate(chunks):
            if len(chunks) > 1:
                logger.info("ComfyUI ASR 分段 {}/{}: {}", i + 1, len(chunks), ch.name)
            got, strs_out, via_json = _transcribe_chunk(
                ch, cfg, prompt,
                offset=offset,
                duration=durs[i] if durs else None,
            )
            if strs_out:
                outputs = strs_out
            if got:
                items.extend(got)
                if len(chunks) > 1:
                    logger.info(
                        "ComfyUI ASR 分段 {}/{} 完成: {} 段台词",
                        i + 1, len(chunks), len(got),
                    )
                elif via_json:
                    logger.info(
                        "ComfyUI ASR 完成: {} 段台词（JSON 时间戳）", len(got)
                    )
            elif len(chunks) > 1:
                logger.warning(
                    "ComfyUI ASR 分段 {}/{} 无台词输出，跳过", i + 1, len(chunks)
                )
            if durs:
                offset += durs[i]
    finally:
        if len(chunks) > 1:
            for p in chunks:
                p.unlink(missing_ok=True)

    if not items:
        raise ComfyError(f"工作流输出中无文本: {outputs}")
    items.sort(key=lambda it: it.start)
    if len(chunks) > 1:
        logger.info(
            "ComfyUI ASR 完成: {} 段台词（{} 分段合并，共 {:.0f}s）",
            len(items), len(chunks), offset,
        )
    return Transcript(language=cfg.language, items=items)


def transcribe_video(
    video_path: str | Path,
    cfg: ASRConfig,
    *,
    work_dir: str | Path,
    on_progress=None,
    root_dir: str | Path | None = None,
    media_info=None,
) -> Transcript:
    """视频 → 提取音轨 → 转录（按 engine 选本地或 ComfyUI 工作流）。

    media_info 可外部传入以避免 extract_audio 重复 ffprobe。
    """
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    wav = work_dir / "audio_16k.wav"
    if not wav.exists():
        extract_audio(video_path, wav, sample_rate=16000, mono=True,
                      on_progress=on_progress, info=media_info)
    if (cfg.engine or "").strip().lower() == "comfyui":
        return transcribe_comfyui(wav, cfg, root_dir=root_dir)
    return transcribe_audio(wav, cfg)
