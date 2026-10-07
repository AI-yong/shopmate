"""사용자 이미지를 안전하게 임시 저장하고 SigLIP 상품 검색으로 연결한다."""

from __future__ import annotations

import hashlib
import io
import uuid
from functools import lru_cache
from typing import Any

from minio import Minio
from PIL import Image, ImageOps, UnidentifiedImageError

from shopmate import config
from shopmate.store import session_state
from shopmate.search.siglip import DEFAULT_MODEL, DEFAULT_REVISION, get_query_encoder, search

ALLOWED_TYPES = {
    "image/jpeg": {"JPEG"},
    "image/png": {"PNG"},
    "image/webp": {"WEBP"},
}
OUTPUT_TYPE = "image/jpeg"
MAX_SIDE = 2048


class ImageQueryError(ValueError):
    pass


@lru_cache(maxsize=1)
def minio_client() -> Minio:
    return Minio(config.MINIO_ENDPOINT,
                 access_key=config.MINIO_ACCESS_KEY,
                 secret_key=config.MINIO_SECRET_KEY,
                 secure=config.MINIO_SECURE)


def sanitize_image(raw: bytes, content_type: str) -> tuple[bytes, int, int, str]:
    content_type = (content_type or "").split(";", 1)[0].strip().lower()
    if content_type not in ALLOWED_TYPES:
        raise ImageQueryError("JPEG, PNG, WebP 이미지만 업로드할 수 있습니다.")
    if not raw:
        raise ImageQueryError("이미지 파일이 비어 있습니다.")
    if len(raw) > config.IMAGE_QUERY_MAX_BYTES:
        raise ImageQueryError(
            f"이미지는 {config.IMAGE_QUERY_MAX_BYTES // (1024 * 1024)}MB 이하여야 합니다.")
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            if opened.format not in ALLOWED_TYPES[content_type]:
                raise ImageQueryError("Content-Type과 실제 이미지 형식이 다릅니다.")
            width, height = opened.size
            if width <= 0 or height <= 0 or width * height > config.IMAGE_QUERY_MAX_PIXELS:
                raise ImageQueryError("이미지 해상도가 허용 범위를 벗어났습니다.")
            opened.load()
            image = ImageOps.exif_transpose(opened)
            # 투명 영역은 검정으로 변환하지 않고 쇼핑몰 카드와 같은 흰 배경에 합성한다.
            if image.mode in ("RGBA", "LA") or "transparency" in image.info:
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
            image.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            # EXIF/ICC/XMP를 전달하지 않아 위치·기기 정보와 지시성 메타데이터를 제거한다.
            image.save(output, format="JPEG", quality=90, optimize=True)
            normalized = output.getvalue()
            return normalized, image.width, image.height, hashlib.sha256(normalized).hexdigest()
    except ImageQueryError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise ImageQueryError("손상됐거나 지원하지 않는 이미지입니다.") from error


def _ensure_bucket(client: Minio, bucket: str) -> None:
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)


def store_query(user_id: str, raw: bytes, content_type: str) -> dict[str, Any]:
    normalized, width, height, digest = sanitize_image(raw, content_type)
    query_id = str(uuid.uuid4())
    owner_key = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:24]
    key = f"query-images/{owner_key}/{query_id}.jpg"
    client = minio_client()
    _ensure_bucket(client, config.MINIO_BUCKET)
    client.put_object(config.MINIO_BUCKET, key, io.BytesIO(normalized), len(normalized),
                      content_type=OUTPUT_TYPE)
    try:
        with session_state.connect() as connection:
            session_state.save_image_query(
                connection, query_id, user_id, config.MINIO_BUCKET, key, digest,
                width, height, config.IMAGE_QUERY_TTL_SECONDS)
    except Exception:
        client.remove_object(config.MINIO_BUCKET, key)
        raise
    return {"query_image_id": query_id, "width": width, "height": height,
            "content_type": OUTPUT_TYPE, "expires_in": config.IMAGE_QUERY_TTL_SECONDS}


def store_pil_query(user_id: str, image: Image.Image) -> dict[str, Any]:
    """VLM이 고른 아이템 크롭을 원본 업로드와 같은 보안·TTL 계약으로 저장한다."""
    output = io.BytesIO()
    image.convert("RGB").save(output, format="JPEG", quality=90)
    return store_query(user_id, output.getvalue(), OUTPUT_TYPE)


def load_query_image(user_id: str, query_id: str) -> Image.Image:
    record = get_query_record(user_id, query_id)
    response = minio_client().get_object(record["object_bucket"], record["object_key"])
    try:
        raw = response.read(config.IMAGE_QUERY_MAX_BYTES + 1)
    finally:
        response.close()
        response.release_conn()
    if len(raw) > config.IMAGE_QUERY_MAX_BYTES:
        raise ImageQueryError("저장된 이미지가 허용 크기를 초과합니다.")
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            opened.load()
            return opened.convert("RGB")
    except (UnidentifiedImageError, OSError) as error:
        raise ImageQueryError("저장된 이미지를 읽을 수 없습니다.") from error


def get_query_record(user_id: str, query_id: str) -> dict[str, Any]:
    """이미지를 다운로드하지 않고 형식·소유권·만료만 확인한다."""
    try:
        parsed = str(uuid.UUID(query_id))
    except (ValueError, TypeError) as error:
        raise ImageQueryError("올바른 이미지 ID가 아닙니다.") from error
    with session_state.connect() as connection:
        record = session_state.load_image_query(connection, parsed, user_id)
    if record is None:
        raise ImageQueryError("이미지가 없거나 만료됐습니다. 다시 업로드해 주세요.")
    return record


def find_similar(user_id: str, query_id: str, limit: int = 10,
                 group: str | None = None, category: str | None = None,
                 **filters) -> list[dict[str, Any]]:
    image = load_query_image(user_id, query_id)
    encoder = get_query_encoder(DEFAULT_MODEL, DEFAULT_REVISION, "auto")
    vector = encoder.encode_image(image)
    return search(config.APP_SHOP_DSN, vector, limit, group, category, **filters)


def _remove_objects(rows) -> int:
    removed = 0
    client = minio_client()
    for bucket, key in rows:
        try:
            client.remove_object(bucket, key)
            removed += 1
        except Exception as error:
            # DB 행은 이미 만료/삭제됐다. 객체 삭제 실패는 서비스 기동을 막지 않고
            # 운영 로그로 남겨 별도 청소가 가능하게 한다.
            print(f"[이미지 질의] MinIO 삭제 실패 {bucket}/{key}: {error}")
    return removed


def purge_expired() -> int:
    with session_state.connect() as connection:
        rows = session_state.delete_expired_image_queries(connection)
    return _remove_objects(rows)


def delete_user_queries(user_id: str) -> int:
    with session_state.connect() as connection:
        rows = session_state.delete_user_image_queries(connection, user_id)
    return _remove_objects(rows)
