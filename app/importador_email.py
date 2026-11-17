"""Importação automática de boletos a partir do Gmail (IMAP + senha de app).

Fluxo por e-mail:
  1. Busca e-mails com anexo que ainda NÃO têm a etiqueta "FluxoPay".
  2. Para cada anexo PDF/JPG/PNG: lê o boleto (leitor_boleto).
  3. Código válido + valor + vencimento + loja identificada → salva direto.
     Faltou algo → vai para a Caixa de Entrada (revisão) com o arquivo já salvo.
     Código já cadastrado → "duplicado". Nota fiscal / PDF sem boleto → "ignorado".
  4. Aplica a etiqueta "FluxoPay" no e-mail (não marca como lido).

Configuração (variáveis de ambiente no Render):
  GMAIL_USER, GMAIL_APP_PASSWORD   — obrigatórias
  GMAIL_LABEL   (padrão "FluxoPay")
  GMAIL_PASTA   (padrão "INBOX")
  IMPORT_DIAS   (padrão 30)  — só olha e-mails dos últimos N dias
  IMPORT_MAX    (padrão 15)  — máximo de e-mails por execução (RAM/tempo)
"""
from __future__ import annotations

import email
import gc
import hashlib
import html
import imaplib
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime

from . import db, leitor_boleto, storage

GMAIL_USER = os.getenv("GMAIL_USER", "").strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
GMAIL_LABEL = os.getenv("GMAIL_LABEL", "FluxoPay").strip()
GMAIL_PASTA = os.getenv("GMAIL_PASTA", "INBOX").strip()
IMPORT_DIAS = int(os.getenv("IMPORT_DIAS", "30"))
IMPORT_MAX = int(os.getenv("IMPORT_MAX", "15"))

_EXT_OK = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
_MIN_IMG_BYTES = 20 * 1024        # imagens menores costumam ser logo/assinatura
_MAX_ANEXO = 8 * 1024 * 1024

_lock = threading.Lock()
ultima_execucao: dict = {}


def configurado() -> bool:
    return bool(GMAIL_USER and GMAIL_APP_PASSWORD)


# ── helpers de e-mail ─────────────────────────────────────────────────────────

def _decod(valor: str | None) -> str:
    if not valor:
        return ""
    try:
        return str(make_header(decode_header(valor)))
    except Exception:
        return valor


def _texto_corpo(msg: Message, limite: int = 6000) -> str:
    partes: list[str] = []
    for part in msg.walk():
        if part.get_content_maintype() != "text" or part.get_filename():
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            txt = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
        except Exception:
            continue
        if part.get_content_subtype() == "html":
            txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", txt, flags=re.S | re.I)
            txt = html.unescape(re.sub(r"<[^>]+>", " ", txt))
        partes.append(txt)
        if sum(map(len, partes)) > limite:
            break
    return re.sub(r"[ \t]+", " ", "\n".join(partes))[:limite]


def _anexos(msg: Message):
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        nome = _decod(part.get_filename())
        ctype = (part.get_content_type() or "").lower()
        ext = os.path.splitext(nome.lower())[1] if nome else ""
        mime = _EXT_OK.get(ext) or (ctype if ctype in _EXT_OK.values() else None)
        if not mime:
            continue
        dados = part.get_payload(decode=True) or b""
        if not dados or len(dados) > _MAX_ANEXO:
            continue
        if mime.startswith("image/") and len(dados) < _MIN_IMG_BYTES:
            continue
        yield (nome or f"anexo{ext or '.pdf'}"), mime, dados


def _parece_nota_fiscal(texto: str) -> bool:
    t = (texto or "").lower()
    return ("danfe" in t or "nota fiscal" in t or "nf-e" in t) and "linha digit" not in t


# ── processamento de um anexo ─────────────────────────────────────────────────

def processar_anexo(
    dados: bytes,
    mime: str,
    nome: str,
    *,
    message_id: str,
    remetente: str,
    remetente_nome: str,
    assunto: str,
    recebido_em: str,
    corpo: str,
    lojas: list[dict],
) -> dict:
    chave = f"{message_id}|{nome}|{hashlib.sha1(dados).hexdigest()[:16]}"
    if db.importacao_existe(chave):
        return {"status": "ja_processado"}

    leitura = leitor_boleto.ler_boleto(
        dados, mime, lojas,
        texto_extra=f"{assunto}\n{corpo}",
        fornecedor_padrao=remetente_nome or None,
    )
    base = dict(
        chave=chave, message_id=message_id, remetente=remetente, assunto=assunto[:300],
        recebido_em=recebido_em, arquivo_nome=nome[:200],
        estabelecimento_id=leitura.estabelecimento_id, fornecedor=leitura.fornecedor,
        valor=leitura.valor,
        data_vencimento=leitura.vencimento.isoformat() if leitura.vencimento else None,
        linha_digitavel=leitura.linha_digitavel,
        codigo_valido=int(leitura.codigo_valido), usou_ia=int(leitura.usou_ia),
        pagador_nome=leitura.pagador_nome, pagador_cnpj=leitura.pagador_cnpj,
    )

    # 1) não é boleto (nota fiscal, comprovante, etc.)
    if not leitura.codigo_valido:
        texto_pdf = leitor_boleto.texto_do_pdf(dados) if mime == "application/pdf" else ""
        nao_boleto = (not leitura.eh_boleto) or _parece_nota_fiscal(texto_pdf) or (
            texto_pdf.strip() and not leitor_boleto.gemini_disponivel()
            and not leitura.linha_digitavel and "boleto" not in texto_pdf.lower()
        )
        if nao_boleto:
            db.registrar_importacao(**base, status="ignorado", motivo="anexo não parece ser um boleto")
            return {"status": "ignorado"}

    # 2) já cadastrado
    if leitura.linha_digitavel:
        existente = db.boleto_por_linha(leitura.linha_digitavel)
        if existente:
            db.registrar_importacao(**base, status="duplicado", boleto_id=existente["id"],
                                    motivo=f"já cadastrado como boleto #{existente['id']}")
            return {"status": "duplicado"}

    # 3) grava o arquivo (vale tanto p/ salvar direto quanto p/ revisão)
    nome_arq, url, _pdf = storage.armazenar_boleto(dados, nome, mime, leitura.estabelecimento_id)
    del _pdf
    base["arquivo_url"] = url

    # 4) completo → salva direto
    if leitura.completa():
        boleto_id = db.salvar_boleto(
            leitura.estabelecimento_id, nome_arq, url, float(leitura.valor), leitura.vencimento,
            leitura.linha_digitavel, fornecedor=leitura.fornecedor,
            criado_por=f"e-mail · {remetente}"[:120],
        )
        db.registrar_importacao(**base, status="importado", boleto_id=boleto_id,
                                motivo="; ".join(leitura.observacoes) or None)
        return {"status": "importado", "boleto_id": boleto_id}

    # 5) incompleto → caixa de entrada
    db.registrar_importacao(**base, status="revisao",
                            motivo="; ".join(leitura.observacoes) or "dados incompletos")
    return {"status": "revisao"}


# ── execução ──────────────────────────────────────────────────────────────────

def _buscar_uids(imap: imaplib.IMAP4_SSL) -> list[bytes]:
    consulta = f'has:attachment -label:{GMAIL_LABEL} newer_than:{IMPORT_DIAS}d'
    try:
        typ, data = imap.uid("SEARCH", "X-GM-RAW", f'"{consulta}"')
        if typ == "OK":
            return (data[0] or b"").split()
    except imaplib.IMAP4.error:
        pass
    desde = (date.today() - timedelta(days=IMPORT_DIAS)).strftime("%d-%b-%Y")
    typ, data = imap.uid("SEARCH", None, "SINCE", desde)
    return (data[0] or b"").split() if typ == "OK" else []


def importar_emails(max_emails: int | None = None) -> dict:
    """Executa uma rodada de importação. Seguro para chamar em paralelo
    (uma rodada por vez)."""
    global ultima_execucao
    if not configurado():
        return {"erro": "Gmail não configurado (GMAIL_USER / GMAIL_APP_PASSWORD)."}
    if not _lock.acquire(blocking=False):
        return {"erro": "Já existe uma importação em andamento."}

    inicio = time.time()
    resumo = {"inicio": datetime.now().isoformat(timespec="seconds"), "emails": 0, "anexos": 0,
              "importado": 0, "revisao": 0, "duplicado": 0, "ignorado": 0, "ja_processado": 0,
              "erros": 0, "restantes": 0, "mensagens_erro": []}
    imap = None
    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
        imap.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        imap.select(f'"{GMAIL_PASTA}"', readonly=False)
        uids = _buscar_uids(imap)
        limite = max_emails or IMPORT_MAX
        resumo["restantes"] = max(0, len(uids) - limite)
        lojas = db.listar_estabelecimentos()

        for uid in uids[:limite]:
            try:
                typ, data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
                if typ != "OK" or not data or not isinstance(data[0], tuple):
                    continue
                msg = email.message_from_bytes(data[0][1])
                del data
                nome_rem, end_rem = parseaddr(_decod(msg.get("From")))
                assunto = _decod(msg.get("Subject"))
                message_id = (msg.get("Message-ID") or f"uid-{uid.decode()}").strip()
                try:
                    recebido = parsedate_to_datetime(msg.get("Date")).isoformat(timespec="seconds")
                except Exception:
                    recebido = datetime.now().isoformat(timespec="seconds")
                corpo = _texto_corpo(msg)
                resumo["emails"] += 1

                for nome, mime, dados in _anexos(msg):
                    resumo["anexos"] += 1
                    try:
                        r = processar_anexo(
                            dados, mime, nome, message_id=message_id, remetente=end_rem,
                            remetente_nome=nome_rem, assunto=assunto, recebido_em=recebido,
                            corpo=corpo, lojas=lojas,
                        )
                        resumo[r["status"]] = resumo.get(r["status"], 0) + 1
                    except Exception as exc:
                        resumo["erros"] += 1
                        resumo["mensagens_erro"].append(f"{assunto[:60]} / {nome}: {type(exc).__name__}")
                        print(f"[email] erro no anexo {nome!r}: {exc!r}", flush=True)
                    finally:
                        del dados
                        gc.collect()

                del msg
                try:
                    imap.uid("STORE", uid, "+X-GM-LABELS", f'("{GMAIL_LABEL}")')
                except Exception as exc:
                    print(f"[email] não consegui etiquetar: {exc!r}", flush=True)
            except Exception as exc:
                resumo["erros"] += 1
                resumo["mensagens_erro"].append(f"uid {uid!r}: {type(exc).__name__}: {exc}")
                print(f"[email] erro no e-mail uid={uid!r}: {exc!r}", flush=True)
    except imaplib.IMAP4.error as exc:
        resumo["erro"] = f"Falha ao entrar no Gmail: {exc}. Confira GMAIL_USER e a senha de app."
    except Exception as exc:
        resumo["erro"] = f"{type(exc).__name__}: {exc}"
    finally:
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                pass
        resumo["duracao_s"] = round(time.time() - inicio, 1)
        resumo["fim"] = datetime.now().isoformat(timespec="seconds")
        ultima_execucao = resumo
        _lock.release()
        gc.collect()
    print(f"[email] importação: {resumo}", flush=True)
    return resumo


def importar_em_segundo_plano() -> bool:
    """Dispara a importação numa thread (para não estourar timeout do HTTP)."""
    if _lock.locked():
        return False
    threading.Thread(target=importar_emails, daemon=True).start()
    return True


def em_andamento() -> bool:
    return _lock.locked()


def reler_importacao(imp: dict, lojas: list[dict]) -> str:
    """Lê de novo um item da Caixa de Entrada (ex.: depois de configurar o Gemini
    ou cadastrar CNPJs). Se ficar completo, salva o boleto direto."""
    dados = None
    url = imp.get("arquivo_url") or ""
    try:
        if url.startswith("/uploads/"):
            from pathlib import Path
            dados = (Path(db.BASE_DIR) / url.lstrip("/")).read_bytes()
        elif url.startswith("http"):
            import urllib.request
            with urllib.request.urlopen(url, timeout=30) as resp:
                dados = resp.read(12 * 1024 * 1024)
    except Exception as exc:
        print(f"[reler] #{imp['id']}: não baixou o arquivo: {exc!r}", flush=True)
    if not dados:
        return "sem_arquivo"
    leitura = leitor_boleto.ler_boleto(
        dados, "application/pdf" if dados[:4] == b"%PDF" else "image/jpeg", lojas,
        texto_extra=imp.get("assunto") or "",
    )
    del dados
    gc.collect()
    campos = dict(
        estabelecimento_id=leitura.estabelecimento_id or imp.get("estabelecimento_id"),
        fornecedor=leitura.fornecedor or imp.get("fornecedor"),
        valor=leitura.valor or imp.get("valor"),
        data_vencimento=leitura.vencimento.isoformat() if leitura.vencimento else imp.get("data_vencimento"),
        linha_digitavel=leitura.linha_digitavel or imp.get("linha_digitavel"),
        codigo_valido=int(leitura.codigo_valido), usou_ia=int(leitura.usou_ia),
        pagador_nome=leitura.pagador_nome or imp.get("pagador_nome"),
        pagador_cnpj=leitura.pagador_cnpj or imp.get("pagador_cnpj"),
        motivo="; ".join(leitura.observacoes) or None,
    )
    if leitura.linha_digitavel:
        existente = db.boleto_por_linha(leitura.linha_digitavel)
        if existente:
            db.atualizar_importacao(imp["id"], **campos, status="duplicado", boleto_id=existente["id"])
            return "duplicado"
    if leitura.completa():
        boleto_id = db.salvar_boleto(
            leitura.estabelecimento_id, imp.get("arquivo_nome") or "boleto.pdf", url,
            float(leitura.valor), leitura.vencimento, leitura.linha_digitavel,
            fornecedor=leitura.fornecedor, criado_por=f"e-mail · {imp.get('remetente') or ''}"[:120],
        )
        db.atualizar_importacao(imp["id"], **campos, status="importado", boleto_id=boleto_id)
        return "importado"
    db.atualizar_importacao(imp["id"], **campos)
    return "revisao"
