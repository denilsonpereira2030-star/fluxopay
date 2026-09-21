from __future__ import annotations

import io
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter, ImageOps
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas as rl_canvas

A4_W, A4_H = A4


def _criar_pagina_divisoria(estabelecimento: str, qtd_boletos: int = 0) -> bytes:
    buffer = io.BytesIO()
    c = rl_canvas.Canvas(buffer, pagesize=A4)
    w, h = A4_W, A4_H

    c.setFillColor(HexColor("#FFFFFF"))
    c.rect(0, 0, w, h, fill=1, stroke=0)

    c.setFillColor(HexColor("#1D4ED8"))
    c.rect(0, h - 40, w, 40, fill=1, stroke=0)

    c.setFillColor(HexColor("#FFFFFF"))
    c.setFont("Helvetica-Bold", 11)
    c.drawString(24, h - 26, "FluxoPay · Gestão de Contas a Pagar")

    data_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    c.setFont("Helvetica", 10)
    c.drawRightString(w - 24, h - 26, f"Gerado em {data_str}")

    c.setStrokeColor(HexColor("#E2E8F0"))
    c.setLineWidth(1)
    c.line(24, h - 56, w - 24, h - 56)

    centro_y = h / 2

    c.setFillColor(HexColor("#64748B"))
    c.setFont("Helvetica", 10)
    c.drawCentredString(w / 2, centro_y + 52, "INÍCIO DOS BOLETOS DA LOJA:")

    c.setStrokeColor(HexColor("#1D4ED8"))
    c.setLineWidth(2.5)
    c.line(w / 2 - 40, centro_y + 44, w / 2 + 40, centro_y + 44)

    nome_upper = estabelecimento.upper()
    font_size = 30 if len(nome_upper) <= 20 else (24 if len(nome_upper) <= 30 else 18)
    c.setFillColor(HexColor("#0F172A"))
    c.setFont("Helvetica-Bold", font_size)
    c.drawCentredString(w / 2, centro_y + 8, nome_upper)

    c.setStrokeColor(HexColor("#1D4ED8"))
    c.setLineWidth(2.5)
    c.line(w / 2 - 40, centro_y - 4, w / 2 + 40, centro_y - 4)

    if qtd_boletos > 0:
        plural = "s" if qtd_boletos != 1 else ""
        c.setFillColor(HexColor("#64748B"))
        c.setFont("Helvetica", 10)
        c.drawCentredString(w / 2, centro_y - 24, f"{qtd_boletos} boleto{plural} neste lote")

    c.setStrokeColor(HexColor("#E2E8F0"))
    c.setLineWidth(1)
    c.line(24, 36, w - 24, 36)

    c.setFillColor(HexColor("#94A3B8"))
    c.setFont("Helvetica", 8)
    c.drawCentredString(w / 2, 22, "Documento gerado automaticamente — uso interno")

    c.showPage()
    c.save()
    return buffer.getvalue()


def gerar_pdf_agrupado_bytes(rows: list[dict]) -> bytes:
    """Gera o PDF do lote em memória, lendo arquivo_dados de cada boleto."""
    from . import db as _db

    if not rows:
        raise ValueError("Nenhum boleto encontrado para gerar o PDF.")

    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["estabelecimento"]].append(row)

    writer = PdfWriter()
    for estabelecimento, boletos in grouped.items():
        div_bytes = _criar_pagina_divisoria(estabelecimento, qtd_boletos=len(boletos))
        writer.add_page(PdfReader(io.BytesIO(div_bytes)).pages[0])

        for row in boletos:
            dados = _db.get_boleto_arquivo(row["id"])
            if not dados:
                continue
            try:
                reader = PdfReader(io.BytesIO(dados))
                for page in reader.pages:
                    writer.add_page(page)
            except Exception:
                # arquivo corrompido — pula sem travar o lote
                pass

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def gerar_lote_impressao_bytes(rows: list[dict]) -> bytes:
    return gerar_pdf_agrupado_bytes(rows)


def gerar_pdf_urgentes_bytes(rows: list[dict]) -> bytes:
    return gerar_pdf_agrupado_bytes(rows)


def _scanner_filter(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img)
    gray = img.convert("L")
    gray = ImageOps.autocontrast(gray, cutoff=0.5)
    gray = gray.filter(ImageFilter.UnsharpMask(radius=1.2, percent=120, threshold=3))

    arr = np.array(gray, dtype=np.float32)
    pad = 16
    mean_img = Image.fromarray(arr.astype(np.uint8)).filter(ImageFilter.BoxBlur(pad))
    mean_arr = np.array(mean_img, dtype=np.float32)
    binary = np.where(arr >= mean_arr - 10, 255, 0).astype(np.uint8)
    result = Image.fromarray(binary, mode="L")

    a4_w_px, a4_h_px = 1654, 2339
    rw, rh = result.size
    scale = min(a4_w_px / rw, a4_h_px / rh)
    result = result.resize((int(rw * scale), int(rh * scale)), Image.LANCZOS)

    page = Image.new("L", (a4_w_px, a4_h_px), 255)
    page.paste(result, ((a4_w_px - result.width) // 2, (a4_h_px - result.height) // 2))
    return page


def processar_upload(file_bytes: bytes, original_name: str, content_type: str) -> tuple[str, str, bytes]:
    """
    Recebe os bytes brutos do upload e retorna (nome_arquivo, mime_type, bytes_pdf).
    Imagens passam pelo filtro scanner e são convertidas para PDF.
    PDFs são devolvidos como estão.
    """
    ext_original = Path(original_name or "arquivo").suffix.lower()
    is_image = (
        (content_type and content_type.startswith("image/"))
        or ext_original in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"}
    )

    nome_limpo = "".join(c for c in (original_name or "arquivo") if c.isalnum() or c in "._- ").replace(" ", "_")
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")

    if is_image:
        raw = Image.open(io.BytesIO(file_bytes))
        scanned = _scanner_filter(raw)
        buf = io.BytesIO()
        scanned.save(buf, "PDF", resolution=200)
        nome = f"{timestamp}_{nome_limpo}_scan.pdf"
        return nome, "application/pdf", buf.getvalue()

    nome = f"{timestamp}_{nome_limpo}.pdf"
    return nome, "application/pdf", file_bytes
