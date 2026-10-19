# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "numpy>=1.26,<3",
#   "opencv-python>=4.10,<5",
#   "Pillow>=10.4,<13",
#   "pillow-heif>=0.18,<2",
#   "reportlab>=4.2,<5",
#   "pymupdf>=1.24,<2",
# ]
# ///
"""Crop four ID-card photos locally and print at physical size on one A4 page."""

import argparse
import sys
from io import BytesIO
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

CARD_WIDTH_MM = 85.6
CARD_HEIGHT_MM = 54.0
CARD_CORNER_RADIUS_MM = 3.0
CARD_RATIO = CARD_WIDTH_MM / CARD_HEIGHT_MM


def load_photo(path, *, max_pixels=None):
    register_heif_opener()
    with Image.open(path) as source:
        if max_pixels is not None and source.width * source.height > max_pixels:
            raise ValueError(f"图片超过 {max_pixels // 1_000_000} MP，请先降低照片分辨率")
        # Apply phone orientation before any detection or manual coordinates.
        image = ImageOps.exif_transpose(source).convert("RGBA")
        background = Image.new("RGBA", image.size, "white")
        image = Image.alpha_composite(background, image).convert("RGB")
    if min(image.size) < 100:
        raise ValueError(f"图片太小，无法可靠裁切：{path}")
    return cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)


def order_corners(points):
    """Return a convex, clockwise quad, with its longer edge horizontal."""
    points = np.asarray(points, dtype=np.float32).reshape(4, 2)
    if not np.isfinite(points).all():
        raise ValueError("四角坐标必须是有限数值")
    hull = cv2.convexHull(points).reshape(-1, 2)
    if len(hull) != 4 or abs(cv2.contourArea(hull)) < 100:
        raise ValueError("请选择四个不同的卡片外角，不能共线或落在三角形内部")
    center = points.mean(axis=0)
    angles = np.arctan2(points[:, 1] - center[1], points[:, 0] - center[0])
    points = points[np.argsort(angles)]
    points = np.roll(points, -np.argmin(points.sum(axis=1)), axis=0)
    lengths = np.linalg.norm(np.roll(points, -1, axis=0) - points, axis=1)
    if lengths[0] + lengths[2] < lengths[1] + lengths[3]:
        points = np.roll(points, -1, axis=0)
    return points


def background_masks(lab):
    """Estimate a uniform backdrop from the perimeter, in color rather than gray."""
    border = np.concatenate((lab[0], lab[-1], lab[1:-1, 0], lab[1:-1, -1]))
    background = np.median(border, axis=0)
    distances = np.linalg.norm(border - background, axis=1)
    # A patterned backdrop cannot be represented by one color: use edges instead.
    if np.mean(distances < 24) < 0.65:
        return [], None
    noise = float(np.quantile(distances[distances < 24], 0.9))
    minimum = max(12.0, noise + 8)
    difference = np.linalg.norm(lab - background, axis=2)
    distance_image = np.clip(difference, 0, 255).astype(np.uint8)
    otsu, _ = cv2.threshold(distance_image, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    thresholds = {minimum, max(minimum, otsu), max(minimum, otsu * 0.75)}
    masks = [(difference > threshold).astype(np.uint8) * 255
             for threshold in sorted(thresholds)]
    return masks, (background, minimum)


def boundary_score(points, lab, edge_distance, background):
    """Score evidence on all four sides, not just a rectangle's area or shape."""
    height, width = lab.shape[:2]
    contrasts, supports, separations = [], [], []
    offset = max(3.0, min(height, width) * 0.006)
    for start, end in zip(points, np.roll(points, -1, axis=0)):
        direction = end - start
        length = np.linalg.norm(direction)
        # order_corners is clockwise in image coordinates; this points inward.
        inward = np.array([-direction[1], direction[0]]) / length
        positions = start + np.linspace(0.12, 0.88, 48)[:, None] * direction
        inside = positions + inward * offset
        outside = positions - inward * offset
        valid = ((outside[:, 0] >= 0) & (outside[:, 0] < width)
                 & (outside[:, 1] >= 0) & (outside[:, 1] < height)
                 & (inside[:, 0] >= 0) & (inside[:, 0] < width)
                 & (inside[:, 1] >= 0) & (inside[:, 1] < height))
        # Image-frame edges must not masquerade as the card's outline.
        if np.mean(valid) < 0.75:
            return None
        inside = np.rint(inside[valid]).astype(int)
        outside = np.rint(outside[valid]).astype(int)
        on_edge = np.rint(positions[valid]).astype(int)
        for samples in (inside, outside, on_edge):
            samples[:, 0] = np.clip(samples[:, 0], 0, width - 1)
            samples[:, 1] = np.clip(samples[:, 1], 0, height - 1)
        inner = lab[inside[:, 1], inside[:, 0]]
        outer = lab[outside[:, 1], outside[:, 0]]
        # Medians reduce the effect of lettering, portraits and isolated shadows.
        contrast = np.linalg.norm(np.median(inner, axis=0) - np.median(outer, axis=0))
        support = np.mean(edge_distance[on_edge[:, 1], on_edge[:, 0]] < 5)
        contrasts.append(min(contrast / 45, 1.0))
        supports.append(support)
        if background is not None:
            color, threshold = background
            inner_difference = np.linalg.norm(inner - color, axis=1)
            outer_difference = np.linalg.norm(outer - color, axis=1)
            separations.append(np.mean((inner_difference > threshold)
                                       & (outer_difference <= threshold)))
    if sum(c > 0.15 or s > 0.5 for c, s in zip(contrasts, supports)) < 3:
        return None
    score = 0.35 * np.mean(contrasts) + 0.25 * np.mean(supports)
    if separations:
        score += 0.2 * np.mean(separations)
    return float(score)


def refine_card_corners(points, contour, image_shape):
    """Fit the straight side segments, excluding rounded ends, then intersect."""
    samples = contour.reshape(-1, 2).astype(np.float32)
    lengths = np.linalg.norm(np.roll(points, -1, axis=0) - points, axis=1)
    band = max(3.0, float(min(lengths)) * 0.02)
    lines = []
    for start, end in zip(points, np.roll(points, -1, axis=0)):
        direction = end - start
        length = np.linalg.norm(direction)
        unit = direction / length
        relative = samples - start
        along = (relative @ unit) / length
        distance = np.abs(relative[:, 0] * unit[1] - relative[:, 1] * unit[0])
        selected = samples[(along > 0.15) & (along < 0.85) & (distance < band)]
        if len(selected) < 10:
            return points
        line = cv2.fitLine(selected, cv2.DIST_HUBER, 0, 0.01, 0.01).reshape(4)
        lines.append((line[:2], line[2:]))
    corners = []
    for index in range(4):
        previous_direction, previous_origin = lines[index - 1]
        direction, origin = lines[index]
        matrix = np.column_stack((previous_direction, -direction))
        if abs(np.linalg.det(matrix)) < 0.2:
            return points
        parameters = np.linalg.solve(matrix, origin - previous_origin)
        corners.append(previous_origin + parameters[0] * previous_direction)
    refined = np.asarray(corners, dtype=np.float32)
    height, width = image_shape
    if (not np.isfinite(refined).all()
            or np.any(refined < 0)
            or np.any(refined > [width - 1, height - 1])
            or np.max(np.linalg.norm(refined - points, axis=1)) > 0.08 * min(lengths)
            or not cv2.isContourConvex(refined)):
        return points
    return refined


def detect_card(image):
    """Find a card using backdrop separation and evidence along its four sides."""
    height, width = image.shape[:2]
    scale = min(1.0, 1400 / max(height, width))
    small = cv2.resize(image, (round(width * scale), round(height * scale)))
    blurred = cv2.GaussianBlur(small, (5, 5), 0)
    lab_bytes = cv2.cvtColor(blurred, cv2.COLOR_BGR2LAB)
    lab = lab_bytes.astype(np.float32)
    color_masks, background = background_masks(lab)
    gray = cv2.cvtColor(blurred, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 35, 110)
    # Equal brightness does not imply equal color: include chromatic boundaries.
    for channel in (1, 2):
        edges = cv2.bitwise_or(edges, cv2.Canny(lab_bytes[:, :, channel], 20, 60))
    edge_distance = cv2.distanceTransform(255 - edges, cv2.DIST_L2, 3)
    _, threshold = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    sources = [(mask, True) for mask in color_masks]
    sources.extend((mask, False) for mask in (edges, threshold, cv2.bitwise_not(threshold)))
    kernel = np.ones((3, 3), np.uint8)
    image_area = small.shape[0] * small.shape[1]
    candidates = []
    seen = set()
    for mask, is_foreground in sources:
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        mode = cv2.RETR_EXTERNAL if is_foreground else cv2.RETR_LIST
        contours, _ = cv2.findContours(mask, mode, cv2.CHAIN_APPROX_NONE)
        for contour in contours:
            area = abs(cv2.contourArea(contour))
            if not 0.06 * image_area <= area <= 0.98 * image_area:
                continue
            outline = cv2.convexHull(contour) if is_foreground else contour
            perimeter = cv2.arcLength(outline, True)
            for epsilon in (0.01, 0.02, 0.035):
                quad = cv2.approxPolyDP(outline, epsilon * perimeter, True)
                if len(quad) != 4 or not cv2.isContourConvex(quad):
                    continue
                try:
                    points = order_corners(quad)
                except ValueError:
                    continue
                points = refine_card_corners(points, contour, small.shape[:2])
                key = tuple(np.rint(points / 3).astype(int).ravel())
                if key in seen:
                    continue
                seen.add(key)
                lengths = np.linalg.norm(np.roll(points, -1, axis=0) - points, axis=1)
                ratio = (lengths[0] + lengths[2]) / (lengths[1] + lengths[3])
                if not 1.15 <= ratio <= 2.25:
                    continue
                if min(lengths) < 0.08 * min(small.shape[:2]):
                    continue
                score = boundary_score(points, lab, edge_distance, background)
                if score is None:
                    continue
                shape = 1 / (1 + 4 * abs(np.log(ratio / CARD_RATIO)))
                score += 0.12 * shape + 0.08 * np.sqrt(area / image_area)
                # Restore each axis separately to account for resize rounding.
                original = points * [width / small.shape[1], height / small.shape[0]]
                candidates.append((score, original.astype(np.float32)))
    if not candidates:
        raise ValueError("未找到可靠的卡片四边形，请用 --manual 手动选四角")
    return max(candidates, key=lambda item: item[0])[1]


def select_corners(image, initial=None, index=1):
    """Local OpenCV review window; no photos or coordinates are saved."""
    height, width = image.shape[:2]
    scale = min(1.0, 1100 / width, 720 / height)
    preview = cv2.resize(image, (round(width * scale), round(height * scale)))
    factors = np.array([preview.shape[1] / width, preview.shape[0] / height])
    points = [] if initial is None else (initial * factors).tolist()
    title = f"Card {index}/4 - Enter: accept | R: reset | Esc: cancel"

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            if len(points) == 4:
                points.clear()
            points.append([x, y])
        elif event == cv2.EVENT_RBUTTONDOWN and points:
            points.pop()

    try:
        cv2.namedWindow(title, cv2.WINDOW_AUTOSIZE)
        cv2.setMouseCallback(title, on_mouse)
        while True:
            frame = preview.copy()
            valid = None
            if len(points) == 4:
                try:
                    valid = order_corners(np.array(points) / factors)
                    outline = np.rint(valid * factors).astype(np.int32)
                    cv2.polylines(frame, [outline], True, (0, 220, 0), 2)
                except ValueError:
                    pass
            for point in points:
                cv2.circle(frame, tuple(np.rint(point).astype(int)), 5, (0, 0, 255), -1)
            cv2.putText(frame, "Click 4 outer corners. Right-click: undo.",
                        (12, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
            cv2.imshow(title, frame)
            key = cv2.waitKey(30) & 0xFF
            if key == 27 or cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE) < 1:
                raise ValueError("已取消，未生成 PDF")
            if key in (ord("r"), ord("R")):
                points.clear()
            if key in (10, 13) and valid is not None:
                return valid
    except cv2.error as exc:
        raise ValueError("无法打开预览窗口，请在有桌面界面的本机运行 --manual / --review") from exc
    finally:
        cv2.destroyAllWindows()


def rectify_card(image, corners, dpi=300, inset_px=0):
    if type(inset_px) is not int or not 0 <= inset_px <= 20:
        raise ValueError("向内裁切必须为 0–20 的整数像素（以 300 DPI 为基准）")
    points = order_corners(corners)
    width = round(CARD_WIDTH_MM / 25.4 * dpi)
    height = round(CARD_HEIGHT_MM / 25.4 * dpi)
    target = np.float32([[0, 0], [width - 1, 0],
                         [width - 1, height - 1], [0, height - 1]])
    if inset_px:
        # Use the same physical inset at preview, 300-DPI and 600-DPI resolutions.
        # Incorporate the crop into the homography to avoid a second resampling.
        reference = np.float32([round(CARD_WIDTH_MM / 25.4 * 300) - 1,
                                round(CARD_HEIGHT_MM / 25.4 * 300) - 1])
        size = np.float32([width - 1, height - 1])
        margin = inset_px * size / reference
        target = (target - margin) * size / (size - 2 * margin)
    transform = cv2.getPerspectiveTransform(points, target)
    return cv2.warpPerspective(image, transform, (width, height),
                               flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def card_positions():
    """Coordinates in PDF points, front/back per row, first ID in the top row."""
    page_width, page_height = A4
    card_width, card_height = CARD_WIDTH_MM * mm, CARD_HEIGHT_MM * mm
    gap_x, gap_y = 12 * mm, 20 * mm
    left = (page_width - 2 * card_width - gap_x) / 2
    bottom = (page_height - 2 * card_height - gap_y) / 2
    return [(left, bottom + card_height + gap_y),
            (left + card_width + gap_x, bottom + card_height + gap_y),
            (left, bottom), (left + card_width + gap_x, bottom)]


def watermark_settings(value):
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("text"), str):
        raise ValueError("水印参数格式不正确")
    text = " ".join(value["text"].split())
    if not text or len(text) > 40 or any(not char.isprintable() for char in text):
        raise ValueError("水印请输入 1–40 个可打印字符")
    if any(ord(char) > 0xFFFF for char in text):
        raise ValueError("水印暂不支持 Emoji 等扩展字符，请使用常用中英文")
    result = {"text": text}
    for key, default, low, high in (("font_size", 12, 6, 36), ("angle", -30, -90, 90),
                                    ("density", 3, 1, 5), ("transparency", 75, 0, 95)):
        number = value.get(key, default)
        if type(number) not in (int, float) or not np.isfinite(number) or not low <= number <= high:
            raise ValueError(f"水印 {key} 必须介于 {low} 和 {high} 之间")
        result[key] = float(number)
    return result


def draw_watermark(pdf, settings, x=0, y=0, grayscale=False):
    """Tile the same watermark on the standalone preview and each PDF card."""
    if settings is None:
        return
    font_name = "WatermarkCJK"
    if font_name not in pdfmetrics.getRegisteredFontNames():
        # Bundle/subset a real CJK font so printing needs no system font download.
        import pymupdf
        pdfmetrics.registerFont(TTFont(font_name, BytesIO(pymupdf.Font("cjk").buffer)))
    width, height = CARD_WIDTH_MM * mm, CARD_HEIGHT_MM * mm
    font_size = settings["font_size"]
    text = settings["text"]
    gap = (6 - settings["density"]) * 3 * mm
    step_x = pdfmetrics.stringWidth(text, font_name, font_size) + gap
    step_y = font_size * 1.3 + gap
    radius = np.hypot(width, height) / 2 + font_size
    columns = int(np.ceil(radius / step_x)) + 1
    rows = int(np.ceil(radius / step_y)) + 1
    pdf.saveState()
    outline = pdf.beginPath()
    outline.roundRect(x, y, width, height, CARD_CORNER_RADIUS_MM * mm)
    pdf.clipPath(outline, stroke=0, fill=0)
    pdf.translate(x + width / 2, y + height / 2)
    pdf.rotate(-settings["angle"])
    pdf.setFont(font_name, font_size)
    if grayscale:
        pdf.setFillGray(0.299 * 0.32 + 0.587 * 0.37 + 0.114 * 0.42)
    else:
        pdf.setFillColorRGB(0.32, 0.37, 0.42)
    pdf.setFillAlpha(1 - settings["transparency"] / 100)
    for row in range(-rows, rows + 1):
        for column in range(-columns, columns + 1):
            pdf.drawCentredString(column * step_x, row * step_y - font_size * 0.35, text)
    pdf.restoreState()


def watermark_preview(settings):
    """Render only the watermark to a transparent PNG; never accepts ID photos."""
    import pymupdf
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(CARD_WIDTH_MM * mm, CARD_HEIGHT_MM * mm))
    draw_watermark(pdf, settings)
    pdf.showPage()
    pdf.save()
    with pymupdf.open(stream=buffer.getvalue(), filetype="pdf") as document:
        return document[0].get_pixmap(matrix=pymupdf.Matrix(3, 3), alpha=True).tobytes("png")


def render_pdf(cards, watermark=None, grayscale=False, jpeg=True):
    if len(cards) != 4:
        raise ValueError("必须提供四张卡面")
    if type(grayscale) is not bool:
        raise ValueError("黑白输出开关必须是布尔值")
    if type(jpeg) is not bool:
        raise ValueError("JPEG 压缩开关必须是布尔值")
    watermark = watermark_settings(watermark)
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=1)
    pdf.setTitle("ID Card Print - A4 - Actual Size")
    pdf.setAuthor("")
    # Viewers may honor this preference, but printer settings still need checking.
    pdf.setViewerPreference("PrintScaling", "None")
    for card, (x, y) in zip(cards, card_positions()):
        rgb = cv2.cvtColor(card, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb)
        if grayscale:
            image = image.convert("L")
        if jpeg:
            encoded = BytesIO()
            image.save(encoded, format="JPEG", quality=95, subsampling=0, optimize=True)
            encoded.seek(0)
            # Keep the JPEG stream so ReportLab embeds it directly as DCTDecode.
            image_reader = ImageReader(encoded)
        else:
            image_reader = ImageReader(image)
        pdf.saveState()
        outline = pdf.beginPath()
        outline.roundRect(x, y, CARD_WIDTH_MM * mm, CARD_HEIGHT_MM * mm,
                          CARD_CORNER_RADIUS_MM * mm)
        pdf.clipPath(outline, stroke=0, fill=0)
        pdf.drawImage(image_reader, x, y,
                      width=CARD_WIDTH_MM * mm, height=CARD_HEIGHT_MM * mm)
        pdf.restoreState()
        draw_watermark(pdf, watermark, x, y, grayscale=grayscale)
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="四张身份证正反面照片 → 自动裁切 / 透视矫正 → 原尺寸单页 A4 PDF",
        epilog="输入顺序：第一张正面 第一张反面 第二张正面 第二张反面。打印选择实际大小 / 100%。")
    parser.add_argument("photos", nargs=4, type=Path, metavar="PHOTO")
    parser.add_argument("-o", "--output", type=Path, default=Path("id-cards-a4.pdf"))
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--manual", action="store_true", help="为每张照片手动点击卡片四角")
    modes.add_argument("--already-cropped", action="store_true", help="输入已准确裁好的卡面，跳过检测")
    parser.add_argument("--review", action="store_true", help="逐张预览检测四角，可点击重新选择")
    parser.add_argument("--rotate", nargs=4, type=int, choices=(0, 180), default=[0] * 4,
                        metavar="DEG", help="各卡面矫正后额外旋转 0 或 180 度；默认全部为 0")
    parser.add_argument("--dpi", type=int, default=300, help="PDF 中卡面采样分辨率，150–1200，默认 300")
    args = parser.parse_args(argv)
    if not 150 <= args.dpi <= 1200:
        parser.error("--dpi 必须介于 150 和 1200 之间")
    if args.already_cropped and args.review:
        parser.error("--already-cropped 不与 --review 同时使用")
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.output.suffix.lower() != ".pdf":
            raise ValueError("输出文件必须使用 .pdf 扩展名")
        if args.output.exists():
            raise ValueError(f"输出文件已存在，请用 -o 选择新路径，不会覆盖：{args.output}")
        if not args.output.parent.is_dir():
            raise ValueError(f"输出目录不存在：{args.output.parent}")
        cards = []
        for index, (path, rotation) in enumerate(zip(args.photos, args.rotate), 1):
            print(f"[{index}/4] 处理 {path.name}", flush=True)
            image = load_photo(path)
            if args.manual:
                corners = select_corners(image, index=index)
            elif args.already_cropped:
                height, width = image.shape[:2]
                ratio = max(width, height) / min(width, height)
                if abs(ratio / CARD_RATIO - 1) > 0.08:
                    raise ValueError(f"{path.name} 的宽高比不像完整卡面，请改用 --manual")
                corners = np.float32([[0, 0], [width - 1, 0],
                                      [width - 1, height - 1], [0, height - 1]])
            else:
                try:
                    corners = detect_card(image)
                except ValueError:
                    if not args.review:
                        raise
                    corners = None
                if args.review:
                    corners = select_corners(image, initial=corners, index=index)
            card = rectify_card(image, corners, args.dpi)
            if rotation:
                card = cv2.rotate(card, cv2.ROTATE_180)
            cards.append(card)
        data = render_pdf(cards)
        with args.output.open("xb") as output:
            output.write(data)
        print(f"已生成：{args.output.resolve()}")
        print("A4 单页；每个卡面 85.6 × 54 mm；打印选择「实际大小 / 100%」，关闭适合页面。")
        print("请检查 PDF 是否完整裁切、方向正确。自动检测分析颜色和边缘，不识别证件内容。")
        return 0
    except (OSError, ValueError, cv2.error, Image.DecompressionBombError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
