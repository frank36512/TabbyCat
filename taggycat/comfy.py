"""ComfyUI HTTP 公共客户端：工作流加载 / 文件上传 / 提交 / 轮询。

ASR 走工作流时复用这套机制；TTS/VLM 各自保留等价实现（已带引擎特有语义）。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import requests
from loguru import logger


class ComfyError(RuntimeError):
    """ComfyUI 交互失败。"""


_session: requests.Session | None = None


def get_session() -> requests.Session:
    """进程级复用 Session（连接池/keep-alive）。"""
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def load_workflow(path: Path) -> dict:
    """读取 API 格式工作流 JSON（拒绝 UI 格式）。"""
    if not path.exists():
        raise ComfyError(f"工作流文件不存在: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ComfyError(f"工作流 JSON 解析失败 {path.name}: {e}") from e
    if isinstance(data, dict) and "nodes" in data and "prompt" not in data:
        raise ComfyError(
            f"工作流 {path.name} 是 UI 格式，需要 API 格式："
            "ComfyUI 设置开启 Dev mode → Save (API Format)"
        )
    prompt = data.get("prompt") if isinstance(data, dict) and "prompt" in data else data
    if not isinstance(prompt, dict):
        raise ComfyError(f"工作流格式无法识别: {path}")
    return prompt


def upload(base_url: str, path: Path, *, timeout: int = 120) -> str:
    """上传文件到 ComfyUI input（/upload/image 同时接受音频），返回引用名。"""
    base = base_url.rstrip("/")
    if not path.exists():
        raise ComfyError(f"文件不存在: {path}")
    mime = {
        ".wav": "audio/wav", ".mp3": "audio/mpeg", ".flac": "audio/flac",
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    }.get(path.suffix.lower(), "application/octet-stream")
    try:
        with path.open("rb") as f:
            r = get_session().post(
                f"{base}/upload/image",
                files={"image": (path.name, f, mime)},
                data={"overwrite": "true", "type": "input"},
                timeout=timeout,
            )
    except requests.RequestException as e:
        raise ComfyError(f"ComfyUI 上传失败 {base}: {e}") from e
    if r.status_code != 200:
        hint = ""
        if r.status_code == 413:
            hint = (
                "（请求体超限：调小 asr.comfyui.max_upload_mb 可自动分段上传，"
                "或启动 ComfyUI 时加 --max-upload-size 调大上限）"
            )
        raise ComfyError(
            f"ComfyUI 上传失败 HTTP {r.status_code}: {r.text[:300]}{hint}"
        )
    try:
        data = r.json()
    except ValueError as e:
        raise ComfyError(f"ComfyUI 上传返回非 JSON: {r.text[:200]}") from e
    name = data.get("name") or path.name
    sub = data.get("subfolder") or ""
    return f"{sub}/{name}" if sub else str(name)


def submit(base_url: str, prompt: dict) -> str:
    base = base_url.rstrip("/")
    payload = {"prompt": prompt, "client_id": uuid.uuid4().hex}
    try:
        r = get_session().post(f"{base}/prompt", json=payload, timeout=30)
    except requests.RequestException as e:
        raise ComfyError(f"ComfyUI 连接失败 {base}: {e}") from e
    if r.status_code != 200:
        raise ComfyError(f"ComfyUI 提交失败 HTTP {r.status_code}: {r.text[:400]}")
    try:
        data = r.json()
    except ValueError as e:
        raise ComfyError(f"ComfyUI 返回非 JSON: {r.text[:200]}") from e
    if data.get("node_errors") or data.get("error"):
        raise ComfyError(
            f"ComfyUI 工作流错误: {json.dumps(data, ensure_ascii=False)[:500]}"
        )
    pid = data.get("prompt_id")
    if not pid:
        raise ComfyError(f"ComfyUI 未返回 prompt_id: {data}")
    return str(pid)


def poll(base_url: str, pid: str, *, timeout: int, interval: float = 1.0) -> dict:
    base = base_url.rstrip("/")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = get_session().get(f"{base}/history/{pid}", timeout=15)
        except requests.RequestException as e:
            raise ComfyError(f"ComfyUI 轮询失败: {e}") from e
        if r.status_code == 200:
            try:
                data = r.json()
            except ValueError:
                data = {}
            entry = data.get(pid)
            if entry:
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    raise ComfyError(
                        f"ComfyUI 执行出错: {json.dumps(status, ensure_ascii=False)[:500]}"
                    )
                outputs = entry.get("outputs") or {}
                if outputs:
                    return outputs
        time.sleep(interval)
    raise ComfyError(f"ComfyUI 超时（{timeout}s）未产出: {pid}")


def collect_strings(obj, out: list[str]) -> None:
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            collect_strings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            collect_strings(v, out)


def run_workflow(
    workflow_path: Path,
    base_url: str,
    patch,                            # callable(prompt) -> prompt
    *,
    timeout: int,
    interval: float = 1.0,
) -> dict:
    """加载 → patch → 提交 → 轮询，返回 outputs。"""
    prompt = load_workflow(workflow_path)
    prompt = patch(prompt)
    pid = submit(base_url, prompt)
    logger.debug("ComfyUI 已提交: {} ({})", workflow_path.name, pid)
    return poll(base_url, pid, timeout=timeout, interval=interval)
