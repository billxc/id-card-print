# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.50,<2", "Pillow>=10.4,<13", "pypdf>=5,<7"]
# ///
"""Drive the running local app with synthetic images in an isolated browser."""

import argparse
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw
from playwright.sync_api import expect, sync_playwright
from pypdf import PdfReader


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--channel", default="chrome", help="Installed browser channel, e.g. chrome or msedge")
    parser.add_argument("--screenshots", type=Path, help="Optional directory for synthetic-only screenshots")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temp, sync_playwright() as playwright:
        directory = Path(temp)
        sample = directory / "sample.png"
        image = Image.new("RGB", (1200, 900), (38, 49, 64))
        draw = ImageDraw.Draw(image)
        draw.polygon([(210, 190), (1000, 250), (950, 750), (180, 660)], fill=(230, 234, 237))
        draw.text((410, 400), "SYNTHETIC TEST / NO PERSONAL DATA", fill=(50, 70, 95), font_size=24)
        draw.rectangle((340, 480, 780, 510), fill=(120, 145, 170))
        image.save(sample)
        blank = directory / "blank.png"
        Image.new("RGB", (1200, 900), "white").save(blank)
        browser = playwright.chromium.launch(channel=args.channel, headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1100}, accept_downloads=True)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        page.goto(args.url, wait_until="networkidle")
        expect(page.locator("h1")).to_contain_text("一张 A4")
        expect(page.locator("#download")).to_be_disabled()
        for index in range(4):
            page.locator(f"#photo-{index}").set_input_files(blank if index == 2 else sample)
            expect(page.locator("#crop-dialog")).to_be_visible()
            if index == 0:
                handle = page.locator("#corner-overlay circle").first
                bounds = handle.bounding_box()
                x, y = bounds["x"] + bounds["width"] / 2, bounds["y"] + bounds["height"] / 2
                page.mouse.move(x, y)
                page.mouse.down()
                page.mouse.move(x + 3, y + 3, steps=5)
                page.mouse.up()
                handle.focus()
                handle.press("ArrowRight")
                expect(page.locator("#crop-image")).to_be_hidden()
                with page.expect_response("**/api/crop") as response:
                    page.locator("#preview-crop").click()
                assert response.value.ok
                expect(page.locator("#crop-image")).to_be_visible()
            if index == 1:
                with page.expect_response("**/api/crop") as response:
                    page.locator("#rotate-card").click()
                assert response.value.ok
                expect(page.locator("#apply-crop")).to_be_enabled()
            if index == 2:
                expect(page.locator("#corner-overlay circle")).to_have_count(0)
                bounds = page.locator("#corner-overlay").bounding_box()
                for x, y in ((0.1, 0.18), (0.9, 0.18), (0.9, 0.82), (0.1, 0.82)):
                    page.mouse.click(bounds["x"] + x * bounds["width"], bounds["y"] + y * bounds["height"])
                expect(page.locator("#corner-overlay circle")).to_have_count(4)
            page.locator("#apply-crop").click()
            expect(page.locator("#crop-dialog")).not_to_be_visible()
            expect(page.locator("#ready-count")).to_have_text(f"{index + 1} / 4 已确认")
        expect(page.locator("#paper img")).to_have_count(4)
        page.locator("#dpi").select_option("600")
        with page.expect_download() as download:
            page.locator("#download").click()
        output = directory / "download.pdf"
        download.value.save_as(output)
        pdf = PdfReader(output)
        assert len(pdf.pages) == 1
        assert abs(float(pdf.pages[0].mediabox.width) * 25.4 / 72 - 210) < 0.01
        assert abs(float(pdf.pages[0].mediabox.height) * 25.4 / 72 - 297) < 0.01
        expect(page.locator("#download")).to_be_enabled()
        if args.screenshots:
            args.screenshots.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(args.screenshots / "desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if args.screenshots:
            page.screenshot(path=str(args.screenshots / "mobile.png"), full_page=True)
        page.locator("#slot-0 .upload-area").click()
        expect(page.locator("#crop-dialog")).to_be_visible()
        assert page.locator("#crop-dialog").evaluate("element => element.scrollWidth <= element.clientWidth")
        page.locator("#reset-corners").click()
        expect(page.locator("#apply-crop")).to_be_disabled()
        page.locator("#cancel-editor").click()
        expect(page.locator("#ready-count")).to_have_text("4 / 4 已确认")
        page.locator("#slot-0 .remove-button").click()
        expect(page.locator("#download")).to_be_disabled()
        expect(page.locator("#ready-count")).to_have_text("3 / 4 已确认")
        page.emulate_media(color_scheme="dark")
        page.locator("#clear-all").click()
        expect(page.locator("#ready-count")).to_have_text("0 / 4 已确认")
        expect(page.locator("#paper img")).to_have_count(0)
        page.reload(wait_until="networkidle")
        expect(page.locator("#ready-count")).to_have_text("0 / 4 已确认")
        assert not errors, errors
        context.close()
        browser.close()
    print("PASS: upload, automatic/manual corners, drag/keyboard, rotation, confirm, 600-DPI PDF download,")
    print("      mobile layout/editor, cancel, remove, clear, refresh; no browser console errors.")


if __name__ == "__main__":
    main()
