from __future__ import annotations

import io
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter, ImageOps
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas as rl_canvas

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOADS_DIR = BASE_DIR / "uploads"
LOTE_OUTPUT_PATH = BASE_DIR / "lote_impressao.pdf"
URGENTES_OUTPUT_PATH = BASE_DIR / "boletos_urgentes_hoje.pdf"

# Largura/altura A4 em pontos (72 dpi base do ReportLab)
A4_W, A4_H = A4


def _criar_pagina_divisoria(estabelecimento: str, qtd_boletos: int = 0) -> bytes:
    """
    Capa corporativa A4 branca para separar as lojas no PDF mesclado.
    Layout: fundo branco limpo com header azul topo, bloco central de texto,
    rodapé com data de geração.  Mantém legível após impressão P&B.
    """
    buffer = io.BytesIO()
    c = rl_canvas.Canvas(buffer, pagesize=A4)
    w, h = A4_W, A4_H

    # ── fundo branco ──────────────────────────────────────────────────────────
    c.setFillColor(HexColor("#FFFFFF"))
    c.rect(0, 0, w, h, fill=1, stroke=0)

    # ── faixa de topo azul (40 pt) ────────────────────────────────────────────
    c.setFillColor(HexColor("#1D4ED8"))
    c.rect(0, h - 40, w, 40, fill=1, stroke=0)

    c.setFillColor(HexColor("#FFFFFF"))
    c.setFont("Helvetica-Bold", 11)
    c.drawString(24, h - 26, "FluxoPay · Gestão de Contas a Pagar")

    data_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    c.setFont("Helvetica", 10)
    c.drawRightString(w - 24, h - 26, f"Gerado em {data_str}")

    # ── linha divisória horizontal ────────────────────────────────────────────
    c.setStrokeColor(HexColor("#E2E8F0"))
    c.setLineWidth(1)
    c.line(24, h - 56, w - 24, h - 56)

    # ── bloco central ─────────────────────────────────────────────────────────
    centro_y = h / 2

    # rótulo superior
    c.setFillColor(HexColor("#64748B"))
    c.setFont("Helvetica", 10)
    label = "INÍCIO DOS BOLETOS DA LOJA:"
    c.drawCentredString(w / 2, centro_y + 52, label)

    # traço decorativo curto acima do nome
    c.setStrokeColor(HexColor("#1D4ED8"))
    c.setLineWidth(2.5)
    c.line(w / 2 - 40, centro_y + 44, w / 2 + 40, centro_y + 44)

    # nome do estabelecimento — caixa alta, negrito, tamanho adaptativo
    nome_upper = estabelecimento.upper()
    font_size = 30 if len(nome_upper) <= 20 else (24 if len(nome_upper) <= 30 else 18)
    c.setFillColor(HexColor("#0F172A"))
    c.setFont("Helvetica-Bold", font_size)
    c.drawCentredString(w / 2, centro_y + 8, nome_upper)

    # traço decorativo abaixo do nome
    c.setStrokeColor(HexColor("#1D4ED8"))
    c.setLineWidth(2.5)
    c.line(w / 2 - 40, centro_y - 4, w / 2 + 40, centro_y - 4)

    # contagem de boletos
    if qtd_boletos > 0:
        plural = "s" if qtd_boletos != 1 else ""
        c.setFillColor(HexColor("#64748B"))
        c.setFont("Helvetica", 10)
        c.drawCentredString(w / 2, centro_y - 24, f"{qtd_boletos} boleto{plural} neste lote")

    # ── rodapé ────────────────────────────────────────────────────────────────
    c.setStrokeColor(HexColor("#E2E8F0"))
    c.setLineWidth(1)
    c.line(24, 36, w - 24, 36)

    c.setFillColor(HexColor("#94A3B8"))
    c.setFont("Helvetica", 8)
    c.drawCentredString(w / 2, 22, "Documento gerado automaticamente — uso interno")

    c.showPage()
    c.save()
    return buffer.getvalue()


def gerar_pdf_agrupado(rows: list[dict], output_path: Path) -> Path:
    if not rows:
        raise ValueError("Nenhum boleto encontrado para gerar o PDF.")

    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["estabelecimento"]].append(row)

    writer = PdfWriter()
    for estabelecimento, boletos in grouped.items():
        div_bytes = _criar_pagina_divisoria(estabelecimento, qtd_boletos=len(boletos))
        divisoria = PdfReader(io.BytesIO(div_bytes)).pages[0]
        writer.add_page(divisoria)
        for row in boletos:
            boleto_path = Path(row["caminho_arquivo"])
            if boleto_path.exists():
                reader = PdfReader(str(boleto_path))
                for page in reader.pages:
                    writer.add_page(page)

    with output_path.open("wb") as f:
        writer.write(f)

    return output_path


def gerar_lote_impressao_pdf(rows: list[dict]) -> Path:
    return gerar_pdf_agrupado(rows, LOTE_OUTPUT_PATH)


def gerar_pdf_urgentes(rows: list[dict]) -> Path:
    return gerar_pdf_agrupado(rows, URGENTES_OUTPUT_PATH)


def _scanner_filter(img: Image.Image) -> Image.Image:
    """
    Aplica processamento estilo scanner a uma foto de boleto:
    1. Corrige orientação EXIF (fotos de celular chegam rotacionadas).
    2. Converte para escala de cinza.
    3. Auto-contraste: estica o histograma para remover sombras de fundo.
    4. Nitidez leve para destacar texto e código de barras.
    5. Binarização adaptativa via numpy: pixels abaixo de um limiar local
       (média de uma janela 32x32) viram branco, os demais viram preto —
       elimina gradientes de iluminação sem apagar detalhes finos.
    6. Redimensiona para caber exatamente em A4 a 200 dpi sem distorcer.
    """
    # 1. orientação EXIF
    img = ImageOps.exif_transpose(img)

    # 2. grayscale
    gray = img.convert("L")

    # 3. auto-contraste (corta 0.5% de pixels extremos em cada ponta)
    gray = ImageOps.autocontrast(gray, cutoff=0.5)

    # 4. nitidez
    gray = gray.filter(ImageFilter.UnsharpMask(radius=1.2, percent=120, threshold=3))

    # 5. binarização adaptativa via numpy
    arr = np.array(gray, dtype=np.float32)
    pad = 16  # meia-janela de 32x32
    # média local com box-filter eficiente usando integral image
    from PIL import ImageFilter as _IF
    mean_img = Image.fromarray(arr.astype(np.uint8)).filter(
        ImageFilter.BoxBlur(pad)
    )
    mean_arr = np.array(mean_img, dtype=np.float32)
    # pixel vira branco se estiver acima de (média local - 10), preto caso contrário
    binary = np.where(arr >= mean_arr - 10, 255, 0).astype(np.uint8)
    result = Image.fromarray(binary, mode="L")

    # 6. redimensionar para A4 a 200 dpi (1654 x 2339 px), mantendo proporção
    a4_w_px, a4_h_px = 1654, 2339
    rw, rh = result.size
    scale = min(a4_w_px / rw, a4_h_px / rh)
    new_w, new_h = int(rw * scale), int(rh * scale)
    result = result.resize((new_w, new_h), Image.LANCZOS)

    # centralizar em página A4 branca
    page = Image.new("L", (a4_w_px, a4_h_px), 255)
    offset_x = (a4_w_px - new_w) // 2
    offset_y = (a4_h_px - new_h) // 2
    page.paste(result, (offset_x, offset_y))

    return page


def salvar_upload(file_bytes: bytes, original_name: str, content_type: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    nome_limpo = "".join(c for c in (original_name or "arquivo") if c.isalnum() or c in "._- ").replace(" ", "_")

    ext_original = Path(original_name or "arquivo").suffix.lower()
    is_image = (
        (content_type and content_type.startswith("image/"))
        or ext_original in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"}
    )

    if is_image:
        raw = Image.open(io.BytesIO(file_bytes))
        scanned = _scanner_filter(raw)
        # salva como PDF A4 com resolução declarada de 200 dpi
        pdf_path = UPLOADS_DIR / f"{timestamp}_{nome_limpo}_scan.pdf"
        scanned.save(pdf_path, "PDF", resolution=200)
        return str(pdf_path)

    # arquivo já é PDF — salva direto
    ext = ".pdf" if ("pdf" in (content_type or "") or ext_original == ".pdf") else ".bin"
    target = UPLOADS_DIR / f"{timestamp}_{nome_limpo}{ext}"
    target.write_bytes(file_bytes)
    return str(target)
