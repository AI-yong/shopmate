"""Client for the warm local Qwen3-VL embedding service."""

from __future__ import annotations

import base64
import json
import os
from io import BytesIO
from urllib import error, request

import numpy as np

import config
import image_query_service
from qwen3_vl_embedding import EMBEDDING_DIM
from search_qwen3_vl import search

SERVICE_URL = os.environ.get("QWEN3_VL_SERVICE_URL", "http://127.0.0.1:8092").rstrip("/")
SERVICE_TIMEOUT_SECONDS = float(os.environ.get("QWEN3_VL_SERVICE_TIMEOUT_SECONDS", "30"))


def _call(path: str, payload: dict) -> np.ndarray:
    body = json.dumps(payload).encode("utf-8")
    call = request.Request(
        f"{SERVICE_URL}{path}", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with request.urlopen(call, timeout=SERVICE_TIMEOUT_SECONDS) as response:
            result = json.load(response)
    except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Qwen3-VL 임베딩 서비스 호출 실패({SERVICE_URL}{path}): {exc}. "
            "`.venv-qwen/bin/python qwen3_vl_embedding_server.py --device auto`로 "
            "상주 서비스를 먼저 실행하세요.") from exc
    if result.get("error"):
        raise RuntimeError(f"Qwen3-VL 임베딩 서비스 오류: {result['error']}")
    vector = np.asarray(result.get("embedding"), dtype=np.float32)
    if vector.shape != (EMBEDDING_DIM,) or not np.isfinite(vector).all():
        raise RuntimeError("Qwen3-VL 임베딩 서비스가 잘못된 벡터를 반환했습니다.")
    return vector


def _image_payload(image) -> str:
    buffer = BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=95)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def image_vector(image) -> np.ndarray:
    return _call("/embed/image", {"image_base64": _image_payload(image)})


def fused_vector(image, text: str) -> np.ndarray:
    return _call("/embed/fused", {
        "image_base64": _image_payload(image),
        "text": " ".join(str(text or "").split()),
    })


def find_similar(user_id: str, query_image_id: str, text: str | None = None,
                 limit: int = 10, **filters):
    image = image_query_service.load_query_image(user_id, query_image_id)
    normalized = " ".join(str(text or "").split())
    vector = fused_vector(image, normalized) if normalized else image_vector(image)
    return search(config.APP_SHOP_DSN, vector, limit, **filters)
