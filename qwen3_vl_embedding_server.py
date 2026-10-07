"""Keep Qwen3-VL loaded once and serve local retrieval embeddings over HTTP."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO

from PIL import Image

from qwen3_vl_embedding import EMBEDDING_DIM, Qwen3VLEncoder


class VectorCache:
    """Small process-local LRU for repeated interactive queries."""

    def __init__(self, capacity: int = 256):
        self.capacity = capacity
        self.values: OrderedDict[str, list[float]] = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key: str) -> list[float] | None:
        with self.lock:
            value = self.values.get(key)
            if value is not None:
                self.values.move_to_end(key)
            return value

    def put(self, key: str, value: list[float]) -> None:
        with self.lock:
            self.values[key] = value
            self.values.move_to_end(key)
            while len(self.values) > self.capacity:
                self.values.popitem(last=False)


class Handler(BaseHTTPRequestHandler):
    encoder: Qwen3VLEncoder
    inference_lock = threading.Lock()
    cache = VectorCache()

    def _json(self, status: int, value: dict) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != "/health":
            self._json(404, {"error": "not_found"})
            return
        self._json(200, {
            "status": "ok",
            "model_name": self.encoder.model_name,
            "model_revision": self.encoder.revision,
            "embedding_dim": EMBEDDING_DIM,
            "device": self.encoder.device,
            "cache_entries": len(self.cache.values),
        })

    @staticmethod
    def _decode_image(payload: dict) -> tuple[Image.Image, bytes]:
        raw = base64.b64decode(payload["image_base64"], validate=True)
        if not raw or len(raw) > 10 * 1024 * 1024:
            raise ValueError("이미지 크기가 허용 범위를 벗어났습니다.")
        with Image.open(BytesIO(raw)) as opened:
            image = opened.convert("RGB")
        return image, raw

    def do_POST(self) -> None:
        if self.path not in ("/embed/image", "/embed/fused"):
            self._json(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 12 * 1024 * 1024:
                raise ValueError("요청 크기가 허용 범위를 벗어났습니다.")
            raw_body = self.rfile.read(length)
            payload = json.loads(raw_body)
            text = " ".join(str(payload.get("text") or "").split())
            image, image_raw = self._decode_image(payload)
            key = hashlib.sha256(
                self.path.encode("utf-8") + b"\0" + text.encode("utf-8") + b"\0" + image_raw
            ).hexdigest()
            cached = self.cache.get(key)
            if cached is not None:
                self._json(200, {"embedding": cached, "cached": True})
                return
            with self.inference_lock:
                if self.path == "/embed/image":
                    vector = self.encoder.encode_image(image)
                else:
                    vector = self.encoder.encode_fused(image, text)
            output = vector.tolist()
            self.cache.put(key, output)
            self._json(200, {"embedding": output, "cached": False})
        except Exception as exc:  # noqa: BLE001 - local service returns a bounded error
            self._json(400, {"error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, format, *args):  # noqa: A002
        return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8092)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--dtype", default="auto")
    parser.add_argument("--cache-size", type=int, default=256)
    args = parser.parse_args()
    if args.cache_size < 0:
        parser.error("--cache-size는 0 이상이어야 합니다.")
    Handler.encoder = Qwen3VLEncoder(device=args.device, dtype=args.dtype)
    Handler.cache = VectorCache(args.cache_size)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(json.dumps({
        "status": "ready", "url": f"http://{args.host}:{args.port}",
        "device": Handler.encoder.device, "embedding_dim": EMBEDDING_DIM,
    }), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
