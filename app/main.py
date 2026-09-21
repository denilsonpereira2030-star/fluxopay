from __future__ import annotations

import gc
import io
import os
import uuid
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, pdf_service

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Gestão de Contas a Pagar", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

db.init_db()


# ── session helpers ──────────────────────────────────────────────────────────

_sessions: dict[str, dict] = {}


def _make_token() -> str:
    import secrets
    return secrets.token_hex(32)


def _get_user(request: Request) -> dict | None:
    token = request.cookies.get("session")
    if not token:
        return None
    return _sessions.get(token)


def _require_user(request: Request) -> dict:
    user = _get_user(request)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def _require_financeiro(request: Request) -> dict:
    user = _require_user(request)
    if user["perfil"] != "financeiro":
        raise HTTPException(status_code=403)
    return user


# ── template context helpers ─────────────────────────────────────────────────

def _tpl(request: Request, template: str, user: dict | None = None, status_code: int = 200, **kwargs):
    ctx = {"request": request, "user": user, **kwargs}
    try:
        return templates.TemplateResponse(request, template, ctx, status_code=status_code)
    except TypeError:
        return templates.TemplateResponse(template, ctx, status_code=status_code)


def _format_currency(valor: float) -> str:
    return f"R$ {float(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _format_date_br(d: str | date) -> str:
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return d.strftime("%d/%m/%Y")


templates.env.filters["currency"] = _format_currency
templates.env.filters["date_br"] = _format_date_br


# ── AUTH ─────────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _get_user(request):
        return RedirectResponse("/", status_code=303)
    return _tpl(request, "login.html")


@app.post("/login")
async def login_post(
    request: Request,
    login: str = Form(...),
    senha: str = Form(...),
    remember: str = Form(""),
):
    user = db.authenticate_user(login.strip(), senha)
    if not user:
        return _tpl(request, "login.html", error="Usuário ou senha incorretos.", status_code=401)
    token = _make_token()
    _sessions[token] = user
    response = RedirectResponse("/", status_code=303)
    # "Manter conectado": 30 dias; sessão normal: 24 horas
    max_age = 2592000 if remember == "1" else 86400
    response.set_cookie("session", token, httponly=True, samesite="lax", max_age=max_age)
    return response


@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get("session")
    if token:
        _sessions.pop(token, None)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("session")
    return response


# ── ROOT ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user["perfil"] == "gerente":
        return RedirectResponse("/gerente", status_code=303)
    return RedirectResponse("/financeiro", status_code=303)


# ── GERENTE VIEWS ─────────────────────────────────────────────────────────────

@app.get("/gerente", response_class=HTMLResponse)
async def gerente_novo(request: Request):
    user = _require_user(request)
    if user["perfil"] != "gerente":
        return RedirectResponse("/financeiro", status_code=303)
    est_nome = db.get_estabelecimento_nome(user["estabelecimento_id"])
    hoje = date.today()
    return _tpl(request, "gerente_novo.html", user=user, est_nome=est_nome, hoje=hoje)


@app.post("/gerente/boleto", response_class=HTMLResponse)
def gerente_salvar_boleto(
    request: Request,
    valor: float = Form(...),
    data_vencimento: str = Form(...),
    arquivo: UploadFile = File(None),
    confirmar_duplicata: str = Form(""),
):
    user = _require_user(request)
    if user["perfil"] != "gerente":
        raise HTTPException(403)

    vencimento = date.fromisoformat(data_vencimento)
    est_nome = db.get_estabelecimento_nome(user["estabelecimento_id"])

    if not confirmar_duplicata:
        dup = db.verificar_duplicata(user["estabelecimento_id"], valor, vencimento)
        if dup:
            return _tpl(
                request, "gerente_novo.html", user=user, est_nome=est_nome,
                hoje=date.today(),
                duplicata_aviso=dup,
                dup_valor=valor,
                dup_vencimento=data_vencimento,
                status_code=200,
            )

    conteudo = arquivo.file.read() if arquivo and arquivo.filename else None
    if not conteudo:
        return _tpl(request, "gerente_novo.html", user=user, est_nome=est_nome, hoje=date.today(), error="Selecione um arquivo antes de salvar.", status_code=422)

    _MAX_UPLOAD = 5 * 1024 * 1024  # 5 MB
    if len(conteudo) > _MAX_UPLOAD:
        return _tpl(
            request, "gerente_novo.html", user=user, est_nome=est_nome,
            hoje=date.today(),
            error=f"Arquivo muito grande ({len(conteudo) // 1024} KB). Limite máximo: 5 MB.",
            status_code=413,
        )

    print(f"[upload] arquivo={arquivo.filename!r} tamanho={len(conteudo)} bytes tipo={arquivo.content_type!r}", flush=True)

    try:
        nome, tipo, dados = pdf_service.processar_upload(conteudo, arquivo.filename, arquivo.content_type or "application/octet-stream")
        del conteudo

        if db.supabase:
            caminho = f"{user['estabelecimento_id']}/{uuid.uuid4()}_{nome}"
            db.supabase.storage.from_(db.SUPABASE_BUCKET).upload(
                path=caminho,
                file=dados,
                file_options={"content-type": tipo},
            )
            arquivo_url = db.supabase.storage.from_(db.SUPABASE_BUCKET).get_public_url(caminho)
        else:
            # fallback local: salva em disco para dev
            dest = Path(db.BASE_DIR) / "uploads" / nome
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(dados)
            arquivo_url = f"/uploads/{nome}"

        del dados
        gc.collect()

        db.salvar_boleto(user["estabelecimento_id"], nome, arquivo_url, valor, vencimento)
    except Exception as exc:
        print(f"ERRO FATAL NO UPLOAD: {str(exc)}", flush=True)
        return _tpl(
            request, "gerente_novo.html", user=user, est_nome=est_nome,
            hoje=date.today(),
            error=f"Erro ao salvar o boleto. Tente novamente. ({type(exc).__name__})",
            status_code=500,
        )

    return RedirectResponse("/gerente/contas?ok=1", status_code=303)


@app.get("/gerente/contas", response_class=HTMLResponse)
async def gerente_contas(request: Request, ok: str = ""):
    user = _require_user(request)
    if user["perfil"] != "gerente":
        return RedirectResponse("/financeiro", status_code=303)
    rows = db.listar_boletos(estabelecimento_id=user["estabelecimento_id"])
    est_nome = db.get_estabelecimento_nome(user["estabelecimento_id"])
    return _tpl(request, "gerente_contas.html", user=user, est_nome=est_nome, boletos=rows, success=bool(ok), hoje=date.today().isoformat())


@app.get("/gerente/boleto/{boleto_id}/editar", response_class=HTMLResponse)
async def gerente_editar_form(boleto_id: int, request: Request):
    user = _require_user(request)
    boleto = db.get_boleto_by_id(boleto_id)
    if not boleto or boleto["estabelecimento_id"] != user["estabelecimento_id"]:
        raise HTTPException(403)
    est_nome = db.get_estabelecimento_nome(user["estabelecimento_id"])
    return _tpl(request, "gerente_editar.html", user=user, boleto=boleto, est_nome=est_nome)


@app.post("/gerente/boleto/{boleto_id}/editar")
async def gerente_editar_post(boleto_id: int, request: Request, valor: float = Form(...), data_vencimento: str = Form(...)):
    user = _require_user(request)
    boleto = db.get_boleto_by_id(boleto_id)
    if not boleto or boleto["estabelecimento_id"] != user["estabelecimento_id"] or boleto["status"] != "Pendente":
        raise HTTPException(403)
    db.atualizar_boleto(boleto_id, valor=valor, data_vencimento=date.fromisoformat(data_vencimento))
    return RedirectResponse("/gerente/contas?ok=1", status_code=303)


@app.post("/gerente/boleto/{boleto_id}/excluir")
async def gerente_excluir(boleto_id: int, request: Request):
    user = _require_user(request)
    boleto = db.get_boleto_by_id(boleto_id)
    if not boleto or boleto["estabelecimento_id"] != user["estabelecimento_id"] or boleto["status"] != "Pendente":
        raise HTTPException(403)
    db.excluir_boleto(boleto_id)
    return RedirectResponse("/gerente/contas", status_code=303)


# ── FINANCEIRO VIEWS ──────────────────────────────────────────────────────────

@app.get("/financeiro", response_class=HTMLResponse)
async def financeiro_dashboard(request: Request):
    user = _get_user(request)
    if not user or user["perfil"] != "financeiro":
        return RedirectResponse("/login", status_code=303)
    stats = db.get_dashboard_stats()
    urgent_rows = db.listar_boletos_urgentes_hoje()
    return _tpl(request, "financeiro_dashboard.html", user=user, stats=stats, urgent_rows=urgent_rows)


@app.get("/financeiro/lote", response_class=HTMLResponse)
async def financeiro_lote(request: Request):
    user = _get_user(request)
    if not user or user["perfil"] != "financeiro":
        return RedirectResponse("/login", status_code=303)
    lote_inicio, lote_fim = db.calcular_lote_atual()
    rows = db.listar_boletos_do_lote()
    return _tpl(request, "financeiro_lote.html", user=user, boletos=rows, lote_inicio=lote_inicio, lote_fim=lote_fim)


@app.get("/financeiro/historico", response_class=HTMLResponse)
async def financeiro_historico(
    request: Request,
    status: str = "",
    data_inicio: str = "",
    data_fim: str = "",
):
    user = _get_user(request)
    if not user or user["perfil"] != "financeiro":
        return RedirectResponse("/login", status_code=303)

    di = date.fromisoformat(data_inicio) if data_inicio else None
    df = date.fromisoformat(data_fim) if data_fim else None

    rows = db.listar_boletos(
        status=status if status and status != "Todos" else None,
        data_inicio=di,
        data_fim=df,
    )
    return _tpl(request, "financeiro_historico.html", user=user, boletos=rows, filtro_status=status, filtro_inicio=data_inicio, filtro_fim=data_fim, statuses=db.STATUSES)


@app.post("/financeiro/boleto/{boleto_id}/status")
async def financeiro_status(boleto_id: int, request: Request, status: str = Form(...), origem: str = Form("")):
    user = _get_user(request)
    if not user or user["perfil"] != "financeiro":
        raise HTTPException(403)
    if status not in db.STATUSES:
        raise HTTPException(400)
    db.atualizar_boleto(boleto_id, status=status)
    redirect = origem if origem in ("/financeiro", "/financeiro/lote", "/financeiro/historico") else "/financeiro"
    return RedirectResponse(redirect, status_code=303)


@app.post("/financeiro/boleto/{boleto_id}/editar")
async def financeiro_editar(
    boleto_id: int,
    request: Request,
    valor: float = Form(...),
    data_vencimento: str = Form(...),
    status: str = Form(...),
    origem: str = Form(""),
):
    user = _get_user(request)
    if not user or user["perfil"] != "financeiro":
        raise HTTPException(403)
    db.atualizar_boleto(boleto_id, valor=valor, data_vencimento=date.fromisoformat(data_vencimento), status=status)
    redirect = origem if origem in ("/financeiro", "/financeiro/lote", "/financeiro/historico") else "/financeiro/historico"
    return RedirectResponse(redirect, status_code=303)


@app.post("/financeiro/boleto/{boleto_id}/excluir")
async def financeiro_excluir(boleto_id: int, request: Request, origem: str = Form("")):
    user = _get_user(request)
    if not user or user["perfil"] != "financeiro":
        raise HTTPException(403)
    db.excluir_boleto(boleto_id)
    redirect = origem if origem in ("/financeiro", "/financeiro/lote", "/financeiro/historico") else "/financeiro"
    return RedirectResponse(redirect, status_code=303)


# ── PDF DOWNLOADS ─────────────────────────────────────────────────────────────

@app.get("/financeiro/pdf/lote")
async def download_lote(request: Request, marcar_em_lote: str = "1"):
    user = _get_user(request)
    if not user or user["perfil"] != "financeiro":
        raise HTTPException(403)
    rows = db.listar_boletos_do_lote()
    if not rows:
        raise HTTPException(404, "Nenhum boleto no lote atual.")
    if marcar_em_lote == "1":
        db.marcar_lote_em_lote([r["id"] for r in rows])
    pdf_bytes = pdf_service.gerar_lote_impressao_bytes(rows)
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": "inline; filename=lote_impressao.pdf"},
    )


@app.get("/financeiro/pdf/lote/pedro")
async def download_lote_pedro(request: Request, marcar_em_lote: str = "1"):
    """Atalho rápido: gera o lote da semana atual (mesmo que /pdf/lote)."""
    return await download_lote(request, marcar_em_lote=marcar_em_lote)


@app.get("/financeiro/pdf/urgentes")
async def download_urgentes(request: Request):
    user = _get_user(request)
    if not user or user["perfil"] != "financeiro":
        raise HTTPException(403)
    rows = db.listar_boletos_urgentes_hoje()
    if not rows:
        raise HTTPException(404, "Nenhum boleto urgente hoje.")
    pdf_bytes = pdf_service.gerar_pdf_urgentes_bytes(rows)
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": "inline; filename=boletos_urgentes_hoje.pdf"},
    )


# ── ARQUIVO DE BOLETO (visualização inline) ───────────────────────────────────

@app.get("/financeiro/boleto/{boleto_id}/arquivo")
async def ver_arquivo_financeiro(boleto_id: int, request: Request):
    user = _get_user(request)
    if not user or user["perfil"] != "financeiro":
        raise HTTPException(403)
    boleto = db.get_boleto_by_id(boleto_id)
    if not boleto or not boleto.get("arquivo_url"):
        raise HTTPException(404, "Arquivo não encontrado.")
    return RedirectResponse(url=boleto["arquivo_url"], status_code=302)


@app.get("/gerente/boleto/{boleto_id}/arquivo")
async def ver_arquivo_gerente(boleto_id: int, request: Request):
    user = _require_user(request)
    boleto = db.get_boleto_by_id(boleto_id)
    if not boleto or boleto["estabelecimento_id"] != user["estabelecimento_id"]:
        raise HTTPException(403)
    if not boleto.get("arquivo_url"):
        raise HTTPException(404, "Arquivo não encontrado.")
    return RedirectResponse(url=boleto["arquivo_url"], status_code=302)


# ── BOLETOS POR ESTABELECIMENTO ───────────────────────────────────────────────

_MESES = [
    (1, "Janeiro"), (2, "Fevereiro"), (3, "Março"), (4, "Abril"),
    (5, "Maio"), (6, "Junho"), (7, "Julho"), (8, "Agosto"),
    (9, "Setembro"), (10, "Outubro"), (11, "Novembro"), (12, "Dezembro"),
]


def _anos_disponiveis() -> list[int]:
    hoje = date.today()
    return list(range(hoje.year - 2, hoje.year + 2))


@app.get("/financeiro/boletos", response_class=HTMLResponse)
async def financeiro_boletos(
    request: Request,
    mes: str = "",
    ano: str = "",
):
    user = _get_user(request)
    if not user or user["perfil"] != "financeiro":
        return RedirectResponse("/login", status_code=303)
    mes_sel = int(mes) if mes.isdigit() else None
    ano_sel = int(ano) if ano.isdigit() else None
    grupos = db.listar_boletos_por_estabelecimento(ano=ano_sel, mes=mes_sel)
    total_geral_qtd   = sum(g["qtd"]   for g in grupos.values())
    total_geral_valor = sum(g["total"] for g in grupos.values())
    return _tpl(
        request, "financeiro_boletos.html", user=user,
        grupos=grupos, meses=_MESES, anos=_anos_disponiveis(),
        mes_sel=mes_sel, ano_sel=ano_sel,
        total_geral_qtd=total_geral_qtd, total_geral_valor=total_geral_valor,
        hoje=date.today().isoformat(),
    )


@app.get("/gerente/boletos", response_class=HTMLResponse)
async def gerente_boletos(
    request: Request,
    mes: str = "",
    ano: str = "",
):
    user = _require_user(request)
    if user["perfil"] != "gerente":
        return RedirectResponse("/financeiro", status_code=303)
    est_nome = db.get_estabelecimento_nome(user["estabelecimento_id"])
    mes_sel = int(mes) if mes.isdigit() else None
    ano_sel = int(ano) if ano.isdigit() else None
    grupos = db.listar_boletos_por_estabelecimento(ano=ano_sel, mes=mes_sel)
    return _tpl(
        request, "gerente_boletos.html", user=user, est_nome=est_nome,
        grupos=grupos, meses=_MESES, anos=_anos_disponiveis(),
        mes_sel=mes_sel, ano_sel=ano_sel,
        hoje=date.today().isoformat(),
    )
