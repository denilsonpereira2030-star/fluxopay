"""Leitura automática de boletos (PDF, foto ou texto).

Estratégia em camadas, da mais barata/confiável para a mais cara:

1. Texto do PDF (pypdf) → procura a linha digitável / código de barras e
   valida os dígitos verificadores. Do código saem, sem IA, o VALOR e o
   VENCIMENTO (boleto bancário) — é matemática, não "achismo".
2. CNPJs no texto → identifica a loja pagadora (casando com o CNPJ
   cadastrado de cada loja) e o beneficiário.
3. Só quando falta algo (foto, PDF escaneado, fornecedor desconhecido) e
   existe GEMINI_API_KEY, chama o Gemini. O que o Gemini devolve também passa
   pela validação dos dígitos — se o código não fecha, não é aceito.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import unicodedata
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

# ── módulos 10 / 11 ───────────────────────────────────────────────────────────


def _mod10(num: str) -> int:
    soma, peso = 0, 2
    for d in reversed(num):
        p = int(d) * peso
        soma += p // 10 + p % 10
        peso = 1 if peso == 2 else 2
    resto = soma % 10
    return 0 if resto == 0 else 10 - resto


def _mod11_bancario(num: str) -> int:
    soma, peso = 0, 2
    for d in reversed(num):
        soma += int(d) * peso
        peso = 2 if peso == 9 else peso + 1
    dv = 11 - (soma % 11)
    return 1 if dv in (0, 10, 11) else dv


def _mod11_arrecadacao(num: str) -> int:
    soma, peso = 0, 2
    for d in reversed(num):
        soma += int(d) * peso
        peso = 2 if peso == 9 else peso + 1
    resto = soma % 11
    if resto in (0, 1):
        return 0
    if resto == 10:
        return 1
    return 11 - resto


# ── vencimento (fator) ────────────────────────────────────────────────────────

_BASE_ANTIGA = date(1997, 10, 7)
_BASE_NOVA = date(2025, 2, 22)  # fator reiniciou em 1000 nesta data (FEBRABAN)


def _fator_para_data(fator: int, hoje: date | None = None) -> date | None:
    if fator <= 0:
        return None
    hoje = hoje or date.today()
    candidatos = [_BASE_ANTIGA + timedelta(days=fator)]
    if fator >= 1000:
        candidatos.append(_BASE_NOVA + timedelta(days=fator - 1000))
    return min(candidatos, key=lambda d: abs((d - hoje).days))


# ── código de barras / linha digitável ────────────────────────────────────────

@dataclass
class Codigo:
    tipo: str                 # "bancario" | "arrecadacao"
    linha_digitavel: str      # formatada
    codigo_barras: str        # 44 dígitos
    valor: float | None
    vencimento: date | None
    banco: str | None = None


def _formatar_linha_bancaria(l: str) -> str:
    return f"{l[0:5]}.{l[5:10]} {l[10:15]}.{l[15:21]} {l[21:26]}.{l[26:32]} {l[32]} {l[33:47]}"


def _formatar_linha_arrecadacao(l: str) -> str:
    return " ".join(f"{l[i:i+11]}-{l[i+11]}" for i in range(0, 48, 12))


def _bancario_de_barras(cb: str) -> Codigo | None:
    if len(cb) != 44 or cb[0] == "8":
        return None
    if _mod11_bancario(cb[:4] + cb[5:]) != int(cb[4]):
        return None
    campo_livre = cb[19:44]
    c1 = cb[0:4] + campo_livre[0:5]
    c2 = campo_livre[5:15]
    c3 = campo_livre[15:25]
    linha = (c1 + str(_mod10(c1)) + c2 + str(_mod10(c2)) + c3 + str(_mod10(c3))
             + cb[4] + cb[5:19])
    fator, valor = int(cb[5:9]), int(cb[9:19]) / 100
    return Codigo("bancario", _formatar_linha_bancaria(linha), cb,
                  valor or None, _fator_para_data(fator), cb[0:3])


def _bancario_de_linha(l: str) -> Codigo | None:
    if len(l) != 47:
        return None
    for ini, fim in ((0, 9), (10, 20), (21, 31)):
        if _mod10(l[ini:fim]) != int(l[fim]):
            return None
    cb = l[0:4] + l[32] + l[33:47] + l[4:9] + l[10:20] + l[21:31]
    return _bancario_de_barras(cb)


def _arrecadacao_modulo(cb_ou_bloco: str):
    return _mod10 if cb_ou_bloco[2] in "67" else _mod11_arrecadacao


def _arrecadacao_de_barras(cb: str) -> Codigo | None:
    if len(cb) != 44 or cb[0] != "8" or cb[2] not in "6789":
        return None
    mod = _arrecadacao_modulo(cb)
    if mod(cb[:3] + cb[4:]) != int(cb[3]):
        return None
    blocos = [cb[i:i+11] for i in range(0, 44, 11)]
    linha = "".join(b + str(mod(b)) for b in blocos)
    valor = int(cb[4:15]) / 100 if cb[2] in "68" else None
    return Codigo("arrecadacao", _formatar_linha_arrecadacao(linha), cb, valor or None, None)


def _arrecadacao_de_linha(l: str) -> Codigo | None:
    if len(l) != 48 or l[0] != "8":
        return None
    mod = _arrecadacao_modulo(l)
    for i in range(0, 48, 12):
        if mod(l[i:i+11]) != int(l[i+11]):
            return None
    cb = "".join(l[i:i+11] for i in range(0, 48, 12))
    return _arrecadacao_de_barras(cb)


def decodificar(digitos: str) -> Codigo | None:
    """Recebe linha digitável (47/48) ou código de barras (44) e valida."""
    d = re.sub(r"\D", "", digitos or "")
    if len(d) == 47:
        return _bancario_de_linha(d)
    if len(d) == 48:
        return _arrecadacao_de_linha(d)
    if len(d) == 44:
        return _arrecadacao_de_barras(d) if d[0] == "8" else _bancario_de_barras(d)
    return None


_RE_CANDIDATO = re.compile(r"\d[\d.\s\-]{38,75}\d")


def procurar_codigo(texto: str) -> Codigo | None:
    """Acha o primeiro código válido (com DV conferido) dentro de um texto."""
    if not texto:
        return None
    for m in _RE_CANDIDATO.finditer(texto):
        bruto = re.sub(r"\D", "", m.group())
        # o trecho pode ter dígitos "grudados" antes/depois: testa janelas
        for tam in (47, 48, 44):
            for ini in range(0, max(0, len(bruto) - tam) + 1):
                cod = decodificar(bruto[ini:ini + tam])
                if cod:
                    return cod
    return None


# ── CNPJ ──────────────────────────────────────────────────────────────────────

_RE_CNPJ = re.compile(r"(?<!\d)\d{2}\.?\d{3}\.?\d{3}\s?/?\s?\d{4}\s?-?\s?\d{2}(?!\d)")


def cnpj_valido(c: str) -> bool:
    c = re.sub(r"\D", "", c or "")
    if len(c) != 14 or c == c[0] * 14:
        return False
    def dv(base: str, pesos: list[int]) -> str:
        r = sum(int(a) * b for a, b in zip(base, pesos)) % 11
        return "0" if r < 2 else str(11 - r)
    p1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    d1 = dv(c[:12], p1)
    d2 = dv(c[:12] + d1, [6] + p1)
    return c[12:] == d1 + d2


def procurar_cnpjs(texto: str) -> list[str]:
    vistos: list[str] = []
    for m in _RE_CNPJ.finditer(texto or ""):
        c = re.sub(r"\D", "", m.group())
        if cnpj_valido(c) and c not in vistos:
            vistos.append(c)
    return vistos


def formatar_cnpj(c: str) -> str:
    c = re.sub(r"\D", "", c or "")
    return f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}" if len(c) == 14 else c


# ── campos auxiliares do texto ────────────────────────────────────────────────

_RE_DATA = r"(\d{2})[/.\-](\d{2})[/.\-](\d{4})"
_RE_VENC = re.compile(r"venc\w*[^\d]{0,40}" + _RE_DATA, re.I)
_RE_BENEF = re.compile(
    r"(?:benefici[aá]rio|cedente)\s*(?:final)?\s*[:\-]?\s*\n?\s*([A-ZÀ-Ú0-9][^\n]{2,80})", re.I)


def _normalizar(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def procurar_vencimento(texto: str) -> date | None:
    m = _RE_VENC.search(texto or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def procurar_beneficiario(texto: str) -> str | None:
    for m in _RE_BENEF.finditer(texto or ""):
        nome = m.group(1)
        nome = _RE_CNPJ.sub("", nome)
        nome = re.split(r"\s{2,}|CNPJ|CPF|Ag[eê]ncia|C[oó]digo", nome, flags=re.I)[0]
        nome = nome.strip(" -:,")
        if len(nome) >= 3 and not re.fullmatch(r"[\d\s./-]+", nome):
            return nome[:120]
    return None


_RE_PAGADOR = re.compile(r"(?:pagador|sacado)\s*[:\-]?", re.I)


def procurar_pagador(texto: str) -> tuple[str | None, str | None]:
    """Devolve (nome, cnpj) do pagador/sacado impresso no boleto."""
    for m in _RE_PAGADOR.finditer(texto or ""):
        janela = texto[m.end(): m.end() + 260]
        cnpj = next((c for c in procurar_cnpjs(janela)), None)
        linhas = [l.strip() for l in janela.splitlines() if l.strip()]
        nome = None
        for l in linhas[:3]:
            l2 = _RE_CNPJ.sub("", l)
            l2 = re.split(r"\s{2,}|CNPJ|CPF|Endere|Rua |Av\.|Avenida", l2, flags=re.I)[0].strip(" -:,")
            if len(l2) >= 3 and re.search(r"[A-Za-zÀ-ú]{3,}", l2) and not re.match(r"(?i)avalista|benefici", l2):
                nome = l2[:120]
                break
        if nome or cnpj:
            return nome, cnpj
    return None, None


# ── extração de texto ─────────────────────────────────────────────────────────

def texto_do_pdf(dados: bytes, senhas: list[str] | None = None, max_chars: int = 20000) -> str:
    """Extrai texto do PDF. Tenta senhas (alguns fornecedores protegem o PDF
    com parte do CNPJ do cliente)."""
    from pypdf import PdfReader
    stream = io.BytesIO(dados)
    try:
        reader = PdfReader(stream)
        if reader.is_encrypted:
            ok = False
            for s in [""] + list(senhas or []):
                try:
                    if reader.decrypt(s):
                        ok = True
                        break
                except Exception:
                    continue
            if not ok:
                return ""
        partes: list[str] = []
        total = 0
        for page in reader.pages[:6]:
            t = page.extract_text() or ""
            partes.append(t)
            total += len(t)
            if total > max_chars:
                break
        return "\n".join(partes)
    except Exception:
        return ""
    finally:
        stream.close()


# ── Gemini (opcional) ─────────────────────────────────────────────────────────

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
# Tenta na ordem; se um modelo for desativado pelo Google (404), usa o próximo.
_MODELOS = [m.strip() for m in os.getenv(
    "GEMINI_MODEL", "gemini-flash-latest,gemini-2.5-flash,gemini-2.0-flash").split(",") if m.strip()]
_modelo_ok: str | None = None

_PROMPT = (
    "Você é um leitor de boletos brasileiros. O arquivo é um boleto (PDF ou FOTO tirada "
    "com celular — pode estar torta, com sombra ou dobrada). Extraia os campos e responda "
    "SOMENTE o JSON pedido.\n"
    "- linha_digitavel: só os dígitos (47 para boleto bancário, 48 para contas de consumo/"
    "tributos que começam com 8), exatamente como impressos. Se não houver linha digitável "
    "legível, use os 44 dígitos do código de barras escritos embaixo dele. NÃO invente "
    "dígitos: se algum estiver ilegível, deixe o campo vazio.\n"
    "- valor: 'Valor do documento' / 'Valor cobrado' em reais, com ponto decimal.\n"
    "- vencimento: data de vencimento no formato AAAA-MM-DD.\n"
    "- beneficiario_*: quem RECEBE o dinheiro (cedente/fornecedor).\n"
    "- pagador_*: quem PAGA (sacado/cliente). Não confunda com o beneficiário nem com o "
    "'sacador/avalista'.\n"
    "- CNPJ/CPF só com dígitos.\n"
    "- eh_boleto=false se o arquivo não for um boleto (nota fiscal, comprovante, etc.)."
)

_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "eh_boleto": {"type": "BOOLEAN"},
        "linha_digitavel": {"type": "STRING"},
        "valor": {"type": "NUMBER"},
        "vencimento": {"type": "STRING"},
        "beneficiario_nome": {"type": "STRING"},
        "beneficiario_cnpj": {"type": "STRING"},
        "pagador_nome": {"type": "STRING"},
        "pagador_cnpj": {"type": "STRING"},
    },
    "required": ["eh_boleto"],
}


def gemini_disponivel() -> bool:
    return bool(GEMINI_API_KEY)


def _preparar_imagem(dados: bytes) -> tuple[bytes, str]:
    """Corrige rotação (EXIF), reduz para no máx. 2400px e recomprime em JPEG.
    Melhora a leitura, economiza cota do Gemini e memória no Render."""
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(dados)) as img:
        if img.format == "JPEG":
            img.draft("RGB", (2400, 2400))
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((2400, 2400))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=88, optimize=True)
        return out.getvalue(), "image/jpeg"


def _chamar_gemini(modelo: str, corpo: bytes, timeout: int) -> dict:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"
    req = urllib.request.Request(
        url, data=corpo,
        headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        r = json.loads(resp.read().decode())
    texto = r["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(texto)


def ler_com_gemini(dados: bytes, mime: str, timeout: int = 60) -> dict | None:
    global _modelo_ok
    if not GEMINI_API_KEY or len(dados) > 15 * 1024 * 1024:
        return None
    if mime.startswith("image/"):
        try:
            dados, mime = _preparar_imagem(dados)
        except Exception as exc:
            print(f"[gemini] imagem inválida: {exc!r}", flush=True)
            return None
    corpo = json.dumps({
        "contents": [{"parts": [
            {"text": _PROMPT},
            {"inline_data": {"mime_type": mime, "data": base64.b64encode(dados).decode()}},
        ]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": _SCHEMA,
        },
    }).encode()
    modelos = ([_modelo_ok] if _modelo_ok else []) + [m for m in _MODELOS if m != _modelo_ok]
    try:
        for modelo in modelos:
            for tentativa in range(2):
                try:
                    r = _chamar_gemini(modelo, corpo, timeout)
                    _modelo_ok = modelo
                    return r
                except urllib.error.HTTPError as exc:
                    detalhe = exc.read()[:300].decode(errors="ignore")
                    print(f"[gemini] {modelo} HTTP {exc.code}: {detalhe}", flush=True)
                    if exc.code in (404, 400) and "not found" in detalhe.lower():
                        break                 # modelo não existe → próximo
                    if exc.code in (429, 500, 503) and tentativa == 0:
                        import time
                        time.sleep(4)         # limite/instabilidade → tenta de novo
                        continue
                    if exc.code in (401, 403):
                        return None           # chave inválida: não adianta insistir
                    break
                except Exception as exc:
                    print(f"[gemini] {modelo} falha: {type(exc).__name__}: {str(exc)[:200]}", flush=True)
                    break
        return None
    finally:
        del corpo


# ── resultado consolidado ─────────────────────────────────────────────────────

@dataclass
class Leitura:
    eh_boleto: bool = True
    linha_digitavel: str | None = None
    codigo_valido: bool = False
    valor: float | None = None
    vencimento: date | None = None
    fornecedor: str | None = None
    cnpj_beneficiario: str | None = None
    pagador_nome: str | None = None
    pagador_cnpj: str | None = None
    cnpjs_encontrados: list[str] = field(default_factory=list)
    estabelecimento_id: int | None = None
    loja_por: str | None = None   # "cnpj" | "nome do pagador" | "nome no texto"
    usou_ia: bool = False
    observacoes: list[str] = field(default_factory=list)

    def completa(self) -> bool:
        # Só salva direto quando a loja veio do CNPJ ou do nome do pagador;
        # "nome solto no texto" é fraco demais → vai para revisão.
        return bool(self.codigo_valido and self.valor and self.vencimento
                    and self.estabelecimento_id and self.loja_por in ("cnpj", "nome do pagador"))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["vencimento"] = self.vencimento.isoformat() if self.vencimento else None
        return d


_STOP = {"ltda", "me", "epp", "eireli", "sa", "s", "a", "de", "da", "do", "das", "dos", "e",
         "comercio", "com", "servicos", "auto", "posto", "loja", "filial", "matriz", "cia"}


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", _normalizar(s)) if len(t) >= 2 and t not in _STOP}


def _identificar_loja(leitura: Leitura, texto: str, lojas: list[dict]) -> None:
    """Ordem: CNPJ do pagador → qualquer CNPJ de loja no boleto →
    nome do pagador parecido com o nome da loja → nome da loja no texto."""
    por_cnpj = {re.sub(r"\D", "", l.get("cnpj") or ""): l["id"] for l in lojas if l.get("cnpj")}
    if leitura.pagador_cnpj and leitura.pagador_cnpj in por_cnpj:
        leitura.estabelecimento_id, leitura.loja_por = por_cnpj[leitura.pagador_cnpj], "cnpj"
        return
    for c in leitura.cnpjs_encontrados:
        if c in por_cnpj and c != leitura.cnpj_beneficiario:
            leitura.estabelecimento_id, leitura.loja_por = por_cnpj[c], "cnpj"
            return
    if leitura.pagador_nome:
        tp = _tokens(leitura.pagador_nome)
        notas = []
        for l in lojas:
            tl = _tokens(l.get("nome") or "")
            if tl:
                notas.append((len(tp & tl) / len(tl), l["id"]))
        notas.sort(reverse=True)
        if notas and notas[0][0] >= 0.5 and (len(notas) == 1 or notas[1][0] < notas[0][0]):
            leitura.estabelecimento_id, leitura.loja_por = notas[0][1], "nome do pagador"
            return
    t = _normalizar(texto)
    achadas = [l["id"] for l in lojas if l.get("nome") and len(l["nome"]) >= 4 and _normalizar(l["nome"]) in t]
    if len(achadas) == 1:
        leitura.estabelecimento_id, leitura.loja_por = achadas[0], "nome no texto"


def _aplicar_ia(leitura: Leitura, ia: dict, cod: "Codigo | None") -> "Codigo | None":
    leitura.usou_ia = True
    if ia.get("eh_boleto") is False and not cod:
        leitura.eh_boleto = False
    if not cod and ia.get("linha_digitavel"):
        cod_ia = decodificar(ia["linha_digitavel"])
        if cod_ia:
            cod = cod_ia
            leitura.linha_digitavel, leitura.codigo_valido = cod.linha_digitavel, True
            leitura.valor = cod.valor or leitura.valor
            leitura.vencimento = cod.vencimento or leitura.vencimento
        else:
            leitura.linha_digitavel = re.sub(r"\D", "", ia["linha_digitavel"]) or None
            leitura.observacoes.append("código lido pela IA não passou na conferência dos dígitos — confira")
    if not leitura.valor and ia.get("valor"):
        try:
            leitura.valor = round(float(ia["valor"]), 2) or None
        except (TypeError, ValueError):
            pass
    if not leitura.vencimento and ia.get("vencimento"):
        try:
            leitura.vencimento = date.fromisoformat(str(ia["vencimento"])[:10])
        except ValueError:
            pass
    if ia.get("beneficiario_nome"):  # IA lê o nome do beneficiário melhor que a regex
        leitura.fornecedor = str(ia["beneficiario_nome"]).strip()[:120]
    bc = re.sub(r"\D", "", ia.get("beneficiario_cnpj") or "")
    if cnpj_valido(bc):
        leitura.cnpj_beneficiario = bc
    pc = re.sub(r"\D", "", ia.get("pagador_cnpj") or "")
    if cnpj_valido(pc) and pc != leitura.cnpj_beneficiario:
        leitura.pagador_cnpj = leitura.pagador_cnpj or pc
    if ia.get("pagador_nome") and not leitura.pagador_nome:
        leitura.pagador_nome = str(ia["pagador_nome"]).strip()[:120]
    for c in (bc, pc):
        if cnpj_valido(c) and c not in leitura.cnpjs_encontrados:
            leitura.cnpjs_encontrados.append(c)
    return cod


def ler_boleto(
    dados: bytes,
    mime: str,
    lojas: list[dict],
    texto_extra: str = "",
    fornecedor_padrao: str | None = None,
) -> Leitura:
    """Lê um anexo (PDF ou imagem) e devolve o que foi possível extrair.

    `lojas`: [{"id", "nome", "cnpj"}]. `texto_extra`: assunto/corpo do e-mail.
    """
    leitura = Leitura()
    senhas = []
    for l in lojas:
        c = re.sub(r"\D", "", l.get("cnpj") or "")
        if c:
            senhas += [c[:5], c[:8], c[:4], c]
    eh_pdf = mime == "application/pdf" or dados[:4] == b"%PDF"
    eh_imagem = not eh_pdf

    texto = texto_do_pdf(dados, senhas) if eh_pdf else ""
    tem_texto = len(re.sub(r"\s", "", texto)) > 80
    texto_total = f"{texto}\n{texto_extra or ''}"

    cod = procurar_codigo(texto) or procurar_codigo(texto_extra)
    leitura.cnpjs_encontrados = procurar_cnpjs(texto_total)
    leitura.fornecedor = procurar_beneficiario(texto)
    leitura.pagador_nome, leitura.pagador_cnpj = procurar_pagador(texto)
    benef_cnpjs = procurar_cnpjs(texto[: texto.lower().find("pagador")] if "pagador" in texto.lower() else "")
    if benef_cnpjs and benef_cnpjs[0] != leitura.pagador_cnpj:
        leitura.cnpj_beneficiario = benef_cnpjs[0]
    if cod:
        leitura.linha_digitavel, leitura.codigo_valido = cod.linha_digitavel, True
        leitura.valor, leitura.vencimento = cod.valor, cod.vencimento
    if not leitura.vencimento:
        leitura.vencimento = procurar_vencimento(texto_total)

    _identificar_loja(leitura, texto_total, lojas)

    # Gemini entra para foto/escaneado SEMPRE, e para PDF quando falta qualquer coisa
    falta_algo = (not cod) or (not leitura.vencimento) or (not leitura.valor) \
        or (not leitura.fornecedor) or (not leitura.estabelecimento_id) or (not leitura.pagador_nome)
    if gemini_disponivel() and (eh_imagem or not tem_texto or falta_algo):
        mime_ia = "application/pdf" if eh_pdf else (mime if mime.startswith("image/") else "image/jpeg")
        ia = ler_com_gemini(dados, mime_ia)
        if ia:
            cod = _aplicar_ia(leitura, ia, cod)
            if not leitura.estabelecimento_id or leitura.loja_por == "nome no texto":
                leitura.estabelecimento_id = None
                _identificar_loja(leitura, texto_total, lojas)
        else:
            leitura.observacoes.append("não foi possível consultar a IA agora (tente 'Ler de novo')")
    elif (eh_imagem or not tem_texto) and not cod and not gemini_disponivel():
        leitura.observacoes.append("foto/PDF escaneado: configure a chave do Gemini para ler automaticamente")

    if not leitura.fornecedor and fornecedor_padrao:
        leitura.fornecedor = fornecedor_padrao[:120]

    if cod and cod.tipo == "arrecadacao" and not leitura.vencimento:
        leitura.observacoes.append("conta de consumo/tributo: vencimento não vem no código")
    if leitura.valor is None and cod:
        leitura.observacoes.append("boleto sem valor no código (valor livre)")
    if not leitura.estabelecimento_id:
        quem = leitura.pagador_nome or (formatar_cnpj(leitura.pagador_cnpj) if leitura.pagador_cnpj else None)
        leitura.observacoes.append(f"loja não identificada{f' (pagador: {quem})' if quem else ''}")
    elif leitura.loja_por != "cnpj":
        leitura.observacoes.append(f"loja identificada pelo {leitura.loja_por} — confira")
    return leitura
