"""Gravação de arquivos de boleto (Supabase Storage, com fallback local p/ dev).

Usado pelo upload manual do gerente e pela importação automática de e-mails.
"""
from __future__ import annotations

import gc
from pathlib import Path

from . import db, pdf_service


def armazenar_boleto(
    conteudo: bytes,
    nome_original: str,
    content_type: str,
    estabelecimento_id: int | None,
) -> tuple[str, str, bytes]:
    """Converte (imagem → PDF com filtro scanner) e grava no Storage.

    Devolve (nome_arquivo, arquivo_url, bytes_pdf). Chave única e só ASCII:
    <loja>/<AAAA-MM>/<uuid>_<nome>.pdf — dois "boleto.pdf" nunca colidem.
    """
    nome, tipo, dados = pdf_service.processar_upload(conteudo, nome_original, content_type)
    caminho = pdf_service.chave_storage(estabelecimento_id, nome)

    if db.supabase:
        bucket = db.supabase.storage.from_(db.SUPABASE_BUCKET)
        ultimo_erro = None
        for _ in range(2):
            try:
                bucket.upload(path=caminho, file=dados,
                              file_options={"content-type": tipo, "upsert": "false"})
                ultimo_erro = None
                break
            except Exception as exc:
                ultimo_erro = exc
                print(f"[storage] StorageApiError ({caminho}): {exc!r}", flush=True)
                caminho = pdf_service.chave_storage(estabelecimento_id, nome)
        if ultimo_erro is not None:
            raise ultimo_erro
        url = bucket.get_public_url(caminho)
    else:
        dest = Path(db.BASE_DIR) / "uploads" / caminho
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(dados)
        url = f"/uploads/{caminho}"

    gc.collect()
    return nome, url, dados
