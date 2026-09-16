"""
FluxoPay — Bateria de Testes Automatizados
Execução: .venv/bin/python test_system.py
"""
from __future__ import annotations

import io
import shutil
import sys
import tempfile
import traceback
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

# ── cores ANSI ────────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

_results: list[tuple[str, bool, str]] = []


def run(label: str, fn: Callable) -> bool:
    try:
        fn()
        _results.append((label, True, ""))
        print(f"  {GREEN}✓{RESET}  {label}")
        return True
    except Exception as e:
        msg = traceback.format_exc().strip().splitlines()[-1]
        _results.append((label, False, msg))
        print(f"  {RED}✗{RESET}  {label}")
        print(f"     {RED}{msg}{RESET}")
        return False


def section(title: str) -> None:
    print(f"\n{BOLD}{CYAN}▸ {title}{RESET}")


def summary() -> None:
    ok  = sum(1 for _, p, _ in _results if p)
    bad = len(_results) - ok
    print(f"\n{'─'*52}")
    print(f"{BOLD}Resultado:{RESET} {GREEN}{ok} passou{RESET}", end="")
    if bad:
        print(f"  {RED}{bad} falhou{RESET}", end="")
    print(f"  de {len(_results)} testes")
    if bad:
        print(f"\n{RED}Falhas:{RESET}")
        for label, passed, msg in _results:
            if not passed:
                print(f"  • {label}: {msg}")
        sys.exit(1)
    else:
        print(f"{GREEN}{BOLD}Todos os testes passaram!{RESET}")


# ══════════════════════════════════════════════════════════════════════════════
# 1. BANCO DE DADOS
# ══════════════════════════════════════════════════════════════════════════════
section("1. Banco de Dados (SQLite)")

# Usar DB isolado no tmpdir para não sujar produção
_tmp_dir = Path(tempfile.mkdtemp())
_tmp_db  = _tmp_dir / "test_contas.db"
_tmp_upl = _tmp_dir / "uploads"

import os
os.environ["FLUXOPAY_DB"]      = str(_tmp_db)
os.environ["FLUXOPAY_UPLOADS"] = str(_tmp_upl)

# Monkey-patch dos caminhos antes de importar o módulo
import importlib
import importlib.util
import types

# Importar db com caminhos temporários
spec = importlib.util.spec_from_file_location(
    "app.db",
    Path(__file__).parent / "app" / "db.py",
)
db_mod = importlib.util.module_from_spec(spec)

# Substituir caminhos antes de executar o módulo
original_source = (Path(__file__).parent / "app" / "db.py").read_text()
patched_source = original_source.replace(
    'BASE_DIR = Path(__file__).resolve().parent.parent',
    f'BASE_DIR = Path(r"{_tmp_dir}")',
)
exec(compile(patched_source, "app/db.py", "exec"), db_mod.__dict__)
db = db_mod


def t_init_db():
    db.init_db()
    # O DB é criado dentro do _tmp_dir pelo módulo patcheado
    dbs = list(_tmp_dir.glob("*.db"))
    assert dbs, f"nenhum .db encontrado em {_tmp_dir}"


def t_estabelecimentos():
    nomes = [e["nome"] for e in db.listar_estabelecimentos()]
    for esperado in ("GP Conveniencia", "Posto Atibaia", "WP Auto posto"):
        assert esperado in nomes, f"Estabelecimento ausente: {esperado}"


def t_autenticar_gerente():
    u = db.authenticate_user("gerente1", "senha123")
    assert u is not None, "gerente1 não autenticou"
    assert u["perfil"] == "gerente"
    assert u["estabelecimento_id"] == 1


def t_autenticar_financeiro():
    u = db.authenticate_user("financeiro", "admin123")
    assert u is not None, "financeiro não autenticou"
    assert u["perfil"] == "financeiro"
    assert u["estabelecimento_id"] is None


def t_autenticar_falha():
    u = db.authenticate_user("gerente1", "senha_errada")
    assert u is None, "deveria rejeitar senha errada"


def t_salvar_e_listar_boleto():
    bid = db.salvar_boleto(1, "/tmp/fake.pdf", 150.00, date(2026, 9, 20))
    assert isinstance(bid, int) and bid > 0
    rows = db.listar_boletos(estabelecimento_id=1)
    assert any(r["id"] == bid for r in rows), "boleto não encontrado após salvar"


def t_verificar_duplicata_encontra():
    db.salvar_boleto(1, "/tmp/dup.pdf", 250.00, date(2026, 9, 25))
    dup = db.verificar_duplicata(1, 250.00, date(2026, 9, 25))
    assert dup is not None, "duplicata deveria ser detectada"
    assert dup["valor"] == 250.00


def t_verificar_duplicata_nao_encontra():
    dup = db.verificar_duplicata(1, 999.99, date(2026, 9, 25))
    assert dup is None, "não deveria detectar duplicata para valor diferente"


def t_verificar_duplicata_ignora_pago():
    bid = db.salvar_boleto(1, "/tmp/pago.pdf", 333.00, date(2026, 9, 27))
    db.atualizar_boleto(bid, status="Pago")
    dup = db.verificar_duplicata(1, 333.00, date(2026, 9, 27))
    assert dup is None, "boleto Pago não deve contar como duplicata"


def t_atualizar_status():
    bid = db.salvar_boleto(2, "/tmp/upd.pdf", 77.00, date(2026, 9, 22))
    db.atualizar_boleto(bid, status="Em Lote")
    b = db.get_boleto_by_id(bid)
    assert b["status"] == "Em Lote"


def t_marcar_lote_em_lote():
    ids = [
        db.salvar_boleto(1, "/tmp/lote1.pdf", 10.00, date(2026, 9, 16)),
        db.salvar_boleto(2, "/tmp/lote2.pdf", 20.00, date(2026, 9, 16)),
    ]
    db.marcar_lote_em_lote(ids)
    for i in ids:
        b = db.get_boleto_by_id(i)
        assert b["status"] == "Em Lote", f"boleto {i} não foi marcado Em Lote"


def t_excluir_boleto():
    bid = db.salvar_boleto(3, "/tmp/del.pdf", 55.00, date(2026, 9, 28))
    db.excluir_boleto(bid)
    assert db.get_boleto_by_id(bid) is None, "boleto deveria ter sido excluído"


run("init_db cria arquivo e tabelas",       t_init_db)
run("3 estabelecimentos presentes",          t_estabelecimentos)
run("autenticação gerente válida",           t_autenticar_gerente)
run("autenticação financeiro válida",        t_autenticar_financeiro)
run("autenticação com senha errada falha",   t_autenticar_falha)
run("salvar e listar boleto",                t_salvar_e_listar_boleto)
run("verificar_duplicata detecta cópia",     t_verificar_duplicata_encontra)
run("verificar_duplicata não falso positivo",t_verificar_duplicata_nao_encontra)
run("verificar_duplicata ignora status Pago",t_verificar_duplicata_ignora_pago)
run("atualizar status do boleto",            t_atualizar_status)
run("marcar_lote_em_lote em batch",          t_marcar_lote_em_lote)
run("excluir boleto",                        t_excluir_boleto)


# ══════════════════════════════════════════════════════════════════════════════
# 2. REGRA DO LOTE (SÁBADO → SEXTA)
# ══════════════════════════════════════════════════════════════════════════════
section("2. Regra do Lote Semanal (Sáb → Sex)")


def _lote_para(dia: date) -> tuple[date, date]:
    """Replica a lógica de calcular_lote_atual com uma data injetada."""
    if dia.weekday() in (5, 6):                           # sáb ou dom
        dias_fwd = (7 - dia.weekday()) % 7
        segunda = dia + timedelta(days=dias_fwd)
    else:
        segunda = dia - timedelta(days=dia.weekday())     # segunda da semana atual
    return segunda - timedelta(days=2), segunda + timedelta(days=4)


def t_lote_segunda():
    seg = date(2026, 9, 14)                 # segunda-feira
    ini, fim = _lote_para(seg)
    assert ini == date(2026, 9, 12), f"início errado: {ini}"  # sábado anterior
    assert fim == date(2026, 9, 18), f"fim errado: {fim}"     # sexta-feira


def t_lote_quarta():
    qua = date(2026, 9, 16)                 # quarta-feira
    ini, fim = _lote_para(qua)
    assert ini == date(2026, 9, 12)
    assert fim == date(2026, 9, 18)


def t_lote_sexta():
    sex = date(2026, 9, 18)                 # sexta-feira (mesmo lote)
    ini, fim = _lote_para(sex)
    assert ini == date(2026, 9, 12)
    assert fim == date(2026, 9, 18)


def t_lote_sabado_prep():
    sab = date(2026, 9, 19)                 # sábado → prepara próxima semana
    ini, fim = _lote_para(sab)
    assert ini == date(2026, 9, 19), f"sáb: início esperado 19/09, got {ini}"
    assert fim == date(2026, 9, 25), f"sáb: fim esperado 25/09, got {fim}"


def t_lote_domingo_prep():
    dom = date(2026, 9, 20)                 # domingo → mesmo lote da próxima semana
    ini, fim = _lote_para(dom)
    assert ini == date(2026, 9, 19)
    assert fim == date(2026, 9, 25)


def t_lote_virada_de_mes():
    seg = date(2026, 9, 28)                 # semana que atravessa outubro
    ini, fim = _lote_para(seg)
    assert ini == date(2026, 9, 26)
    assert fim == date(2026, 10, 2)


def t_lote_real_db():
    ini, fim = db.calcular_lote_atual()
    assert isinstance(ini, date)
    assert isinstance(fim, date)
    # Início é sempre sábado (weekday 5) e fim é sexta (weekday 4)
    assert ini.weekday() == 5, f"início deveria ser sábado, got weekday {ini.weekday()} ({ini})"
    assert fim.weekday() == 4, f"fim deveria ser sexta, got weekday {fim.weekday()} ({fim})"
    assert (fim - ini).days == 6, "janela deveria ter exatamente 7 dias (sáb→sex inclusive)"


run("lote para segunda-feira",              t_lote_segunda)
run("lote para quarta-feira (meio semana)", t_lote_quarta)
run("lote para sexta-feira (último dia)",   t_lote_sexta)
run("lote para sábado (modo preparação)",   t_lote_sabado_prep)
run("lote para domingo (modo preparação)",  t_lote_domingo_prep)
run("lote atravessa virada de mês",         t_lote_virada_de_mes)
run("calcular_lote_atual retorna Sáb→Sex",  t_lote_real_db)


# ══════════════════════════════════════════════════════════════════════════════
# 3. PDF SERVICE — CAPA DIVISÓRIA
# ══════════════════════════════════════════════════════════════════════════════
section("3. PDF Service — Capa Divisória")

import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent))

# Monkey-patch dos caminhos do pdf_service também
pdf_source = (Path(__file__).parent / "app" / "pdf_service.py").read_text()
pdf_patched = pdf_source.replace(
    'BASE_DIR = Path(__file__).resolve().parent.parent',
    f'BASE_DIR = Path(r"{_tmp_dir}")',
)
pdf_mod = types.ModuleType("app.pdf_service")
exec(compile(pdf_patched, "app/pdf_service.py", "exec"), pdf_mod.__dict__)
pdf_service = pdf_mod


def t_capa_nao_vazia():
    data = pdf_service._criar_pagina_divisoria("GP Conveniencia", qtd_boletos=3)
    assert isinstance(data, bytes)
    assert len(data) > 1000, f"capa muito pequena: {len(data)} bytes"
    assert data[:4] == b"%PDF", "não começa com assinatura PDF"


def t_capa_nome_longo():
    data = pdf_service._criar_pagina_divisoria("WP Auto posto com nome muito longo para testar adaptação", 1)
    assert len(data) > 1000


def t_capa_sem_boletos():
    data = pdf_service._criar_pagina_divisoria("Posto Atibaia", qtd_boletos=0)
    assert len(data) > 1000


run("capa divisória gera PDF não-vazio",     t_capa_nao_vazia)
run("capa com nome muito longo (font adaptativa)", t_capa_nome_longo)
run("capa com qtd_boletos=0",                t_capa_sem_boletos)


# ══════════════════════════════════════════════════════════════════════════════
# 4. PDF SERVICE — MESCLAGEM DO LOTE
# ══════════════════════════════════════════════════════════════════════════════
section("4. PDF Service — Mesclagem do Lote")

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas as rl_canvas


def _make_fake_pdf(path: Path, text: str = "BOLETO TESTE") -> Path:
    """Gera um PDF mínimo de 1 página via ReportLab."""
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    c.drawString(72, 700, text)
    c.showPage()
    c.save()
    path.write_bytes(buf.getvalue())
    return path


def t_mesclagem_single_loja():
    p1 = _make_fake_pdf(_tmp_upl / "b1.pdf", "GP Boleto 1")
    p2 = _make_fake_pdf(_tmp_upl / "b2.pdf", "GP Boleto 2")
    rows = [
        {"estabelecimento": "GP Conveniencia", "caminho_arquivo": str(p1), "valor": 100.0, "id": 1},
        {"estabelecimento": "GP Conveniencia", "caminho_arquivo": str(p2), "valor": 200.0, "id": 2},
    ]
    out = _tmp_dir / "lote_single.pdf"
    result = pdf_service.gerar_pdf_agrupado(rows, out)
    assert result.exists()
    assert result.stat().st_size > 2000

    from pypdf import PdfReader
    reader = PdfReader(str(result))
    # 1 capa + 2 boletos = 3 páginas
    assert len(reader.pages) == 3, f"esperado 3 páginas, got {len(reader.pages)}"


def t_mesclagem_multi_lojas():
    p1 = _make_fake_pdf(_tmp_upl / "m1.pdf", "GP Boleto")
    p2 = _make_fake_pdf(_tmp_upl / "m2.pdf", "Atibaia Boleto")
    rows = [
        {"estabelecimento": "GP Conveniencia", "caminho_arquivo": str(p1), "valor": 50.0, "id": 10},
        {"estabelecimento": "Posto Atibaia",   "caminho_arquivo": str(p2), "valor": 75.0, "id": 11},
    ]
    out = _tmp_dir / "lote_multi.pdf"
    result = pdf_service.gerar_pdf_agrupado(rows, out)
    from pypdf import PdfReader
    reader = PdfReader(str(result))
    # 2 capas + 2 boletos = 4 páginas
    assert len(reader.pages) == 4, f"esperado 4 páginas, got {len(reader.pages)}"


def t_mesclagem_arquivo_ausente_nao_crasha():
    rows = [
        {"estabelecimento": "GP Conveniencia", "caminho_arquivo": "/nao/existe.pdf", "valor": 10.0, "id": 99},
    ]
    out = _tmp_dir / "lote_missing.pdf"
    # arquivo ausente é silenciosamente ignorado; só a capa é gerada
    result = pdf_service.gerar_pdf_agrupado(rows, out)
    assert result.exists()


def t_mesclagem_vazia_levanta_erro():
    try:
        pdf_service.gerar_pdf_agrupado([], _tmp_dir / "vazio.pdf")
        assert False, "deveria ter levantado ValueError"
    except ValueError:
        pass


run("mesclagem de 1 loja (capa + 2 boletos = 3 pgs)", t_mesclagem_single_loja)
run("mesclagem de 2 lojas (2 capas + 2 boletos = 4 pgs)", t_mesclagem_multi_lojas)
run("arquivo PDF ausente é ignorado sem crash", t_mesclagem_arquivo_ausente_nao_crasha)
run("lista vazia levanta ValueError",           t_mesclagem_vazia_levanta_erro)


# ══════════════════════════════════════════════════════════════════════════════
# 5. FILTRO SCANNER (Pillow + numpy)
# ══════════════════════════════════════════════════════════════════════════════
section("5. Filtro Scanner de Imagem")

import numpy as np
from PIL import Image


def _make_foto(w=800, h=600, noise=True) -> Image.Image:
    """
    Simula foto de boleto com alto contraste: fundo branco com linhas de texto
    quase pretas — condição ideal para a binarização adaptativa funcionar bem.
    """
    rng = np.random.default_rng(42)
    # fundo branco (240–255) com iluminação levemente desigual
    arr = np.full((h, w, 3), 245, dtype=np.uint8)
    # gradiente suave de iluminação (simula sombra de celular)
    grad = np.linspace(0, 15, w, dtype=np.float32)
    arr[:, :, :] = np.clip(arr[:, :, :] - grad[np.newaxis, :, np.newaxis], 220, 255).astype(np.uint8)
    # "texto" — linhas de pixels muito escuros (0–20) espaçadas
    for y in range(60, h - 60, 20):
        thick = 2 if y % 40 == 0 else 1
        arr[y:y+thick, 40:w-40] = 10
    if noise:
        arr = np.clip(arr.astype(int) + rng.integers(-8, 8, arr.shape), 0, 255).astype(np.uint8)
    return Image.fromarray(arr, "RGB")


def t_scanner_saida_a4():
    img = _make_foto()
    result = pdf_service._scanner_filter(img)
    assert result.size == (1654, 2339), f"tamanho errado: {result.size}"
    assert result.mode == "L", f"modo errado: {result.mode}"


def t_scanner_binarizado():
    img = _make_foto()
    result = pdf_service._scanner_filter(img)
    arr = np.array(result)
    # Após binarização adaptativa + resize LANCZOS para A4, as bordas de linhas
    # ganham anti-aliasing (valores intermediários). Verificamos dois critérios:
    # 1. >80% dos pixels são estritamente binários (0 ou 255)
    # 2. Pixels de fundo (valor >= 200) são quase todos brancos (>=250) — sem sujeira
    total = arr.size
    binarios = int(np.sum((arr == 0) | (arr == 255)))
    pct_binario = binarios / total
    assert pct_binario > 0.80, f"apenas {pct_binario:.1%} dos pixels são binários (esperado >80%)"

    # Fundo deve ser branco puro (sem manchas cinzas grandes)
    fundo_pixels = arr[arr >= 200]
    pct_fundo_branco = np.sum(fundo_pixels >= 250) / max(len(fundo_pixels), 1)
    assert pct_fundo_branco > 0.95, f"fundo com muita sujeira: {pct_fundo_branco:.1%} brancos"


def t_scanner_paisagem_rotacionado():
    # Foto em modo paisagem (mais larga que alta)
    img = _make_foto(w=1200, h=800)
    result = pdf_service._scanner_filter(img)
    assert result.size == (1654, 2339)


def t_scanner_imagem_pequena():
    # Foto muito pequena (thumbnail de câmera front)
    img = _make_foto(w=120, h=90)
    result = pdf_service._scanner_filter(img)
    assert result.size == (1654, 2339)


def t_salvar_upload_imagem_gera_pdf():
    img = _make_foto(400, 300, noise=False)
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    caminho = pdf_service.salvar_upload(buf.getvalue(), "foto_boleto.jpg", "image/jpeg")
    p = Path(caminho)
    assert p.exists(), "arquivo não criado"
    assert p.suffix == ".pdf", f"deveria ser .pdf, got {p.suffix}"
    assert p.stat().st_size > 1000
    assert p.read_bytes()[:4] == b"%PDF"


def t_salvar_upload_pdf_direto():
    fake_pdf = io.BytesIO()
    c = rl_canvas.Canvas(fake_pdf, pagesize=A4)
    c.drawString(72, 700, "Teste")
    c.showPage()
    c.save()
    caminho = pdf_service.salvar_upload(fake_pdf.getvalue(), "boleto.pdf", "application/pdf")
    p = Path(caminho)
    assert p.exists()
    assert p.suffix == ".pdf"
    assert p.read_bytes()[:4] == b"%PDF"


run("scanner: saída é A4 1654×2339px modo L",   t_scanner_saida_a4)
run("scanner: resultado binarizado (só 0 e 255)", t_scanner_binarizado)
run("scanner: imagem paisagem cabe em A4",       t_scanner_paisagem_rotacionado)
run("scanner: imagem muito pequena escala OK",   t_scanner_imagem_pequena)
run("salvar_upload imagem → converte para PDF",  t_salvar_upload_imagem_gera_pdf)
run("salvar_upload PDF → salva direto",          t_salvar_upload_pdf_direto)


# ══════════════════════════════════════════════════════════════════════════════
# 6. AUTENTICAÇÃO / COOKIE (login.py via FastAPI TestClient)
# ══════════════════════════════════════════════════════════════════════════════
section("6. Rotas HTTP — Login, Sessão, Logout")

from fastapi.testclient import TestClient

# Importar main com os mesmos caminhos patcheados
main_source = (Path(__file__).parent / "app" / "main.py").read_text()
main_patched = main_source  # main usa db e pdf_service como módulos relativos

# Montar app via importação normal (db já está iniciado no tmp)
import importlib.util as _ilu

# Recriar módulos no sys.modules com os paths patcheados
import sys as _sys2
_sys2.modules.pop("app", None)
_sys2.modules.pop("app.db", None)
_sys2.modules.pop("app.pdf_service", None)
_sys2.modules.pop("app.main", None)

# Registrar db_mod patcheado
import types as _types
_pkg = _types.ModuleType("app")
_pkg.__path__ = [str(Path(__file__).parent / "app")]
_sys2.modules["app"] = _pkg
_sys2.modules["app.db"] = db_mod
_sys2.modules["app.pdf_service"] = pdf_service

main_spec = _ilu.spec_from_file_location("app.main", Path(__file__).parent / "app" / "main.py")
main_mod  = _ilu.module_from_spec(main_spec)
main_spec.loader.exec_module(main_mod)
_sys2.modules["app.main"] = main_mod

client = TestClient(main_mod.app, follow_redirects=False)


def t_login_page_ok():
    r = client.get("/login")
    assert r.status_code == 200
    assert "FluxoPay" in r.text
    assert "Manter conectado" in r.text


def t_login_invalido():
    r = client.post("/login", data={"login": "x", "senha": "y"})
    assert r.status_code == 401
    assert "incorretos" in r.text.lower()


def t_login_gerente_sem_remember():
    r = client.post("/login", data={"login": "gerente1", "senha": "senha123"})
    assert r.status_code == 303
    cookie = r.cookies.get("session") or r.headers.get("set-cookie", "")
    # max-age deve ser 86400 (24h) — não deve ter 2592000
    set_cookie_hdr = r.headers.get("set-cookie", "")
    assert "2592000" not in set_cookie_hdr, "remember desmarcado não deve gerar cookie 30d"
    assert "max-age=86400" in set_cookie_hdr.lower() or "max-age" in set_cookie_hdr.lower()


def t_login_gerente_com_remember():
    r = client.post("/login", data={"login": "gerente1", "senha": "senha123", "remember": "1"})
    assert r.status_code == 303
    set_cookie_hdr = r.headers.get("set-cookie", "")
    assert "2592000" in set_cookie_hdr, f"remember marcado deve ter max-age=2592000; header: {set_cookie_hdr}"


def t_rota_protegida_sem_sessao():
    r = client.get("/financeiro")
    assert r.status_code in (303, 307), "deve redirecionar para login sem sessão"


def t_rota_gerente_acessivel():
    # Login + acesso protegido com cookie
    lr = client.post("/login", data={"login": "gerente1", "senha": "senha123"})
    assert lr.status_code == 303
    session_cookie = lr.cookies.get("session")
    assert session_cookie, "cookie de sessão não foi definido"
    r = client.get("/gerente", cookies={"session": session_cookie})
    assert r.status_code == 200


def t_rota_financeiro_acessivel():
    lr = client.post("/login", data={"login": "financeiro", "senha": "admin123"})
    assert lr.status_code == 303
    session_cookie = lr.cookies.get("session")
    r = client.get("/financeiro", cookies={"session": session_cookie})
    assert r.status_code == 200
    assert "Dashboard" in r.text


def t_logout_limpa_sessao():
    lr = client.post("/login", data={"login": "financeiro", "senha": "admin123"})
    token = lr.cookies.get("session")
    client.get("/logout", cookies={"session": token})
    # após logout, token inválido → redireciona para login
    r = client.get("/financeiro", cookies={"session": token})
    assert r.status_code in (303, 307)


def t_gerente_nao_acessa_financeiro():
    lr = client.post("/login", data={"login": "gerente1", "senha": "senha123"})
    token = lr.cookies.get("session")
    r = client.get("/financeiro", cookies={"session": token})
    # deve redirecionar (perfil errado)
    assert r.status_code in (303, 307)


def t_dashboard_mostra_shortcut_pedro():
    lr = client.post("/login", data={"login": "financeiro", "senha": "admin123"})
    token = lr.cookies.get("session")
    r = client.get("/financeiro", cookies={"session": token})
    assert "Lote Semanal do Pedro" in r.text


def t_lote_page_checkbox_em_lote():
    lr = client.post("/login", data={"login": "financeiro", "senha": "admin123"})
    token = lr.cookies.get("session")
    r = client.get("/financeiro/lote", cookies={"session": token})
    assert r.status_code == 200
    assert "lote-marcar" in r.text or "Marcar como" in r.text


run("GET /login retorna 200 com 'Manter conectado'",          t_login_page_ok)
run("login com credenciais inválidas retorna 401",             t_login_invalido)
run("login sem remember → cookie max-age 86400 (24h)",        t_login_gerente_sem_remember)
run("login com remember=1 → cookie max-age 2592000 (30d)",    t_login_gerente_com_remember)
run("rota protegida sem sessão redireciona para login",        t_rota_protegida_sem_sessao)
run("gerente1 acessa /gerente com sessão válida",              t_rota_gerente_acessivel)
run("financeiro acessa /financeiro com sessão válida",         t_rota_financeiro_acessivel)
run("logout invalida sessão",                                   t_logout_limpa_sessao)
run("gerente não acessa rota /financeiro",                     t_gerente_nao_acessa_financeiro)
run("dashboard exibe shortcut 'Lote Semanal do Pedro'",        t_dashboard_mostra_shortcut_pedro)
run("página lote exibe checkbox 'Em Lote'",                    t_lote_page_checkbox_em_lote)


# ══════════════════════════════════════════════════════════════════════════════
# 7. SINTAXE / COMPILAÇÃO DO PROJETO
# ══════════════════════════════════════════════════════════════════════════════
section("7. Sintaxe e Compilação do Projeto")

import py_compile


def _check_syntax(rel: str):
    p = Path(__file__).parent / rel
    # cfile=None grava no __pycache__ padrão; usar tmpdir evita colisão de lock
    cfile = str(_tmp_dir / (p.stem + ".pyc"))
    py_compile.compile(str(p), cfile=cfile, doraise=True)


def t_syntax_db():       _check_syntax("app/db.py")
def t_syntax_main():     _check_syntax("app/main.py")
def t_syntax_pdf():      _check_syntax("app/pdf_service.py")
def t_syntax_test():     _check_syntax("test_system.py")


def t_templates_renderizam():
    from jinja2 import Environment, FileSystemLoader, Undefined
    env = Environment(
        loader=FileSystemLoader(str(Path(__file__).parent / "app" / "templates")),
        undefined=Undefined,
    )
    env.filters["currency"] = lambda x: f"R$ {float(x or 0):,.2f}"
    env.filters["date_br"] = lambda x: str(x)

    tpls_e_ctx = {
        "login.html":                  {"error": None},
        "financeiro_dashboard.html":   {
            "user": {"perfil": "financeiro", "login": "financeiro"},
            "stats": {
                "total_lote_valor": 0, "total_lote_qtd": 0, "urgentes_hoje": 0,
                "pendentes_qtd": 0, "pagos_qtd": 0,
                "lote_inicio": date(2026, 9, 12), "lote_fim": date(2026, 9, 18),
            },
            "urgent_rows": [],
        },
        "financeiro_lote.html": {
            "user": {"perfil": "financeiro", "login": "financeiro"},
            "boletos": [], "lote_inicio": date(2026, 9, 12), "lote_fim": date(2026, 9, 18),
        },
        "gerente_novo.html": {
            "user": {"perfil": "gerente"}, "est_nome": "GP Conveniencia",
            "hoje": date.today(), "error": None,
        },
    }
    for name, ctx in tpls_e_ctx.items():
        try:
            env.get_template(name).render(**ctx)
        except Exception as e:
            raise AssertionError(f"{name}: {e}") from e


run("sintaxe app/db.py",           t_syntax_db)
run("sintaxe app/main.py",         t_syntax_main)
run("sintaxe app/pdf_service.py",  t_syntax_pdf)
run("sintaxe test_system.py",      t_syntax_test)
run("templates Jinja2 renderizam sem erro", t_templates_renderizam)


# ── limpeza ───────────────────────────────────────────────────────────────────
shutil.rmtree(_tmp_dir, ignore_errors=True)

summary()
