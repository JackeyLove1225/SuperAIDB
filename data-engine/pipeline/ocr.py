"""PaddleOCR 云服务适配层（tier-1 文字产出，与 PDF/Excel 解析平级）

职责边界：只把图片/扫描页变成文字（markdown），上层（统一提取/映射）零感知。
实现：提交 OCR job → 轮询至完成 → 拉取 JSONL 结果 → 拼接每页 markdown 文本。
配置：config/.env 的 OCR_API_URL/OCR_API_TOKEN/OCR_MODEL（无 token 时降级不可用）。
"""
import json
from core.logger import get_logger
import time
from pathlib import Path

import requests

from config.settings import settings

logger = get_logger(__name__)

_JOB_URL_DEFAULT = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
_MODEL_DEFAULT = "PaddleOCR-VL-1.6"


def _cfg():
    url = getattr(settings, "OCR_API_URL", "") or _JOB_URL_DEFAULT
    token = getattr(settings, "OCR_API_TOKEN", "") or ""
    model = getattr(settings, "OCR_MODEL", "") or _MODEL_DEFAULT
    timeout = int(getattr(settings, "OCR_TIMEOUT", "300") or 300)
    return url, token, model, timeout


def is_available() -> bool:
    """OCR 是否可用（token 已配置）——不可用时上层如实降级，不静默"""
    return bool(_cfg()[1])


def ocr_image_to_markdown(image_path: str) -> str:
    """单个图片文件 → OCR 文字（markdown 拼接）

    Raises: RuntimeError（未配置 token / 提交失败 / 任务失败 / 超时）
    """
    url, token, model, timeout = _cfg()
    if not token:
        raise RuntimeError("OCR 未配置（config/.env 缺 OCR_API_TOKEN），无法识别图片/扫描件")
    path = Path(image_path)
    if not path.exists():
        raise RuntimeError(f"图片文件不存在: {image_path}")

    headers = {"Authorization": f"bearer {token}"}
    data = {"model": model,
            "optionalPayload": json.dumps({
                "useDocOrientationClassify": False,
                "useDocUnwarping": False,
                "useChartRecognition": False,
            })}
    with open(path, "rb") as f:
        resp = requests.post(url, headers=headers, data=data,
                             files={"file": f}, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"OCR 提交失败（HTTP {resp.status_code}）: {resp.text[:150]}")
    job_id = resp.json()["data"]["jobId"]
    logger.info("OCR job 提交成功: %s → %s", path.name, job_id)

    deadline = time.time() + timeout
    jsonl_url = ""
    while time.time() < deadline:
        r = requests.get(f"{url}/{job_id}", headers=headers, timeout=30)
        r.raise_for_status()
        state = r.json()["data"]["state"]
        if state == "done":
            jsonl_url = r.json()["data"]["resultUrl"]["jsonUrl"]
            break
        if state == "failed":
            raise RuntimeError(f"OCR 任务失败: {r.json()['data'].get('errorMsg', '')[:150]}")
        time.sleep(3)
    if not jsonl_url:
        raise RuntimeError(f"OCR 任务超时（{timeout}s 未完成）: {path.name}")

    jl = requests.get(jsonl_url, timeout=60)
    jl.raise_for_status()
    texts = []
    for line in jl.text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            result = json.loads(line)["result"]
            for res in result.get("layoutParsingResults", []):
                t = (res.get("markdown", {}) or {}).get("text", "")
                if t.strip():
                    texts.append(t.strip())
        except Exception:
            continue
    return "\n\n".join(texts)
