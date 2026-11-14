"""TTS 三引擎统一接口：edge 在线 / ComfyUI 工作流 / ComfyUI 语音克隆。

统一约定：`synthesize(text, out_base)` 由引擎自行决定扩展名，
返回最终落盘路径；调用方以 `ext` 属性预生成文件名。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import requests
from loguru import logger

from taggycat.comfy import ComfyError, get_session, poll, submit, upload
from taggycat.config import AppConfig, TTSConfig
from taggycat.paths import resolve_path


class TTSError(RuntimeError):
    """TTS 合成失败。"""


class BaseTTS:
    """引擎基类。"""

    name = "base"
    ext = ".wav"

    def __init__(self, cfg: TTSConfig, *, root_dir: Path | None = None):
        self.cfg = cfg
        self.root_dir = root_dir or Path.cwd()

    def synthesize(self, text: str, out_base: Path, voice: str | None = None) -> Path:
        """合成一段文本，返回落盘文件路径（out_base + ext）。

        voice: 音色覆盖（任务级选择），空则用引擎配置默认值。
        """
        raise NotImplementedError

    def _resolve(self, p: str | Path) -> Path:
        return resolve_path(p, self.root_dir)


# ---------------------------------------------------------------------
# 1) edge：在线免费（edge-tts）
# ---------------------------------------------------------------------
class EdgeTTS(BaseTTS):
    name = "edge"
    ext = ".mp3"

    def synthesize(self, text: str, out_base: Path, voice: str | None = None) -> Path:
        try:
            import edge_tts
        except ImportError as e:  # pragma: no cover
            raise TTSError("未安装 edge-tts，请执行: uv sync --all-extras") from e

        used_voice = (voice or "").strip() or self.cfg.edge.voice or self.cfg.default_voice
        out = out_base.with_suffix(self.ext)
        out.parent.mkdir(parents=True, exist_ok=True)

        async def _run() -> None:
            com = edge_tts.Communicate(
                text, used_voice, rate=self.cfg.edge.rate, volume=self.cfg.edge.volume
            )
            await com.save(str(out))

        # Qt/线程里可能已有事件循环；edge-tts 是纯 async 的，换线程跑
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            runner = asyncio.run
        else:  # pragma: no cover
            import concurrent.futures

            def runner(coro):  # type: ignore[no-untyped-def]
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    return ex.submit(asyncio.run, coro).result()

        try:
            runner(_run())
        except TTSError:
            raise
        except Exception as e:  # noqa: BLE001  网络/证书/配额等底层异常统一包装
            raise TTSError(f"edge-tts 合成失败（{used_voice}）: {e}") from e

        if not out.exists() or out.stat().st_size == 0:
            raise TTSError(f"edge-tts 未产出音频: {out}")
        logger.debug("edge-tts 合成: {} 字 -> {} ({})", len(text), out.name, used_voice)
        return out


# ---------------------------------------------------------------------
# 2/3) ComfyUI 工作流 TTS / 语音克隆（HTTP API）
# ---------------------------------------------------------------------
class ComfyUITTS(BaseTTS):
    """向本地 ComfyUI 提交工作流（API 格式 JSON），轮询取回音频。

    工作流文件需为 API 格式：ComfyUI 设置里开启 Dev mode 后
    「Save (API Format)」导出，或 GET /prompt 的返回体。
    """

    name = "comfyui"
    ext = ".wav"

    def __init__(self, cfg: TTSConfig, *, clone: bool = False, root_dir: Path | None = None):
        super().__init__(cfg, root_dir=root_dir)
        self.ccfg = cfg.comfyui_clone if clone else cfg.comfyui
        self.name = "comfyui_clone" if clone else "comfyui"
        self._ref_name: str | None = None      # 参考音频在 ComfyUI 端的文件名（上传后缓存）

    # ---- 工作流准备 ----
    def _load_workflow(self) -> dict:
        path = self._resolve(self.ccfg.workflow) if self.ccfg.workflow else Path("")
        if not path.exists():
            key = "comfyui_clone" if self.name == "comfyui_clone" else "comfyui"
            raise TTSError(f"工作流文件不存在: {path}（配置 tts.{key}.workflow）")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise TTSError(f"工作流 JSON 解析失败 {path.name}: {e}") from e
        if isinstance(data, dict) and "nodes" in data and "prompt" not in data:
            raise TTSError(
                f"工作流 {path.name} 是 UI 格式，需要 API 格式："
                "在 ComfyUI 设置开启 Dev mode，用 Save (API Format) 导出"
            )
        prompt = data.get("prompt") if isinstance(data, dict) and "prompt" in data else data
        if not isinstance(prompt, dict):
            raise TTSError(f"工作流格式无法识别: {path}")
        return prompt

    @staticmethod
    def _is_clone_ref_value(v: object) -> bool:
        return isinstance(v, str) and v != "" and not v.startswith("[")

    def _patch_prompt(self, prompt: dict, text: str, *, extra: dict | None = None) -> dict:
        """把文本（与参考音频/额外参数）写入工作流输入。"""
        text_key = (self.ccfg.text_input or "").strip()
        ref_key = (self.ccfg.ref_input or "").strip()
        text_hints = {"text", "text_g", "text_l", "prompt", "string", "content", "tts_text"}
        ref_hints = {"audio", "reference_audio", "ref_audio", "voice", "reference", "audio_path"}

        patched_text = False
        patched_ref = not extra  # 无额外要求时视为已满足

        for _nid, node in prompt.items():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            for key, val in inputs.items():
                lk = key.lower()
                # 文本
                if not patched_text:
                    if text_key:
                        if key == text_key:
                            inputs[key] = text
                            patched_text = True
                    elif lk in text_hints and isinstance(val, (str, type(None))):
                        inputs[key] = text
                        patched_text = True
                # 参考音频（克隆）
                if extra and not patched_ref:
                    if ref_key:
                        if key == ref_key:
                            inputs[key] = extra.get(key, val)
                            patched_ref = True
                    elif lk in ref_hints and self._is_clone_ref_value(val):
                        if key in extra:
                            inputs[key] = extra[key]
                        patched_ref = True

        if not patched_text:
            raise TTSError(
                f"工作流中未找到文本输入（text_input='{text_key or '自动'}'），"
                "请在配置 tts.*.text_input 指定输入名"
            )
        if extra and not patched_ref:
            raise TTSError(
                f"工作流中未找到参考音频输入（ref_input='{ref_key or '自动'}'），"
                "请在配置 tts.comfyui_clone.ref_input 指定输入名"
            )
        return prompt

    # ---- 提交与轮询（公共机制委托 comfy.py）----
    def _submit(self, prompt: dict) -> str:
        try:
            return submit(self.ccfg.base_url, prompt)
        except ComfyError as e:
            raise TTSError(str(e)) from e

    def _poll_outputs(self, pid: str) -> dict:
        try:
            return poll(
                self.ccfg.base_url, pid,
                timeout=self.ccfg.timeout, interval=self.ccfg.poll_interval,
            )
        except ComfyError as e:
            raise TTSError(str(e)) from e

    @staticmethod
    def _pick_audio(outputs: dict) -> tuple[str, str, str]:
        """从 outputs 找到第一个音频文件 (filename, subfolder, type)。"""
        for node_out in outputs.values():
            if not isinstance(node_out, dict):
                continue
            for key in ("audio", "audios", "files", "gifs"):
                items = node_out.get(key)
                if isinstance(items, list):
                    for it in items:
                        if isinstance(it, dict) and it.get("filename"):
                            return (
                                str(it["filename"]),
                                str(it.get("subfolder", "")),
                                str(it.get("type", "output")),
                            )
        raise TTSError(f"工作流输出中无音频文件: {list(outputs)}")

    def _download(self, filename: str, subfolder: str, ftype: str, out: Path) -> Path:
        base = self.ccfg.base_url.rstrip("/")
        try:
            r = get_session().get(
                f"{base}/view",
                params={"filename": filename, "subfolder": subfolder, "type": ftype},
                timeout=60,
            )
        except requests.RequestException as e:
            raise TTSError(f"音频下载失败: {e}") from e
        if r.status_code != 200 or not r.content:
            raise TTSError(f"音频下载失败 HTTP {r.status_code}: {filename}")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(r.content)
        return out

    def synthesize(self, text: str, out_base: Path, voice: str | None = None) -> Path:
        # ComfyUI 引擎的音色由工作流自身决定，voice 参数仅作缓存键参与方
        prompt = self._load_workflow()
        extra: dict = {}
        if self.name == "comfyui_clone":
            ref = (self.ccfg.reference_audio or "").strip()
            if ref:
                extra[self.ccfg.ref_input or "audio"] = self._ref_for_comfy(ref)
        prompt = self._patch_prompt(prompt, text, extra=extra or None)
        pid = self._submit(prompt)
        outputs = self._poll_outputs(pid)
        filename, subfolder, ftype = self._pick_audio(outputs)
        ext = Path(filename).suffix or self.ext
        out = out_base.with_suffix(ext)
        self._download(filename, subfolder, ftype, out)
        logger.debug("ComfyUI 合成: {} 字 -> {}", len(text), out.name)
        return out

    def _ref_for_comfy(self, ref: str) -> str:
        """参考音频取值：本地文件先上传到 ComfyUI input，远端工作流才读得到。"""
        p = self._resolve(ref)
        if not p.is_file():
            return ref                       # 已是 ComfyUI 端文件名，原样传入
        if self._ref_name is None:
            try:
                self._ref_name = upload(self.ccfg.base_url, p)
            except ComfyError as e:
                raise TTSError(f"参考音频上传失败 {p.name}: {e}") from e
        return self._ref_name


def get_tts(config: AppConfig) -> BaseTTS:
    """按配置选择 TTS 引擎。"""
    engine = (config.tts.engine or "edge").lower()
    root = config.root_dir
    if engine == "edge":
        return EdgeTTS(config.tts, root_dir=root)
    if engine == "comfyui":
        return ComfyUITTS(config.tts, clone=False, root_dir=root)
    if engine in ("comfyui_clone", "clone"):
        return ComfyUITTS(config.tts, clone=True, root_dir=root)
    raise TTSError(f"未知 TTS 引擎: {config.tts.engine}（edge | comfyui | comfyui_clone）")


# ---------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------
def cache_key(engine: str, voice: str, rate: str, text: str) -> str:
    raw = f"{engine}|{voice}|{rate}|{text}".encode()
    return hashlib.sha1(raw).hexdigest()[:20]


def cached_audio(cache_dir: Path, key: str, ext: str) -> Path | None:
    """命中缓存则返回路径（按引擎扩展名 O(1) 判断）。"""
    p = cache_dir / f"{key}{ext}"
    if p.is_file() and p.stat().st_size > 0:
        return p
    return None
