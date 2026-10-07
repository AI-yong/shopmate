"""Qwen3-VL-Embedding-2B adapter for fashion retrieval."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import numpy as np
import torch
from PIL import Image


MODEL_NAME = "Qwen/Qwen3-VL-Embedding-2B"
MODEL_REVISION = "9f2f7e710d6d81056aa5c0a4f04764fec6bb7bda"
EMBEDDING_DIM = 1536
# 질의 쪽에만 붙는 검색 지시문. 문서 벡터에는 붙지 않으므로 바꿔도 재임베딩이 필요 없다.
QUERY_INSTRUCTION = "Retrieve fashion products relevant to the user's query."


def choose_device(requested: str = "auto") -> str:
    if requested and requested != "auto":
        if requested == "mps" and not torch.backends.mps.is_available():
            raise RuntimeError("MPS를 사용할 수 없습니다.")
        if requested == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA를 사용할 수 없습니다.")
        return requested
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def truncate_and_normalize(matrix: Any, dimension: int = EMBEDDING_DIM) -> np.ndarray:
    if isinstance(matrix, torch.Tensor):
        matrix = matrix.detach().float().cpu().numpy()
    matrix = np.asarray(matrix, dtype=np.float32)
    if matrix.ndim != 2:
        raise ValueError(f"2차원 임베딩 행렬이 필요합니다: {matrix.shape}")
    if not 1 <= dimension <= matrix.shape[1]:
        raise ValueError(f"출력 차원은 1~{matrix.shape[1]}이어야 합니다: {dimension}")
    matrix = matrix[:, :dimension]
    if not np.isfinite(matrix).all():
        raise ValueError("Qwen3-VL 임베딩에 NaN 또는 무한대가 포함됐습니다.")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return (matrix / np.clip(norms, 1e-12, None)).astype(np.float32)


class Qwen3VLEncoder:
    """Official Qwen wrapper with explicit MPS selection and MRL truncation."""

    def __init__(
        self,
        model_name: str = MODEL_NAME,
        revision: str = MODEL_REVISION,
        device: str = "auto",
        dtype: str = "auto",
        dimension: int = EMBEDDING_DIM,
        max_pixels: int = 512 * 512,
    ):
        from huggingface_hub import snapshot_download

        self.model_name = model_name
        self.revision = revision
        self.device = choose_device(device)
        self.dimension = dimension
        if dtype == "auto":
            dtype = "float32" if self.device == "cpu" else "float16"
        if dtype not in ("float32", "float16", "bfloat16"):
            raise ValueError(f"지원하지 않는 dtype입니다: {dtype}")
        if self.device == "mps" and dtype == "bfloat16":
            raise ValueError("MPS에서는 float16 또는 float32를 사용하세요.")
        self.dtype = dtype

        snapshot = Path(snapshot_download(repo_id=model_name, revision=revision))
        wrapper_path = snapshot / "scripts" / "qwen3_vl_embedding.py"
        if not wrapper_path.exists():
            raise RuntimeError(f"Qwen 공식 embedding wrapper가 없습니다: {wrapper_path}")
        module_name = "shopmate_qwen3_vl_embedding_official"
        spec = importlib.util.spec_from_file_location(module_name, wrapper_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Qwen wrapper를 불러올 수 없습니다: {wrapper_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        self.embedder = module.Qwen3VLEmbedder(
            model_name_or_path=str(snapshot),
            dtype=getattr(torch, dtype),
            max_length=1024,
            max_pixels=max_pixels,
            attn_implementation="eager",
        )
        # The upstream wrapper selects only CUDA or CPU. Move the model
        # explicitly so Apple Silicon can use Metal.
        self.embedder.model.to(self.device)

    def _encode(self, item: dict[str, Any]) -> np.ndarray:
        """질의 하나를 임베딩해 MRL 차원으로 자르고 정규화한 1차원 벡터를 돌려준다."""
        output = self.embedder.process([item], normalize=False)
        return truncate_and_normalize(output, self.dimension)[0]

    def encode_image(self, image: Image.Image) -> np.ndarray:
        """Embed an image-only retrieval query in the shared Qwen space."""
        return self._encode({"image": image, "instruction": QUERY_INSTRUCTION})

    def encode_fused(self, image: Image.Image, text: str) -> np.ndarray:
        text = " ".join(str(text or "").split())
        return self._encode({"image": image, "text": text, "instruction": QUERY_INSTRUCTION})
