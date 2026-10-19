"use strict";

const $ = (selector) => document.querySelector(selector);
const token = $('meta[name="local-token"]').content;
const labels = ["第一张 · 正面", "第一张 · 反面", "第二张 · 正面", "第二张 · 反面"];
const slots = [null, null, null, null];
const views = [];
const dialog = $("#crop-dialog");
const overlay = $("#corner-overlay");
const svgNS = "http://www.w3.org/2000/svg";
let editor = null;
let exporting = false;
const watermark = { url: null, settings: null, pending: false, error: "", sequence: 0, controller: null, timer: null };

function renderWatermarks() {
  const enabled = $("#watermark-enabled").checked;
  $("#watermark-fields").hidden = !enabled;
  $("#watermark-fields").disabled = exporting || !enabled;
  $("#watermark-enabled").disabled = exporting;
  $("#density-value").textContent = `${$("#watermark-density").value} / 5`;
  $("#transparency-value").textContent = `${$("#watermark-transparency").value}%`;
  const status = $("#watermark-status");
  status.hidden = !enabled;
  status.classList.toggle("error", Boolean(watermark.error));
  status.textContent = watermark.error || (watermark.pending ? "正在更新水印预览…" : "预览已更新，下载将使用同样的排布。");
  for (const card of document.querySelectorAll(".paper-card")) {
    card.querySelector(".watermark-overlay")?.remove();
    if (enabled && watermark.url && !watermark.pending) {
      const image = document.createElement("img");
      image.className = "watermark-overlay";
      image.src = watermark.url;
      image.alt = "用途水印";
      card.append(image);
    }
  }
  const blocked = enabled && (watermark.pending || !watermark.url || Boolean(watermark.error));
  $("#download").disabled = exporting || !slots.every((entry) => entry?.confirmed) || blocked;
  if (blocked) $("#download-hint").textContent = "请先完成水印设置，等待预览更新";
}

function releaseWatermark() {
  clearTimeout(watermark.timer);
  watermark.controller?.abort();
  watermark.sequence += 1;
  if (watermark.url) URL.revokeObjectURL(watermark.url);
  Object.assign(watermark, { url: null, settings: null, pending: false, error: "" });
}

function updateWatermark() {
  if (exporting) return;
  releaseWatermark();
  if (!$("#watermark-enabled").checked) {
    render();
    return;
  }
  const text = $("#watermark-text").value.trim().replace(/\s+/gu, " ");
  const fontSize = $("#watermark-size").valueAsNumber;
  const angle = $("#watermark-angle").valueAsNumber;
  if (!text || Array.from(text).length > 40) watermark.error = "请输入 1–40 字的用途说明。";
  else if (!Number.isFinite(fontSize) || fontSize < 6 || fontSize > 36) watermark.error = "字号请输入 6–36 pt。";
  else if (!Number.isFinite(angle) || angle < -90 || angle > 90) watermark.error = "角度请输入 -90° 至 90°。";
  if (watermark.error) {
    render();
    return;
  }
  const settings = {
    text, font_size: fontSize, angle,
    density: Number($("#watermark-density").value),
    transparency: Number($("#watermark-transparency").value),
  };
  const sequence = watermark.sequence;
  watermark.pending = true;
  watermark.controller = new AbortController();
  render();
  watermark.timer = setTimeout(async () => {
    try {
      const response = await api("/api/watermark", settings, watermark.controller.signal);
      const blob = await response.blob();
      if (sequence !== watermark.sequence) return;
      watermark.url = URL.createObjectURL(blob);
      watermark.settings = settings;
    } catch (error) {
      if (sequence !== watermark.sequence || error.name === "AbortError") return;
      watermark.error = error.message;
    } finally {
      if (sequence === watermark.sequence) {
        watermark.pending = false;
        render();
      }
    }
  }, 350);
}

$("#watermark-enabled").addEventListener("change", updateWatermark);
$("#watermark-fields").addEventListener("input", updateWatermark);

async function api(path, data, signal) {
  let response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Local-Token": token },
      body: JSON.stringify(data),
      cache: "no-store",
      signal,
    });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new Error("无法连接本地服务，请检查 Python 服务是否仍在运行。");
  }
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.error || `请求失败（${response.status}），请重试。`);
  }
  return response;
}

function announce(text, error = false) {
  const message = $("#message");
  message.textContent = text;
  message.classList.toggle("error", error);
  message.hidden = false;
}

function readFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.split(",", 2)[1]);
    reader.onerror = () => reject(new Error("无法读取文件，请重新选择照片。"));
    reader.readAsDataURL(file);
  });
}

function createSlots() {
  for (let group = 0; group < 2; group += 1) {
    const section = document.createElement("section");
    section.className = "card-group";
    const heading = document.createElement("h3");
    heading.textContent = group === 0 ? "第一张身份证" : "第二张身份证";
    const caption = document.createElement("span");
    caption.textContent = "正面 + 反面";
    heading.append(caption);
    const grid = document.createElement("div");
    grid.className = "slot-grid";
    section.append(heading, grid);
    $("#card-groups").append(section);
    for (let face = 0; face < 2; face += 1) {
      const index = group * 2 + face;
      const slot = $("#slot-template").content.firstElementChild.cloneNode(true);
      slot.id = `slot-${index}`;
      slot.querySelector("h4").textContent = face === 0 ? "正面 · 人像面" : "反面 · 国徽面";
      const input = slot.querySelector("input");
      input.id = `photo-${index}`;
      input.setAttribute("aria-label", `上传${labels[index]}照片`);
      const upload = slot.querySelector(".upload-area");
      upload.setAttribute("aria-label", `上传或编辑${labels[index]}`);
      input.addEventListener("change", () => {
        if (input.files[0]) uploadPhoto(index, input.files[0]);
        input.value = "";
      });
      upload.addEventListener("click", () => {
        if (exporting) return;
        if (slots[index]?.preview) openEditor(index);
        else input.click();
      });
      slot.querySelector(".replace-button").addEventListener("click", () => input.click());
      slot.querySelector(".remove-button").addEventListener("click", () => {
        if (exporting) return;
        slots[index]?.controller?.abort();
        slots[index] = null;
        render();
      });
      for (const name of ["dragenter", "dragover"]) {
        upload.addEventListener(name, (event) => {
          event.preventDefault();
          if (!exporting) upload.classList.add("drag-over");
        });
      }
      upload.addEventListener("dragleave", () => upload.classList.remove("drag-over"));
      upload.addEventListener("drop", (event) => {
        event.preventDefault();
        upload.classList.remove("drag-over");
        if (exporting) return;
        if (event.dataTransfer.files.length !== 1) {
          announce("每个位置请选择一张照片。", true);
          return;
        }
        uploadPhoto(index, event.dataTransfer.files[0]);
      });
      grid.append(slot);
      views.push(slot);
    }
  }
}

function render() {
  let ready = 0;
  for (let index = 0; index < 4; index += 1) {
    const entry = slots[index];
    const view = views[index];
    const state = !entry ? "empty" : entry.error ? "error" : entry.loading ? "loading" : entry.confirmed ? "confirmed" : "review";
    view.dataset.state = state;
    const status = { empty: "待上传", loading: "处理中…", review: "待确认", confirmed: "✓ 已确认", error: "需重试" };
    view.querySelector(".slot-status").textContent = status[state];
    const image = view.querySelector(".slot-image");
    const src = entry?.crop || entry?.preview;
    image.classList.toggle("cropped", Boolean(entry?.crop));
    image.hidden = !src;
    if (src) {
      if (image.getAttribute("src") !== src) image.src = src;
      image.alt = `${labels[index]}预览`;
    } else image.removeAttribute("src");
    view.querySelector(".upload-empty").hidden = Boolean(src);
    view.querySelector(".slot-overlay").hidden = !src;
    view.querySelector(".slot-overlay").textContent = entry?.confirmed ? "重新编辑" : "核对裁切与方向";
    view.querySelector(".upload-area").disabled = exporting || Boolean(entry?.loading);
    view.querySelector(".file-input").disabled = exporting;
    view.querySelector(".slot-name").textContent = entry?.name || "未选择文件";
    view.querySelector(".replace-button").disabled = exporting || !entry;
    view.querySelector(".remove-button").disabled = exporting || !entry;
    const error = view.querySelector(".slot-error");
    error.hidden = !entry?.error;
    error.textContent = entry?.error || "";
    const paperCard = $(`#paper-card-${index}`);
    paperCard.replaceChildren();
    paperCard.classList.toggle("has-image", Boolean(entry?.confirmed));
    if (entry?.confirmed) {
      ready += 1;
      const cardImage = document.createElement("img");
      cardImage.src = entry.crop;
      cardImage.alt = labels[index];
      paperCard.append(cardImage);
    } else {
      const placeholder = document.createElement("span");
      placeholder.textContent = labels[index];
      paperCard.append(placeholder);
    }
  }
  $("#ready-count").textContent = `${ready} / 4 已确认`;
  $("#clear-all").disabled = exporting || !slots.some(Boolean);
  $("#download").disabled = exporting || ready !== 4;
  $("#dpi").disabled = exporting;
  $("#grayscale-enabled").disabled = exporting;
  $("#jpeg-enabled").disabled = exporting;
  const grayscale = $("#grayscale-enabled").checked;
  $("#paper").classList.toggle("is-grayscale", grayscale);
  $("#download span").textContent = exporting ? "正在生成 PDF…" : grayscale ? "下载黑白 A4 PDF" : "下载 A4 PDF";
  $("#download-hint").textContent = exporting ? "正在本机排版，请稍候" : ready === 4 ? "单页 A4 · 每个卡面 85.6 × 54 mm" : "确认全部四个卡面后即可下载";
  renderWatermarks();
}

async function uploadPhoto(index, file) {
  if (exporting) return;
  slots[index]?.controller?.abort();
  const entry = { name: file.name, loading: true, confirmed: false, rotation: 0, controller: new AbortController() };
  slots[index] = entry;
  render();
  try {
    if (file.size > 12 * 1024 * 1024) throw new Error("照片超过 12 MB，请先压缩或降低分辨率。");
    if (!file.size) throw new Error("文件为空，请重新选择照片。");
    entry.image = await readFile(file);
    if (slots[index] !== entry) return;
    const response = await api("/api/analyze", { image: entry.image }, entry.controller.signal);
    const result = await response.json();
    if (slots[index] !== entry) return;
    Object.assign(entry, result, { loading: false });
    render();
    if (!dialog.open) openEditor(index);
  } catch (error) {
    if (slots[index] !== entry || error.name === "AbortError") return;
    entry.loading = false;
    entry.error = error.message;
    entry.image = null;
    render();
  }
}

function openEditor(index) {
  const entry = slots[index];
  if (!entry?.preview || entry.loading || exporting) return;
  editor = {
    index, entry,
    corners: entry.corners ? entry.corners.map((point) => [...point]) : [],
    rotation: entry.rotation,
    insetPx: entry.inset_px ?? 0,
    dirty: !entry.crop,
    busy: false,
    dragging: null,
    controller: new AbortController(),
  };
  $("#editor-title").textContent = labels[index];
  $("#inset-px").value = String(editor.insetPx);
  $("#source-image").src = entry.preview;
  $("#photo-stage").style.aspectRatio = `${entry.width} / ${entry.height}`;
  $("#photo-stage").style.width = `${Math.min(100, 460 * entry.width / entry.height / 600 * 100)}%`;
  overlay.setAttribute("viewBox", `0 0 ${entry.width} ${entry.height}`);
  $("#editor-error").hidden = true;
  showCrop(entry.crop);
  dialog.showModal();
  drawCorners();
  updateEditorButtons();
}

function closeEditor() {
  editor?.controller.abort();
  editor = null;
  dialog.close();
  overlay.replaceChildren();
  $("#source-image").removeAttribute("src");
  $("#crop-image").removeAttribute("src");
}

function showCrop(src) {
  const image = $("#crop-image");
  image.hidden = !src;
  if (src) image.src = src;
  else image.removeAttribute("src");
  $("#crop-placeholder").hidden = Boolean(src);
  $("#crop-caption").textContent = src ? "检查文字是否完整、方向是否正确。" : "先选齐四角，再点击「更新预览」。";
}

function updateEditorButtons() {
  if (!editor) return;
  const enough = editor.corners.length === 4;
  $("#apply-crop").disabled = editor.busy || !enough;
  $("#preview-crop").disabled = editor.busy || !enough;
  $("#rotate-card").disabled = editor.busy || !enough;
  $("#reset-corners").disabled = editor.busy;
  $("#inset-px").disabled = editor.busy;
  $("#apply-crop").textContent = editor.busy ? "正在裁切…" : "确认并使用此卡面";
  $("#corner-status").textContent = enough ? "已选 4 个外角 · 拖动圆点微调，方向键每次移动 1 px" : `已选 ${editor.corners.length} / 4 个外角 · 点击照片补齐四角（顺序不限）`;
}

function sortedPoints(points) {
  const center = points.reduce((sum, p) => [sum[0] + p[0] / points.length, sum[1] + p[1] / points.length], [0, 0]);
  return [...points].sort((a, b) => Math.atan2(a[1] - center[1], a[0] - center[0]) - Math.atan2(b[1] - center[1], b[0] - center[0]));
}

function drawCorners(focused = null) {
  if (!editor) return;
  overlay.replaceChildren();
  const { width, height } = editor.entry;
  const pixels = editor.corners.map(([x, y]) => [x * (width - 1), y * (height - 1)]);
  if (pixels.length > 1) {
    const polygon = document.createElementNS(svgNS, "polygon");
    polygon.setAttribute("points", sortedPoints(pixels).map((point) => point.join(",")).join(" "));
    overlay.append(polygon);
  }
  const unitsPerPixel = width / Math.max(overlay.getBoundingClientRect().width, 100);
  pixels.forEach(([x, y], index) => {
    const circle = document.createElementNS(svgNS, "circle");
    circle.setAttribute("class", "corner-hit");
    circle.setAttribute("cx", x);
    circle.setAttribute("cy", y);
    circle.setAttribute("r", unitsPerPixel * 14);
    circle.setAttribute("tabindex", "0");
    circle.setAttribute("role", "button");
    circle.setAttribute("aria-label", `外角 ${index + 1}，使用方向键微调`);
    circle.dataset.index = index;
    circle.addEventListener("keydown", (event) => {
      if (!editor || editor.busy || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
      event.preventDefault();
      const step = event.shiftKey ? 10 : 1;
      const point = editor.corners[index];
      if (event.key === "ArrowLeft") point[0] -= step / (width - 1);
      if (event.key === "ArrowRight") point[0] += step / (width - 1);
      if (event.key === "ArrowUp") point[1] -= step / (height - 1);
      if (event.key === "ArrowDown") point[1] += step / (height - 1);
      editor.corners[index] = point.map(clamp);
      markDirty();
      drawCorners(index);
    });
    const marker = document.createElementNS(svgNS, "ellipse");
    marker.setAttribute("class", "corner-marker");
    marker.setAttribute("cx", x);
    marker.setAttribute("cy", y);
    marker.setAttribute("rx", unitsPerPixel * 4);
    marker.setAttribute("ry", unitsPerPixel * 4);
    marker.setAttribute("aria-hidden", "true");
    overlay.append(circle, marker);
  });
  if (focused !== null) overlay.querySelector(`[data-index="${focused}"]`)?.focus();
}

const clamp = (value) => Math.max(0, Math.min(1, value));

function pointerPosition(event) {
  const bounds = overlay.getBoundingClientRect();
  return [clamp((event.clientX - bounds.left) / bounds.width), clamp((event.clientY - bounds.top) / bounds.height)];
}

function markDirty() {
  editor.dirty = true;
  showCrop(null);
  $("#editor-error").hidden = true;
  updateEditorButtons();
}

overlay.addEventListener("pointerdown", (event) => {
  if (!editor || editor.busy || event.button !== 0) return;
  const index = event.target.dataset.index;
  if (index !== undefined) editor.dragging = Number(index);
  else if (editor.corners.length < 4) {
    editor.dragging = editor.corners.length;
    editor.corners.push(pointerPosition(event));
    markDirty();
    drawCorners();
  } else return;
  event.preventDefault();
  overlay.setPointerCapture(event.pointerId);
});
overlay.addEventListener("pointermove", (event) => {
  if (!editor || editor.dragging === null || editor.busy) return;
  editor.corners[editor.dragging] = pointerPosition(event);
  markDirty();
  drawCorners();
});
for (const name of ["pointerup", "pointercancel", "lostpointercapture"]) {
  overlay.addEventListener(name, () => { if (editor) editor.dragging = null; });
}

async function cropPreview(apply) {
  if (!editor || editor.busy || editor.corners.length !== 4) return;
  const current = editor;
  current.busy = true;
  updateEditorButtons();
  $("#editor-error").hidden = true;
  try {
    const response = await api("/api/crop", {
      image: current.entry.image, corners: current.corners, rotation: current.rotation,
      inset_px: current.insetPx,
    }, current.controller.signal);
    const result = await response.json();
    if (editor !== current) return;
    showCrop(result.crop);
    current.dirty = false;
    if (apply) {
      Object.assign(current.entry, {
        corners: current.corners.map((point) => [...point]),
        rotation: current.rotation, inset_px: current.insetPx, crop: result.crop, confirmed: true,
      });
      closeEditor();
      render();
    }
  } catch (error) {
    if (editor !== current || error.name === "AbortError") return;
    $("#editor-error").textContent = error.message;
    $("#editor-error").hidden = false;
  } finally {
    if (editor === current) {
      current.busy = false;
      updateEditorButtons();
    }
  }
}

$("#reset-corners").addEventListener("click", () => {
  if (!editor || editor.busy) return;
  editor.corners = [];
  markDirty();
  drawCorners();
});
$("#rotate-card").addEventListener("click", () => {
  if (!editor || editor.busy) return;
  editor.rotation = (editor.rotation + 180) % 360;
  markDirty();
  cropPreview(false);
});
$("#inset-px").addEventListener("change", () => {
  if (!editor || editor.busy) return;
  editor.insetPx = Number($("#inset-px").value);
  markDirty();
  cropPreview(false);
});
$("#preview-crop").addEventListener("click", () => cropPreview(false));
$("#apply-crop").addEventListener("click", () => cropPreview(true));
$("#close-editor").addEventListener("click", closeEditor);
$("#cancel-editor").addEventListener("click", closeEditor);
dialog.addEventListener("cancel", (event) => { event.preventDefault(); closeEditor(); });
window.addEventListener("resize", () => { if (editor) drawCorners(); });

$("#clear-all").addEventListener("click", () => {
  if (exporting) return;
  for (let index = 0; index < 4; index += 1) {
    slots[index]?.controller?.abort();
    slots[index] = null;
  }
  closeEditor();
  releaseWatermark();
  $("#watermark-enabled").checked = false;
  $("#grayscale-enabled").checked = false;
  $("#jpeg-enabled").checked = true;
  $("#watermark-text").value = $("#watermark-text").defaultValue;
  render();
  announce("本次照片、裁切结果与水印文字已从页面中清空。已下载的 PDF 不受影响。");
});

$("#grayscale-enabled").addEventListener("change", render);

$("#download").addEventListener("click", async () => {
  if (exporting || !slots.every((entry) => entry?.confirmed)) return;
  if ($("#watermark-enabled").checked && (watermark.pending || !watermark.url || watermark.error)) return;
  exporting = true;
  render();
  try {
    const response = await api("/api/pdf", {
      dpi: Number($("#dpi").value),
      grayscale: $("#grayscale-enabled").checked,
      jpeg: $("#jpeg-enabled").checked,
      watermark: $("#watermark-enabled").checked ? watermark.settings : null,
      cards: slots.map(({ image, corners, rotation, inset_px }) => ({ image, corners, rotation, inset_px })),
    });
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = $("#grayscale-enabled").checked ? "身份证-A4-黑白.pdf" : "身份证-A4.pdf";
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    announce("PDF 已生成并发起下载。打印请选择 A4、实际大小 / 100%，关闭「适合页面」。");
  } catch (error) {
    announce(error.message, true);
  } finally {
    exporting = false;
    render();
  }
});

// Do not retain sensitive state in a back/forward-cache snapshot.
window.addEventListener("pagehide", () => {
  slots.forEach((entry, index) => {
    entry?.controller?.abort();
    slots[index] = null;
  });
  closeEditor();
  releaseWatermark();
  $("#watermark-enabled").checked = false;
  $("#grayscale-enabled").checked = false;
  $("#jpeg-enabled").checked = true;
  $("#watermark-text").value = $("#watermark-text").defaultValue;
  render();
});

createSlots();
render();
