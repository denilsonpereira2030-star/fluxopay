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


def _require_dono(request: Request) -> dict:
    user = _require_user(request)
    if user["perfil"] != "dono":
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
    perfil = user["perfil"]
    if perfil == "gerente":
        dest = "/gerente"
    elif perfil == "dono":
        dest = "/dono"
    else:
        dest = "/financeiro"
    response = RedirectResponse(dest, status_code=303)
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
    if user["perfil"] == "dono":
        return RedirectResponse("/dono", status_code=303)
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

    _MAX_UPLOAD = 3 * 1024 * 1024  # 3 MB
    if len(conteudo) > _MAX_UPLOAD:
        return _tpl(
            request, "gerente_novo.html", user=user, est_nome=est_nome,
            hoje=date.today(),
            error=f"Arquivo muito grande ({len(conteudo) // 1024} KB). O limite máximo é 3MB.",
            status_code=413,
        )

    print(f"[upload] arquivo={arquivo.filename!r} tamanho={len(conteudo)} bytes tipo={arquivo.content_type!r}", flush=True)

    try:
        nome, tipo, dados = pdf_service.processar_upload(conteudo, arquivo.filename, arquivo.content_type or "application/octet-stream")
        del conteudo

        # Tenta extrair linha digitável antes de liberar os dados do PDF
        linha_digitavel = pdf_service.extrair_linha_digitavel(dados)

        if db.supabase:
            caminho = f"{user['estabelecimento_id']}/{uuid.uuid4()}_{nome}"
            try:
                db.supabase.storage.from_(db.SUPABASE_BUCKET).upload(
                    path=caminho,
                    file=dados,
                    file_options={"content-type": tipo, "upsert": "true"},
                )
            except Exception as storage_exc:
                print(f"[upload] StorageApiError detalhado: {storage_exc!r}", flush=True)
                raise
            arquivo_url = db.supabase.storage.from_(db.SUPABASE_BUCKET).get_public_url(caminho)
        else:
            # fallback local: salva em disco para dev
            dest = Path(db.BASE_DIR) / "uploads" / nome
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(dados)
            arquivo_url = f"/uploads/{nome}"

        del dados
        gc.collect()

        db.salvar_boleto(user["estabelecimento_id"], nome, arquivo_url, valor, vencimento, linha_digitavel)
    except Exception as exc:
        print(f"ERRO FATAL NO UPLOAD: {str(exc)}", flush=True)
        return _tpl(
            request, "gerente_novo.html", user=user, est_nome=est_nome,
            hoje=date.today(),
            error=f"Erro ao salvar o boleto. Tente novamente. ({type(exc).__name__})",
            status_code=500,
        )

    return RedirectResponse("/gerente/contas?msg=Boleto+enviado+com+sucesso%21", status_code=303)


@app.get("/gerente/contas", response_class=HTMLResponse)
async def gerente_contas(request: Request, ok: str = "", msg: str = ""):
    user = _require_user(request)
    if user["perfil"] != "gerente":
        return RedirectResponse("/financeiro", status_code=303)
    rows = db.listar_boletos(estabelecimento_id=user["estabelecimento_id"])
    est_nome = db.get_estabelecimento_nome(user["estabelecimento_id"])
    return _tpl(request, "gerente_contas.html", user=user, est_nome=est_nome, boletos=rows, success=bool(ok or msg), hoje=date.today().isoformat())


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
    return RedirectResponse("/gerente/contas?msg=Boleto+atualizado+com+sucesso%21", status_code=303)


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
    return _tpl(request, "financeiro_historico.html", user=user, boletos=rows,
                filtro_status=status, filtro_inicio=data_inicio, filtro_fim=data_fim,
                statuses=db.STATUSES, hoje=date.today().isoformat())


@app.post("/financeiro/boleto/{boleto_id}/status")
async def financeiro_status(boleto_id: int, request: Request, status: str = Form(...), origem: str = Form("")):
    user = _get_user(request)
    if not user or user["perfil"] != "financeiro":
        raise HTTPException(403)
    if status not in db.STATUSES:
        raise HTTPException(400)
    pago_em = datetime.now().isoformat(timespec="seconds") if status == "Pago" else None
    pago_por = user["login"] if status == "Pago" else None
    db.atualizar_boleto(boleto_id, status=status, pago_em=pago_em, pago_por=pago_por)
    redirect = origem if origem in ("/financeiro", "/financeiro/lote", "/financeiro/historico") else "/financeiro"
    sep = "&" if "?" in redirect else "?"
    return RedirectResponse(f"{redirect}{sep}msg=Status+atualizado+com+sucesso%21", status_code=303)


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
    pdf_bytes = None
    try:
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
    finally:
        del pdf_bytes
        gc.collect()


@app.get("/financeiro/pdf/lote/pedro")
async def download_lote_pedro(request: Request, marcar_em_lote: str = "1"):
    """Atalho rápido: gera o lote da semana atual (mesmo que /pdf/lote)."""
    return await download_lote(request, marcar_em_lote=marcar_em_lote)


@app.get("/financeiro/pdf/urgentes")
async def download_urgentes(request: Request):
    user = _get_user(request)
    if not user or user["perfil"] != "financeiro":
        raise HTTPException(403)
    pdf_bytes = None
    try:
        rows = db.listar_boletos_urgentes_hoje()
        if not rows:
            raise HTTPException(404, "Nenhum boleto urgente hoje.")
        pdf_bytes = pdf_service.gerar_pdf_urgentes_bytes(rows)
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={"Content-Disposition": "inline; filename=boletos_urgentes_hoje.pdf"},
        )
    finally:
        del pdf_bytes
        gc.collect()


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


@app.get("/financeiro/exportar")
async def financeiro_exportar(
    request: Request,
    estabelecimento_id: str = "",
    status: str = "",
    data_inicio: str = "",
    data_fim: str = "",
):
    user = _get_user(request)
    if not user or user["perfil"] not in ("financeiro", "dono"):
        raise HTTPException(403)
    est_id = int(estabelecimento_id) if estabelecimento_id.isdigit() else None
    di = date.fromisoformat(data_inicio) if data_inicio else None
    df = date.fromisoformat(data_fim) if data_fim else None
    csv_content = db.exportar_boletos_csv(
        estabelecimento_id=est_id,
        status=status if status and status != "Todos" else None,
        data_inicio=di,
        data_fim=df,
    )
    filename = f"boletos_{date.today().isoformat()}.csv"
    return StreamingResponse(
        io.BytesIO(csv_content.encode("utf-8-sig")),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ── DONO VIEWS ────────────────────────────────────────────────────────────────

@app.get("/dono", response_class=HTMLResponse)
async def dono_dashboard(
    request: Request,
    estabelecimento_id: str = "",
    status: str = "",
    data_inicio: str = "",
    data_fim: str = "",
):
    _require_dono(request)
    est_id = int(estabelecimento_id) if estabelecimento_id.isdigit() else None
    di = date.fromisoformat(data_inicio) if data_inicio else None
    df = date.fromisoformat(data_fim) if data_fim else None
    boletos = db.listar_boletos(
        estabelecimento_id=est_id,
        status=status if status and status != "Todos" else None,
        data_inicio=di,
        data_fim=df,
    )
    hoje = date.today()
    mes_inicio = date(hoje.year, hoje.month, 1)
    todos_pendentes = db.listar_boletos(status="Pendente")
    urgentes = db.listar_boletos(data_inicio=hoje, data_fim=hoje)
    pagos_mes = db.listar_boletos(status="Pago", data_inicio=mes_inicio)
    stats = {
        "total_pendente": sum(float(r["valor"]) for r in todos_pendentes),
        "qtd_pendente": len(todos_pendentes),
        "qtd_hoje": len(urgentes),
        "total_hoje": sum(float(r["valor"]) for r in urgentes),
        "total_pago_mes": sum(float(r["valor"]) for r in pagos_mes),
        "qtd_pago_mes": len(pagos_mes),
        "total_filtro": sum(float(r["valor"]) for r in boletos),
    }
    return _tpl(
        request, "dono_dashboard.html", user=None,
        boletos=boletos,
        estabelecimentos=db.listar_estabelecimentos(),
        statuses=db.STATUSES,
        stats=stats,
        filtro_est=estabelecimento_id,
        filtro_status=status,
        filtro_inicio=data_inicio,
        filtro_fim=data_fim,
        hoje=hoje.isoformat(),
    )


@app.get("/financeiro/admin", response_class=HTMLResponse)
async def financeiro_admin(request: Request, error: str = ""):
    user = _require_financeiro(request)
    estabelecimentos = db.listar_estabelecimentos()
    gerentes = db.listar_usuarios_gerentes()
    return _tpl(request, "financeiro_admin.html", user=user,
                estabelecimentos=estabelecimentos, gerentes=gerentes,
                error=error)


@app.post("/financeiro/admin/gerente")
async def financeiro_admin_criar_gerente(
    request: Request,
    login: str = Form(...),
    senha: str = Form(...),
    estabelecimento_id: str = Form(""),
    nova_loja: str = Form(""),
):
    _require_financeiro(request)

    est_id_final: int | None = None
    if nova_loja.strip() and not estabelecimento_id:
        try:
            est_id_final = db.criar_estabelecimento(nova_loja.strip())
        except Exception:
            return RedirectResponse(
                f"/financeiro/admin?error=Erro+ao+criar+a+loja+%22{nova_loja}%22.+Verifique+se+o+nome+já+existe.",
                status_code=303,
            )
    elif estabelecimento_id.isdigit():
        est_id_final = int(estabelecimento_id)
    else:
        return RedirectResponse(
            "/financeiro/admin?error=Selecione+um+estabelecimento+ou+informe+o+nome+de+uma+nova+loja.",
            status_code=303,
        )

    try:
        db.criar_gerente(login, senha, est_id_final)
    except Exception:
        return RedirectResponse(
            f"/financeiro/admin?error=Login+%22{login}%22+já+existe.+Escolha+outro.",
            status_code=303,
        )

    return RedirectResponse(
        f"/financeiro/admin?msg=Gerente+%22{login}%22+cadastrado+com+sucesso%21",
        status_code=303,
    )


@app.post("/financeiro/admin/senha-dono")
async def financeiro_admin_senha_dono(
    request: Request,
    nova_senha: str = Form(...),
):
    _require_financeiro(request)
    if len(nova_senha) < 4:
        return RedirectResponse(
            "/financeiro/admin?error=Senha+deve+ter+no+m%C3%ADnimo+4+caracteres.",
            status_code=303,
        )
    db.trocar_senha_usuario("pedro", nova_senha)
    return RedirectResponse(
        "/financeiro/admin?msg=Senha+do+dono+atualizada+com+sucesso%21",
        status_code=303,
    )


@app.post("/financeiro/boleto/{boleto_id}/linha-digitavel")
async def financeiro_salvar_linha(
    boleto_id: int,
    request: Request,
    linha_digitavel: str = Form(""),
    origem: str = Form(""),
):
    user = _get_user(request)
    if not user or user["perfil"] != "financeiro":
        raise HTTPException(403)
    db.atualizar_boleto(boleto_id, linha_digitavel=linha_digitavel.strip() or None)
    redirect = origem if origem in ("/financeiro", "/financeiro/lote", "/financeiro/historico", "/financeiro/boletos") else "/financeiro/historico"
    sep = "&" if "?" in redirect else "?"
    return RedirectResponse(f"{redirect}{sep}msg=C%C3%B3digo+salvo+com+sucesso%21", status_code=303)


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
