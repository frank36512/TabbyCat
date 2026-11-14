"""双语字幕翻译：LLM 批量翻译（缓存于 translations.json，失败降级单语）。"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from loguru import logger

from taggycat.pipeline.orchestrator import StageContext

_LANG_NAMES = {"en": "英语", "ja": "日语", "ko": "韩语", "fr": "法语",
               "de": "德语", "es": "西班牙语", "ru": "俄语", "zh": "中文"}
_TRANSLATE_BATCH = 30
TRANS_CACHE_FILE = "translations.json"


def lang_name(lang: str) -> str:
    return _LANG_NAMES.get(lang.lower(), lang)


def trans_key(text: str, lang: str) -> str:
    return hashlib.sha1(f"{lang}|{text}".encode()).hexdigest()


def load_trans_cache(out_dir: Path) -> dict[str, str]:
    f = out_dir / TRANS_CACHE_FILE
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except (OSError, ValueError) as e:
            logger.warning("翻译缓存损坏，将重新翻译 {}: {}", f, e)
    return {}


def translate_cues(
    ctx: StageContext, texts: list[str], lang2: str, cache: dict[str, str]
) -> tuple[list[str], list[str]]:
    """按批翻译字幕文本，返回 (与 texts 等长的译文列表, 错误列表)。

    缓存命中不调 LLM。每批最多 3 轮只重发仍缺的条目（应对 LLM 返回缺项/瞬断），
    之后对仍缺的条目逐条单发兜底，仍失败才记错误，不抛异常。
    """
    from taggycat.llm import get_llm

    results = [""] * len(texts)
    errors: list[str] = []
    pending: list[tuple[int, str]] = []
    for i, t in enumerate(texts):
        hit = cache.get(trans_key(t, lang2), "")
        if hit:
            results[i] = hit
        elif t:
            pending.append((i, t))
    if not pending:
        return results, errors

    llm = get_llm(ctx.config)
    lang = lang_name(lang2)
    _T_KEYS = ("t", "text", "translation", "translated", "en", "target", "译文")

    def _extract_items(resp):
        """从各种可能的返回形态中取出条目列表/扁平映射。"""
        if isinstance(resp, list):
            return resp, None
        if isinstance(resp, dict):
            for k in ("items", "translations", "results", "data"):
                v = resp.get(k)
                if isinstance(v, list):
                    return v, None
                if isinstance(v, dict):
                    return None, v
            # 顶层扁平映射 {"0": "译文"} / {"s001": "译文"}
            return None, resp
        return None, None

    def _ask(missing: dict[int, str]) -> str:
        """请求翻译 missing 中的条目，回填 results/cache 并从 missing 移除。"""
        items = [{"i": i, "t": t} for i, t in missing.items()]
        resp = llm.chat_json([
            {"role": "system", "content":
             f"你是专业影视字幕译者。把中文解说字幕翻译成{lang}，"
             "口语自然、简洁紧凑，保留画面感与情绪，不加解释。只输出 JSON。"},
            {"role": "user", "content": json.dumps(
                {"items": items}, ensure_ascii=False)},
        ])
        lst, flat = _extract_items(resp)
        n = 0

        def _take(idx, val) -> None:
            nonlocal n
            t = str(val or "").strip()
            if t and idx in missing:
                results[idx] = t
                cache[trans_key(texts[idx], lang2)] = t
                missing.pop(idx, None)
                n += 1

        if isinstance(lst, list):
            for it in lst:
                if isinstance(it, dict):
                    try:
                        idx = int(str(it.get("i")).strip())
                    except (TypeError, ValueError):
                        continue
                    for k in _T_KEYS:
                        if it.get(k):
                            _take(idx, it[k])
                            break
                elif isinstance(it, str) and len(missing) == 1:
                    _take(next(iter(missing)), it)
        if isinstance(flat, dict):
            for k, v in flat.items():
                try:
                    idx = int(str(k).strip().lstrip("sS"))
                except ValueError:
                    continue
                _take(idx, v)
        if n < len(items):
            # 留下原始响应线索，便于诊断模型为何缺项
            logger.debug("翻译返回缺项: {}",
                         json.dumps(resp, ensure_ascii=False, default=str)[:300])
        return f"{n}/{len(items)}"

    for b in range(0, len(pending), _TRANSLATE_BATCH):
        batch = pending[b:b + _TRANSLATE_BATCH]
        batch_no = b // _TRANSLATE_BATCH + 1
        missing = dict(batch)
        last_err = ""
        for attempt in range(3):
            try:
                got = _ask(missing)
                logger.info("字幕翻译第 {} 批: {} 条 → {}", batch_no, got, lang)
            except Exception as e:  # noqa: BLE001
                last_err = str(e)[:200]
                if attempt < 2:
                    logger.warning("字幕翻译失败，2s 后重发缺失条目: {}", last_err)
                    time.sleep(2)
            if not missing:
                break
        # 逐条单发兜底（批内个别条目始终缺失时；两轮）
        for idx in list(missing):
            for attempt in range(2):
                try:
                    _ask({idx: missing[idx]})
                except Exception as e:  # noqa: BLE001
                    last_err = str(e)[:200]
                    if attempt == 0:
                        time.sleep(1)
                if results[idx]:
                    break
            if results[idx]:
                missing.pop(idx, None)
        if missing:
            err = f"batch {batch_no}: {len(missing)} 条未翻译"
            if last_err:
                err += f"（{last_err}）"
            logger.warning("字幕翻译{}", err)
            errors.append(err)
    return results, errors


def ass_font_name(font: Path) -> str:
    """从字体文件路径推断 libass 可识别的 family 名（ASS Fontname 字段）。"""
    stem = font.stem
    low = stem.lower().replace("-", "").replace("_", "")
    if low.startswith("msyh"):
        return "Microsoft YaHei"
    if "notosanssc" in low:
        return "Noto Sans SC"
    for suf in ("Regular", "Bold", "Italic", "Medium", "Light", "Black", "Thin"):
        if stem.endswith(suf) and len(stem) > len(suf):
            return stem[: -len(suf)].rstrip("-_")
    return stem or "sans-serif"


# 旧名称兼容
_lang_name = lang_name
_trans_key = trans_key
_load_trans_cache = load_trans_cache
_translate_cues = translate_cues
_ass_font_name = ass_font_name
