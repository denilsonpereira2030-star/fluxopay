from __future__ import annotations

import gc
import io
import os
import re
import tempfile
import unicodedata
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter, ImageOps
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas as rl_canvas

# Padrões para linha digitável e código de barras
_RE_LINHA = re.compile(
    r'\d{5}\.\d{5}\s+\d{5}\.\d{6}\s+\d{5}\.\d{6}\s+\d\s+\d{14}'
)
_RE_BARRAS = re.compile(r'(?<!\d)\d{44,48}(?!\d)')


def extrair_linha_digitavel(pdf_bytes: bytes) -> str | None:
    """Tenta extrair a linha digitável ou código de barras de um PDF."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        texto = ""
        for page in reader.pages:
            texto += (page.extract_text() or "") + "\n"
            if len(texto) > 8000:
                break

        m = _RE_LINHA.search(texto)
        if m:
            return re.sub(r'\s+', ' ', m.group()).strip()

        # Tenta código de barras numérico sem pontos
        texto_num = re.sub(r'[^\d\s]', ' ', texto)
        m2 = _RE_BARRAS.search(texto_num)
        if m2:
            return m2.group().strip()
    except Exception:
        pass
    return None

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


# ── lote de impressão ─────────────────────────────────────────────────────────

_IMG_MAX_PX = (1654, 2339)  # A4 a ~200 dpi — suficiente p/ impressão de boleto


def _tipo_arquivo(dados: bytes) -> str:
    """Detecta o tipo pelo conteúdo (magic bytes), não pela extensão."""
    cab = dados[:16]
    if cab.startswith(b"%PDF") or b"%PDF" in dados[:1024]:
        return "pdf"
    if cab.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if cab.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if cab[:4] == b"RIFF" and cab[8:12] == b"WEBP":
        return "webp"
    return "desconhecido"


def _imagem_para_pdf(dados: bytes) -> bytes:
    """Converte JPG/PNG em uma página PDF leve (reduz resolução antes de converter)."""
    with Image.open(io.BytesIO(dados)) as img:
        if img.format == "JPEG":
            img.draft("RGB", _IMG_MAX_PX)  # decodifica já reduzido: economiza RAM
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail(_IMG_MAX_PX, Image.LANCZOS)
        out = io.BytesIO()
        try:
            img.save(out, "PDF", resolution=200, quality=80)
            return out.getvalue()
        finally:
            out.close()
            img.close()


def _pagina_aviso(row: dict, motivo: str) -> bytes:
    """Página no lugar de um boleto cujo arquivo não pôde ser lido, para o
    impresso não 'pular' um boleto em silêncio."""
    buffer = io.BytesIO()
    c = rl_canvas.Canvas(buffer, pagesize=A4)
    w, h = A4_W, A4_H
    c.setFillColor(HexColor("#B91C1C"))
    c.setFont("Helvetica-Bold", 20)
    c.drawCentredString(w / 2, h / 2 + 60, "ARQUIVO DO BOLETO INDISPONÍVEL")
    c.setFillColor(HexColor("#0F172A"))
    c.setFont("Helvetica", 12)
    valor = f"R$ {float(row.get('valor') or 0):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    venc = str(row.get("data_vencimento") or "")
    try:
        venc = datetime.fromisoformat(venc).strftime("%d/%m/%Y")
    except ValueError:
        pass
    linhas = [
        f"Boleto #{row.get('id')} · {row.get('estabelecimento') or ''}",
        f"Fornecedor: {row.get('fornecedor') or '—'}",
        f"Valor: {valor}   ·   Vencimento: {venc}",
        f"Motivo: {motivo}",
    ]
    for i, linha in enumerate(linhas):
        c.drawCentredString(w / 2, h / 2 + 20 - i * 20, linha[:90])
    linha_dig = row.get("linha_digitavel")
    if linha_dig:
        c.setFont("Courier", 10)
        c.drawCentredString(w / 2, h / 2 - 80, linha_dig[:90])
    c.showPage()
    c.save()
    try:
        return buffer.getvalue()
    finally:
        buffer.close()


def _adicionar_pdf(writer: PdfWriter, dados: bytes) -> None:
    stream = io.BytesIO(dados)
    try:
        reader = PdfReader(stream)
        if reader.is_encrypted:
            reader.decrypt("")
        for page in reader.pages:
            writer.add_page(page)
    finally:
        stream.close()


def gerar_lote_impressao_arquivo(rows: list[dict]) -> str:
    """Gera o PDF do lote num arquivo temporário e devolve o caminho.

    Pensado para os 512 MB do Render: baixa e processa um boleto por vez,
    fecha cada stream, roda gc a cada arquivo e grava o resultado direto em
    disco (sem manter uma segunda cópia do PDF final em memória).
    Aceita arquivos PDF e imagens (JPG/PNG/WebP). Arquivos ausentes ou
    corrompidos viram uma página de aviso no lugar.
    """
    from . import db as _db

    if not rows:
        raise ValueError("Nenhum boleto encontrado para gerar o PDF.")

    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["estabelecimento"]].append(row)

    writer = PdfWriter()
    for estabelecimento, boletos in grouped.items():
        _adicionar_pdf(writer, _criar_pagina_divisoria(estabelecimento, qtd_boletos=len(boletos)))

        for row in boletos:
            dados = _db.get_boleto_arquivo(row["id"])
            try:
                if not dados:
                    _adicionar_pdf(writer, _pagina_aviso(row, "arquivo não encontrado no storage"))
                    continue
                tipo = _tipo_arquivo(dados)
                if tipo == "pdf":
                    _adicionar_pdf(writer, dados)
                elif tipo in ("jpg", "png", "webp"):
                    _adicionar_pdf(writer, _imagem_para_pdf(dados))
                else:
                    _adicionar_pdf(writer, _pagina_aviso(row, "formato de arquivo não suportado"))
            except Exception as exc:
                print(f"[lote] boleto #{row.get('id')}: {type(exc).__name__}: {exc}", flush=True)
                try:
                    _adicionar_pdf(writer, _pagina_aviso(row, "arquivo corrompido ou ilegível"))
                except Exception:
                    pass
            finally:
                del dados
                gc.collect()

    if hasattr(writer, "compress_identical_objects"):  # pypdf >= 5
        writer.compress_identical_objects(remove_identicals=True, remove_orphans=True)

    fd, caminho = tempfile.mkstemp(prefix="lote_", suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as f:
            writer.write(f)
    except Exception:
        os.unlink(caminho)
        raise
    finally:
        if hasattr(writer, "close"):
            writer.close()
        del writer
        gc.collect()
    return caminho


def gerar_pdf_agrupado_bytes(rows: list[dict]) -> bytes:
    """Compatibilidade: mesma geração, devolvendo bytes."""
    caminho = gerar_lote_impressao_arquivo(rows)
    try:
        return Path(caminho).read_bytes()
    finally:
        os.unlink(caminho)


def gerar_lote_impressao_bytes(rows: list[dict]) -> bytes:
    return gerar_pdf_agrupado_bytes(rows)


def gerar_pdf_urgentes_bytes(rows: list[dict]) -> bytes:
    return gerar_pdf_agrupado_bytes(rows)


def _scanner_filter(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img)
    # Reduz antes de processar: uma foto de 12 MP em float32 ocupa ~50 MB por
    # array e o filtro cria vários — isso derrubava o Render (512 MB).
    img.thumbnail((2000, 2000), Image.LANCZOS)
    gray = img.convert("L")
    gray = ImageOps.autocontrast(gray, cutoff=0.5)
    gray = gray.filter(ImageFilter.UnsharpMask(radius=1.2, percent=120, threshold=3))

    arr = np.array(gray, dtype=np.float32)
    pad = 16
    mean_img = Image.fromarray(arr.astype(np.uint8)).filter(ImageFilter.BoxBlur(pad))
    mean_arr = np.array(mean_img, dtype=np.float32)
    binary = np.where(arr >= mean_arr - 10, 255, 0).astype(np.uint8)
    del arr, mean_arr, mean_img
    result = Image.fromarray(binary, mode="L")
    del binary

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

    nome_limpo = nome_seguro(Path(original_name or "arquivo").stem)
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")

    if is_image:
        raw = Image.open(io.BytesIO(file_bytes))
        if raw.format == "JPEG":
            raw.draft("RGB", (2000, 2000))
        scanned = _scanner_filter(raw)
        raw.close()
        buf = io.BytesIO()
        scanned.save(buf, "PDF", resolution=200)
        scanned.close()
        nome = f"{timestamp}_{nome_limpo}_scan.pdf"
        try:
            return nome, "application/pdf", buf.getvalue()
        finally:
            buf.close()
            gc.collect()

    nome = f"{timestamp}_{nome_limpo}.pdf"
    return nome, "application/pdf", file_bytes


def nome_seguro(nome: str, max_len: int = 60) -> str:
    """Nome só com ASCII [A-Za-z0-9._-]. O Supabase Storage rejeita chaves com
    acentos, espaços e alguns símbolos (StorageApiError 'Invalid key')."""
    nome = unicodedata.normalize("NFKD", nome or "").encode("ascii", "ignore").decode()
    nome = "".join(c if (c.isalnum() or c in "._-") else "_" for c in nome)
    while "__" in nome:
        nome = nome.replace("__", "_")
    nome = nome.strip("._-")[:max_len]
    return nome or "boleto"


def chave_storage(estabelecimento_id: int | None, nome_arquivo: str) -> str:
    """Caminho único no bucket: <loja>/<AAAA-MM>/<uuid>_<nome>.pdf.
    O uuid garante que dois 'boleto.pdf' nunca colidam."""
    p = Path(nome_arquivo or "boleto.pdf")
    ext = p.suffix.lower() if p.suffix.lower() in (".pdf", ".png", ".jpg", ".jpeg") else ".pdf"
    pasta = f"{estabelecimento_id or 'geral'}/{datetime.now():%Y-%m}"
    return f"{pasta}/{uuid.uuid4().hex}_{nome_seguro(p.stem, 40)}{ext}"
