# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "numpy>=1.26,<3", "opencv-python>=4.10,<5", "Pillow>=10.4,<13",
#   "pillow-heif>=0.18,<2", "reportlab>=4.2,<5", "Flask>=3.1,<4", "pymupdf>=1.24,<2",
# ]
# ///
"""Loopback-only HTTP UI. Images live in request memory, never in upload files."""

import argparse
import base64
import binascii
import secrets
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

import cv2
import id_card_pdf as core
import numpy as np
from flask import Flask, Response, jsonify, request
from PIL import Image
from werkzeug.exceptions import BadRequest, HTTPException

MAX_FILE_BYTES = 12 * 1024 * 1024
MAX_PIXELS = 25_000_000
STATIC_DIR = Path(__file__).with_name("static")


def decode_photo(value):
    if not isinstance(value, str) or not value:
        raise ValueError("请选择一张照片")
    if len(value) > 4 * ((MAX_FILE_BYTES + 2) // 3):
        raise ValueError("单张照片不能超过 12 MB")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("照片编码无效，请重新选择文件") from exc
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("单张照片不能超过 12 MB")
    return core.load_photo(BytesIO(raw), max_pixels=MAX_PIXELS)


def image_url(image, max_edge=1400):
    height, width = image.shape[:2]
    scale = min(1, max_edge / max(height, width))
    if scale < 1:
        image = cv2.resize(image, (round(width * scale), round(height * scale)),
                           interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        raise ValueError("无法生成预览")
    return "data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii")


def validated_corners(value, image):
    try:
        points = np.asarray(value, dtype=np.float64)
    except (ValueError, TypeError) as exc:
        raise ValueError("请选好四个卡片外角") from exc
    if points.shape != (4, 2) or not np.isfinite(points).all():
        raise ValueError("请选好四个卡片外角")
    if np.any(points < 0) or np.any(points > 1):
        raise ValueError("四角不能超出照片边界")
    height, width = image.shape[:2]
    return core.order_corners(points * [width - 1, height - 1])


def crop_card(data, dpi):
    if not isinstance(data, dict):
        raise BadRequest("卡面参数格式不正确")
    rotation = data.get("rotation", 0)
    if type(rotation) is not int or rotation not in (0, 180):
        raise ValueError("旋转角度只能是 0 或 180")
    image = decode_photo(data.get("image"))
    corners = validated_corners(data.get("corners"), image)
    card = core.rectify_card(image, corners, dpi, inset_px=data.get("inset_px", 0))
    if rotation:
        card = cv2.rotate(card, cv2.ROTATE_180)
    return card


def create_app():
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = 66 * 1024 * 1024
    app.config["LOCAL_TOKEN"] = secrets.token_urlsafe(32)

    @app.before_request
    def local_requests_only():
        hostname = urlsplit("http://" + request.host).hostname
        if hostname not in ("127.0.0.1", "localhost"):
            return jsonify(error="仅允许从本机地址访问"), 403
        origin = request.headers.get("Origin")
        if origin and origin != request.host_url.rstrip("/"):
            return jsonify(error="不允许跨来源请求"), 403
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            return jsonify(error="不允许跨网站请求"), 403
        if request.path.startswith("/api/"):
            token = request.headers.get("X-Local-Token", "")
            if not secrets.compare_digest(token, app.config["LOCAL_TOKEN"]):
                return jsonify(error="页面已过期，请刷新页面后重试"), 403
            if not request.is_json:
                return jsonify(error="接口只接受 JSON 请求"), 415
            if not isinstance(request.get_json(), dict):
                return jsonify(error="请求内容必须是 JSON 对象"), 400

    @app.after_request
    def private_response(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data: blob:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'"
        )
        return response

    @app.errorhandler(ValueError)
    @app.errorhandler(OSError)
    @app.errorhandler(cv2.error)
    @app.errorhandler(Image.DecompressionBombError)
    def invalid_image(error):
        message = str(error) if isinstance(error, ValueError) else "无法读取图片，请换用有效的 JPG、PNG 或 HEIC 照片"
        return jsonify(error=message), 400

    @app.errorhandler(HTTPException)
    def http_error(error):
        messages = {400: "请求格式不正确", 404: "页面不存在", 405: "请求方法不支持",
                    413: "上传内容太大，单张照片最多 12 MB", 415: "接口只接受 JSON 请求"}
        return jsonify(error=messages.get(error.code, "请求失败")), error.code

    @app.get("/")
    def index():
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return Response(html.replace("__LOCAL_TOKEN__", app.config["LOCAL_TOKEN"]), mimetype="text/html")

    @app.post("/api/analyze")
    def analyze():
        image = decode_photo(request.get_json().get("image"))
        height, width = image.shape[:2]
        try:
            corners = core.detect_card(image)
        except ValueError:
            corners = None
        return jsonify(
            width=width, height=height, preview=image_url(image),
            corners=(corners / [width - 1, height - 1]).tolist() if corners is not None else None,
            crop=image_url(core.rectify_card(image, corners, 150)) if corners is not None else None,
        )

    @app.post("/api/crop")
    def crop():
        return jsonify(crop=image_url(crop_card(request.get_json(), 150)))

    @app.post("/api/watermark")
    def watermark():
        settings = core.watermark_settings(request.get_json())
        return Response(core.watermark_preview(settings), mimetype="image/png")

    @app.post("/api/pdf")
    def pdf():
        data = request.get_json()
        cards = data.get("cards")
        if not isinstance(cards, list) or len(cards) != 4:
            raise ValueError("请上传并确认四个卡面")
        dpi = data.get("dpi", 300)
        if type(dpi) is not int or dpi not in (300, 600):
            raise ValueError("分辨率只能选 300 或 600 DPI")
        watermark = core.watermark_settings(data.get("watermark"))
        grayscale = data.get("grayscale", False)
        if type(grayscale) is not bool:
            raise ValueError("黑白输出开关必须是布尔值")
        jpeg = data.get("jpeg", True)
        if type(jpeg) is not bool:
            raise ValueError("JPEG 压缩开关必须是布尔值")
        # Decode one photo at a time; request bodies and results stay in RAM.
        cropped = [crop_card(card, dpi) for card in cards]
        filename = "id-cards-a4-grayscale.pdf" if grayscale else "id-cards-a4.pdf"
        return Response(core.render_pdf(cropped, watermark, grayscale=grayscale, jpeg=jpeg),
                        mimetype="application/pdf", headers={
                            "Content-Disposition": f'attachment; filename="{filename}"',
                        })

    return app


def main():
    parser = argparse.ArgumentParser(description="身份证 A4 排版：本地 HTTP 界面")
    parser.add_argument("--port", type=int, default=8765, help="本机端口，默认 8765")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须介于 1 和 65535 之间")
    print(f"打开 http://127.0.0.1:{args.port} · 仅本机访问 · Ctrl+C 停止", flush=True)
    create_app().run(host="127.0.0.1", port=args.port, debug=False, threaded=False, use_reloader=False)


if __name__ == "__main__":
    main()
