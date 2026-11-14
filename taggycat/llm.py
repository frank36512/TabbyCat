"""LLM 客户端：云端 OpenAI 兼容为主，本地 Ollama 备选。"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from loguru import logger
from openai import (
    APIConnectionError,
    APITimeoutError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)
from pydantic import BaseModel

from taggycat.comfy import (
    ComfyError,
    collect_strings,
    poll,
    submit,
    upload,
)
from taggycat.config import AppConfig, LLMConfig, VLMConfig
from taggycat.paths import resolve_path

# 可重试：仅传输/限流/服务端瞬时异常（4xx 参数类错误不在此列）
_RETRYABLE_LLM_ERRORS = (
    APIConnectionError, APITimeoutError, RateLimitError, InternalServerError,
)


class LLMApiError(RuntimeError):
    pass


class ChatMessage(BaseModel):
    role: str
    content: Any


# ---------------------------------------------------------------------
# JSON 抽取（容忍 markdown 围栏/前后杂文）
# ---------------------------------------------------------------------
def extract_json(text: str) -> Any:
    """从 LLM 输出中稳健抽取 JSON。"""
    if not text or not text.strip():
        raise LLMApiError("LLM 返回为空")
    s = text.strip()
    # 去围栏
    for marker in ("```json", "```JSON", "```"):
        if s.startswith(marker):
            s = s[len(marker):]
        if s.endswith("```"):
            s = s[: -3]
    s = s.strip()
    # 直接解析
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # 括号匹配扫描
    dec = json.JSONDecoder()
    for i, ch in enumerate(s):
        if ch in "[{":
            try:
                obj, _ = dec.raw_decode(s[i:])
                return obj
            except json.JSONDecodeError:
                continue
    raise LLMApiError(f"无法从 LLM 输出解析 JSON: {text[:200]}")


def normalize_openai_base(base: str) -> str:
    """补全缺失的 /v1 路径。

    LM Studio/Ollama 等本地服务的 OpenAI 兼容端点在 `/v1/...`；
    填裸地址（如 http://192.168.2.2:1234）会打到错误路由，返回
    `choices=null` 之类的非标准响应。已带版本段的地址原样返回。
    """
    b = (base or "").strip().rstrip("/")
    if not b:
        return b
    try:
        path = urlparse(b).path.rstrip("/")
    except ValueError:
        return b
    if path.endswith("/v1") or "/v1/" in path or path.endswith("/chat/completions"):
        return b
    return b + "/v1"


def _make_client(cfg: LLMConfig):
    from openai import OpenAI

    base = normalize_openai_base(cfg.base_url)
    return OpenAI(
        base_url=base or None,
        api_key=cfg.api_key or "EMPTY",
        timeout=cfg.timeout,
        max_retries=0,           # 自行控制重试
    )


class LLMClient:
    """文本对话（剧情分析/文案生成/修复）。"""

    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self._client = _make_client(cfg)

    @property
    def model(self) -> str:
        return self.cfg.model

    def chat(
        self,
        messages: list[dict] | list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str:
        msgs = [m if isinstance(m, dict) else m.model_dump() for m in messages]
        kwargs: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": msgs,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
        }
        if json_mode:
            # 部分兼容服务不支持 response_format，失败则降级
            kwargs["response_format"] = {"type": "json_object"}
        effort = (self.cfg.reasoning_effort or "").strip()
        if effort:
            # 推理模型思考控制（none=关闭思维链，避免配额被推理吃掉）
            kwargs["reasoning_effort"] = effort

        _MAX_TOKENS_LIMIT = 32768
        adjusted: set[str] = set()      # 协议性调整各做一次，不计入重试次数
        attempts = 0
        last_err: Exception | None = None
        while attempts < max(1, self.cfg.max_retries):
            try:
                resp = self._client.chat.completions.create(**kwargs)
                choices = getattr(resp, "choices", None) or []
                content = self._extract_content(resp)
                if content:
                    return content
                finish = getattr(choices[0], "finish_reason", None) if choices else None
                if finish == "length" and "max_tokens" not in adjusted \
                        and kwargs.get("max_tokens", 0) < _MAX_TOKENS_LIMIT:
                    # 推理模型的思维链可能吃掉配额 → 扩容重试（不计次）
                    adjusted.add("max_tokens")
                    kwargs["max_tokens"] = min(
                        int(kwargs.get("max_tokens") or 4096) * 2, _MAX_TOKENS_LIMIT
                    )
                    logger.warning(
                        "max_tokens 被截断（finish_reason=length），扩大到 {} 重试",
                        kwargs["max_tokens"],
                    )
                    continue
                raise LLMApiError(
                    f"LLM 返回空内容（finish_reason={finish}）: {self._safe_dump(resp)}"
                )
            except BadRequestError as e:
                # 服务端不认的可选参数 → 去掉后重试（协议性调整，不计次）
                dropped = ""
                for key, hint in (("response_format", "response_format"),
                                  ("reasoning_effort", "effort")):
                    if key in kwargs and key not in adjusted \
                            and hint in str(e).lower():
                        adjusted.add(key)
                        kwargs.pop(key, None)
                        dropped = key
                        break
                if dropped:
                    logger.warning(
                        "服务端不支持 {}，去掉后重试: {}", dropped, str(e)[:150]
                    )
                    continue
                # 其余 4xx 是确定性错误，重试无意义
                raise LLMApiError(f"LLM 请求被拒绝: {str(e)[:300]}") from e
            except _RETRYABLE_LLM_ERRORS as e:
                last_err = e
                attempts += 1
                if attempts >= max(1, self.cfg.max_retries):
                    break
                wait = min(2**attempts, 10)
                logger.warning("LLM 调用失败(第{}次): {}；{}s 后重试", attempts, e, wait)
                time.sleep(wait)
        raise LLMApiError(f"LLM 调用重试 {self.cfg.max_retries} 次仍失败: {last_err}")

    @staticmethod
    def _extract_content(resp) -> str:
        """稳健取回 content（choices=null、分段 content 列表等异常形态）。"""
        choices = getattr(resp, "choices", None) or []
        if not choices:
            return ""
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)
        if isinstance(content, list):
            # 部分服务返回 [{"type":"text","text":...}, ...] 分段结构
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict):
                    parts.append(str(part.get("text") or ""))
                else:
                    parts.append(str(getattr(part, "text", "") or ""))
            content = "".join(parts)
        return content or ""

    @staticmethod
    def _safe_dump(resp) -> str:
        try:
            return json.dumps(resp.model_dump(), ensure_ascii=False)[:300]
        except Exception:  # noqa: BLE001
            return repr(resp)[:300]

    def chat_json(self, messages: list[dict], **kw) -> Any:
        raw = self.chat(messages, json_mode=True, **kw)
        try:
            return extract_json(raw)
        except LLMApiError:
            # json_mode 失败的兜底：再要一次纯 JSON
            fix_msg = "只输出上面内容对应的合法 JSON，不要任何其他文字。"
            raw = self.chat(
                messages + [{"role": "assistant", "content": raw},
                            {"role": "user", "content": fix_msg}],
                json_mode=False, **kw,
            )
            return extract_json(raw)


# ---------------------------------------------------------------------
# VLM：抽帧画面理解
# ---------------------------------------------------------------------
class VLMClient:
    """多模态视觉理解（OpenAI 兼容 image_url 消息）。"""

    def __init__(self, cfg: VLMConfig):
        self.cfg = cfg
        self._client = _make_client(LLMConfig(
            base_url=cfg.base_url, api_key=cfg.api_key, timeout=cfg.timeout,
            max_retries=cfg.max_retries, model=cfg.model,
        ))

    @staticmethod
    def _img_part(path: str | Path, max_side: int = 640) -> dict:
        """读取图片并（必要时）压缩为 base64 data URL。"""
        from io import BytesIO

        from PIL import Image

        p = Path(path)
        img = Image.open(p).convert("RGB")
        if max(img.size) > max_side:
            img.thumbnail((max_side, max_side))
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=82)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}

    def describe_batch(
        self,
        frames: list[tuple[float, str | Path]],
        *,
        batch_size: int = 6,
        style_hint: str = "",
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[dict]:
        """批量描述帧。返回 [{"t": 秒, "desc": str}, ...]（按输入顺序）。

        on_progress(已处理数, 总数) 每批完成后回调一次（供 UI 细分进度）。
        """
        results: list[dict] = []
        total = len(frames)
        for i in range(0, total, batch_size):
            batch = frames[i:i + batch_size]
            results.extend(self._describe_one_batch(batch, style_hint=style_hint))
            if on_progress:
                try:
                    on_progress(min(i + batch_size, total), total)
                except Exception:  # noqa: BLE001
                    logger.debug("VLM on_progress 回调异常", exc_info=True)
        return results

    def _describe_one_batch(
        self, batch: list[tuple[float, str | Path]], *, style_hint: str = ""
    ) -> list[dict]:
        content: list[dict] = [{"type": "text", "text": self._prompt(len(batch), style_hint)}]
        for t, p in batch:
            content.append({"type": "text", "text": f"[t={t:.2f}s]"})
            content.append(self._img_part(p))

        messages = [{"role": "user", "content": content}]
        last_err: Exception | None = None
        for attempt in range(max(1, self.cfg.max_retries)):
            try:
                resp = self._client.chat.completions.create(
                    model=self.cfg.model, messages=messages,
                    temperature=0.2, max_tokens=1024 * len(batch),
                )
                raw = resp.choices[0].message.content or ""
                arr = extract_json(raw)
                return self._align(arr, batch)
            except Exception as e:  # noqa: BLE001
                last_err = e
                wait = min(2**attempt, 10)
                logger.warning("VLM 调用失败(第{}次): {}；{}s 后重试", attempt + 1, e, wait)
                time.sleep(wait)
        logger.error("VLM 调用最终失败: {}", last_err)
        return []

    @staticmethod
    def _prompt(n: int, style_hint: str) -> str:
        hint = f"（该视频为{style_hint}）" if style_hint else ""
        return (
            f"以下是从一部影片中按时间顺序抽取的 {n} 张截图{hint}。"
            "请为每张图输出一句客观的中文画面描述，涵盖：场景地点、人物动作、情绪氛围、关键物件。"
            '只输出 JSON 数组，格式：[{"t": 时间秒数, "desc": "描述"}]，不要输出任何其他文字。'
        )

    @staticmethod
    def _align(arr: Any, batch: list[tuple[float, str | Path]]) -> list[dict]:
        """对齐返回结果与输入帧（容错数量不匹配）。"""
        if not isinstance(arr, list):
            return []
        out: list[dict] = []
        for idx, (t, _) in enumerate(batch):
            if idx < len(arr) and isinstance(arr[idx], dict) and arr[idx].get("desc"):
                out.append({"t": t, "desc": str(arr[idx]["desc"])[:300]})
        return out


# ---------------------------------------------------------------------
# VLM：抽帧画面理解（ComfyUI 工作流）
# ---------------------------------------------------------------------
class ComfyUIVLMClient:
    """向 ComfyUI 提交视觉理解工作流（API 格式），逐帧取回文本描述。

    与 ComfyUITTS 同一套 /upload/image → /prompt → /history 机制；
    `describe_batch` 契约与 VLMClient 相同（失败帧跳过，不抛异常）。
    """

    name = "comfyui"

    def __init__(self, cfg: VLMConfig, *, root_dir: Path | None = None):
        self.cfg = cfg
        self.ccfg = cfg.comfyui
        self.root_dir = root_dir or Path.cwd()

    def _resolve(self, p: str | Path) -> Path:
        return resolve_path(p, self.root_dir)

    # ---- 工作流准备 ----
    def _load_workflow(self) -> dict:
        path = self._resolve(self.ccfg.workflow) if self.ccfg.workflow else Path("")
        if not path.exists():
            raise LLMApiError(
                f"VLM 工作流文件不存在: {path}（配置 vlm.comfyui.workflow，"
                "或在设置界面选择）"
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise LLMApiError(f"VLM 工作流 JSON 解析失败 {path.name}: {e}") from e
        if isinstance(data, dict) and "nodes" in data and "prompt" not in data:
            raise LLMApiError(
                f"工作流 {path.name} 是 UI 格式，需要 API 格式："
                "ComfyUI 设置开启 Dev mode → Save (API Format)"
            )
        prompt = data.get("prompt") if isinstance(data, dict) and "prompt" in data else data
        if not isinstance(prompt, dict):
            raise LLMApiError(f"工作流格式无法识别: {path}")
        return prompt

    @staticmethod
    def _instruction(style_hint: str) -> str:
        hint = f"（该视频为{style_hint}）" if style_hint else ""
        return (
            f"{hint}请用一句客观的中文描述这张画面："
            "场景地点、人物动作、情绪氛围、关键物件。"
        )

    # 展示/预览节点的 text 是输出回显，写进去会污染取词
    _DISPLAY_HINTS = ("showanything", "showtext", "previewtext",
                      "previewany", "previewimage", "preview")
    # 提示词输入名优先级：模型节点的 custom_prompt 优先于通用 text
    _TEXT_KEY_PRIORITY = ("custom_prompt", "prompt", "question", "caption",
                          "content", "positive", "positive_prompt", "string", "text")

    @classmethod
    def _is_display_node(cls, node: dict) -> bool:
        cs = str(node.get("class_type") or "").lower()
        return any(h in cs for h in cls._DISPLAY_HINTS)

    def _patch_prompt(self, prompt: dict, instruction: str, image_name: str) -> dict:
        """写入图像（必须）与提示词（可选，找不到则用工作流内置提示词）。"""
        img_key = (self.ccfg.image_input or "").strip()
        text_key = (self.ccfg.text_input or "").strip()
        patched_img = False

        # 1) 图像输入（必须）
        for _nid, node in prompt.items():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            is_load_image = node.get("class_type") == "LoadImage"
            for key, val in inputs.items():
                if patched_img:
                    break
                lk = key.lower()
                if img_key:
                    if key == img_key:
                        inputs[key] = image_name
                        patched_img = True
                elif (
                    (is_load_image and lk in ("image", "file"))
                    or (lk in ("image_path", "image_file") and isinstance(val, str))
                ):
                    inputs[key] = image_name
                    patched_img = True

        # 2) 提示词输入（可选）
        patched_text = False
        if text_key:
            for _nid, node in prompt.items():
                inputs = node.get("inputs") if isinstance(node, dict) else None
                if isinstance(inputs, dict) and text_key in inputs:
                    inputs[text_key] = instruction
                    patched_text = True
                    break
        else:
            best: tuple[int, dict, str] | None = None   # (优先级, inputs, key)
            for _nid, node in prompt.items():
                if not isinstance(node, dict) or self._is_display_node(node):
                    continue
                inputs = node.get("inputs")
                if isinstance(inputs, dict):
                    for key, val in inputs.items():
                        lk = key.lower()
                        if lk not in self._TEXT_KEY_PRIORITY:
                            continue
                        if not isinstance(val, (str, type(None))):
                            continue
                        rank = self._TEXT_KEY_PRIORITY.index(lk)
                        if best is None or rank < best[0]:
                            best = (rank, inputs, key)
                    if best and best[0] == 0:
                        break
                if best is None and isinstance(node.get("text"), (str, type(None))) \
                        and "text" in node:
                    best = (len(self._TEXT_KEY_PRIORITY) - 1, node, "text")
            if best is not None:
                best[1][best[2]] = instruction
                patched_text = True

        if not patched_img:
            raise LLMApiError(
                f"工作流中未找到图像输入（image_input='{img_key or '自动'}'），"
                "请在配置 vlm.comfyui.image_input 指定输入名"
            )
        if not patched_text:
            logger.info("VLM 工作流无提示词输入，使用工作流内置提示词")
        return prompt

    # ---- 提交与轮询（公共机制委托 comfy.py）----
    def _upload(self, path: str | Path) -> str:
        """上传帧图片到 ComfyUI input，返回 LoadImage 用的文件名。"""
        try:
            return upload(self.ccfg.base_url, Path(path), timeout=60)
        except ComfyError as e:
            raise LLMApiError(str(e)) from e

    def _submit(self, prompt: dict) -> str:
        try:
            return submit(self.ccfg.base_url, prompt)
        except ComfyError as e:
            raise LLMApiError(str(e)) from e

    def _poll_outputs(self, pid: str) -> dict:
        try:
            return poll(
                self.ccfg.base_url, pid,
                timeout=self.ccfg.timeout, interval=self.ccfg.poll_interval,
            )
        except ComfyError as e:
            raise LLMApiError(str(e)) from e

    @classmethod
    def _pick_text(cls, outputs: dict, *, exclude: str = "") -> str:
        """从 outputs 递归收集文本，取最长的非平凡字符串作为画面描述。"""
        strs: list[str] = []
        collect_strings(outputs, strs)
        ex = (exclude or "").strip()
        cands = [s.strip() for s in strs
                 if len(s.strip()) >= 4 and not s.strip().startswith("{")
                 and s.strip() != ex]
        if not cands:
            raise LLMApiError(f"工作流输出中无文本描述: {list(outputs)}")
        return max(cands, key=len)

    # ---- 对外接口（与 VLMClient.describe_batch 同契约）----
    def describe_batch(
        self,
        frames: list[tuple[float, str | Path]],
        *,
        batch_size: int = 1,
        style_hint: str = "",
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[dict]:
        instruction = self._instruction(style_hint)
        template = self._load_workflow()
        results: list[dict] = []
        total = len(frames)
        for idx, (t, p) in enumerate(frames):
            try:
                name = self._upload(p)
                prompt = self._patch_prompt(
                    json.loads(json.dumps(template)), instruction, name
                )
                pid = self._submit(prompt)
                text = self._pick_text(self._poll_outputs(pid), exclude=instruction)
                results.append({"t": t, "desc": text[:300]})
            except LLMApiError as e:
                logger.warning("ComfyUI VLM 单帧失败 t={:.1f}: {}", t, e)
            except Exception as e:  # noqa: BLE001
                logger.warning("ComfyUI VLM 单帧异常 t={:.1f}: {}", t, e)
            if on_progress:
                try:
                    on_progress(idx + 1, total)
                except Exception:  # noqa: BLE001
                    logger.debug("VLM on_progress 回调异常", exc_info=True)
        return results


# ---------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------
def get_llm(config: AppConfig) -> LLMClient:
    return LLMClient(config.effective_llm)


def get_vlm(config: AppConfig) -> VLMClient | ComfyUIVLMClient | None:
    """VLM 未启用或未配置时返回 None（理解阶段降级为纯台词轨）。

    provider="comfyui" 走视觉理解工作流；配置缺失则直接报错（用户显式
    选择了该 provider，静默降级会浪费一次运行）。
    """
    v = config.vlm
    if not v.enabled:
        return None
    provider = (v.provider or "openai_compatible").strip().lower()
    if provider in ("comfyui", "comfyui_workflow"):
        if not v.comfyui.workflow:
            raise LLMApiError(
                "vlm.provider=comfyui 但未配置 vlm.comfyui.workflow"
                "（在设置界面「画面理解 VLM」选择工作流 JSON）"
            )
        return ComfyUIVLMClient(v, root_dir=config.root_dir)
    key = (v.api_key or "").strip()
    if not v.base_url or not v.model or not key or "REPLACE" in key:
        logger.warning("VLM 未配置（api_key/base_url），画面理解将降级为纯台词轨")
        return None
    return VLMClient(v)
