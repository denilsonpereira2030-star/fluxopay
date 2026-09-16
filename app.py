from __future__ import annotations

import io
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import streamlit as st
from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

BASE_DIR = Path(__file__).resolve().parent
UPLOADS_DIR = BASE_DIR / "uploads"
DB_PATH = BASE_DIR / "contas.db"
LOTE_OUTPUT_PATH = BASE_DIR / "lote_impressao.pdf"
URGENTES_OUTPUT_PATH = BASE_DIR / "boletos_urgentes_hoje.pdf"
STATUSES = ["Pendente", "Em Lote", "Pago"]

CUSTOM_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap');

* {
    font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif !important;
    box-sizing: border-box;
}

body, .stApp {
    background-color: #0F172A !important;
    color: #F8FAFC !important;
}

header[data-testid="stHeader"], footer, div[data-testid="stDecoration"], #MainMenu {
    display: none !important;
}

.block-container {
    padding: 1rem 1rem 3rem 1rem !important;
    max-width: 1000px !important;
}

.login-wrapper {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    min-height: 80vh;
    padding: 1rem;
}

.login-card {
    background: #1E293B;
    border: 1px solid #334155;
    border-radius: 24px;
    padding: 2.25rem 1.75rem;
    width: 100%;
    max-width: 400px;
    box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
}

.login-badge {
    background: rgba(37, 99, 235, 0.15);
    border: 1px solid rgba(59, 130, 246, 0.3);
    color: #60A5FA;
    font-size: 0.75rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    padding: 0.35rem 0.75rem;
    border-radius: 9999px;
    display: inline-block;
    margin-bottom: 0.75rem;
}

.login-title {
    font-size: 1.5rem;
    font-weight: 800;
    color: #FFFFFF;
    margin: 0 0 0.25rem 0;
    letter-spacing: -0.025em;
}

.login-sub {
    font-size: 0.875rem;
    color: #94A3B8;
    margin-bottom: 1.5rem;
}

.app-header {
    background: #1E293B;
    border: 1px solid #334155;
    border-radius: 18px;
    padding: 1rem 1.25rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 1.25rem;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
}

.header-brand {
    font-size: 1.1rem;
    font-weight: 800;
    color: #FFFFFF;
    margin: 0;
}

.header-tag {
    background: #2563EB;
    color: #FFFFFF;
    font-size: 0.75rem;
    font-weight: 700;
    padding: 0.3rem 0.7rem;
    border-radius: 8px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
}

.stTabs [data-baseweb="tab-list"] {
    gap: 6px !important;
    background-color: #1E293B !important;
    padding: 5px !important;
    border-radius: 14px !important;
    border: 1px solid #334155 !important;
    width: 100% !important;
}

.stTabs [data-baseweb="tab"] {
    flex: 1 !important;
    height: 44px !important;
    border-radius: 10px !important;
    border: none !important;
    color: #94A3B8 !important;
    font-weight: 600 !important;
    font-size: 0.85rem !important;
    background-color: transparent !important;
    text-align: center !important;
}

.stTabs [aria-selected="true"] {
    background-color: #2563EB !important;
    color: #FFFFFF !important;
}

.stTabs [data-baseweb="tab-highlight"] {
    display: none !important;
}

.card-box,
.section-card {
    background: #1E293B;
    border: 1px solid #334155;
    border-radius: 18px;
    padding: 1.25rem;
    margin-top: 1rem;
    box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.2);
}

.section-title {
    font-size: 1.05rem;
    font-weight: 800;
    color: #FFFFFF;
    margin: 0 0 1rem 0;
    letter-spacing: -0.015em;
}

.stButton > button {
    width: 100% !important;
    min-height: 48px !important;
    border-radius: 12px !important;
    font-weight: 700 !important;
    font-size: 0.95rem !important;
    background: #334155 !important;
    color: #FFFFFF !important;
    border: 1px solid #475569 !important;
    transition: all 0.2s ease !important;
    margin-top: 0.25rem;
}

.stButton > button:active {
    transform: scale(0.98) !important;
}

.btn-whatsapp-touch {
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    width: 100% !important;
    min-height: 52px !important;
    background: linear-gradient(135deg, #10B981 0%, #059669 100%) !important;
    color: #FFFFFF !important;
    font-weight: 800 !important;
    font-size: 1rem !important;
    border-radius: 14px !important;
    text-decoration: none !important;
    box-shadow: 0 8px 20px rgba(16, 185, 129, 0.3) !important;
    margin-top: 1rem !important;
    text-align: center !important;
}

.btn-whatsapp-touch:hover {
    color: #FFFFFF !important;
}

div[data-testid="stMetric"] {
    background: #1E293B !important;
    border: 1px solid #334155 !important;
    border-radius: 16px !important;
    padding: 1rem !important;
}

div[data-testid="stMetricLabel"] {
    color: #94A3B8 !important;
    font-size: 0.75rem !important;
    font-weight: 700 !important;
}

div[data-testid="stMetricValue"] {
    color: #FFFFFF !important;
    font-size: 1.4rem !important;
    font-weight: 800 !important;
}

.whatsapp-banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    flex-wrap: wrap;
    background: linear-gradient(135deg, rgba(16,185,129,0.12), rgba(20,184,166,0.10));
    border: 1px solid rgba(16,185,129,0.28);
    color: #D1FAE5;
    border-radius: 16px;
    padding: 0.9rem 1rem;
    margin: 0.4rem 0 1rem;
}

.whatsapp-banner__text {
    font-weight: 700;
    letter-spacing: -0.01em;
}

.whatsapp-banner a {
    text-decoration: none;
}

.boleto-card {
    background: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-left: 5px solid #2563EB;
    border-radius: 18px;
    padding: 1rem;
    box-shadow: 0 4px 18px -4px rgba(15, 23, 42, 0.06);
    margin-bottom: 1rem;
}

.boleto-card--urgent {
    border-left-color: #F59E0B;
}

.boleto-card__header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    flex-wrap: wrap;
    margin-bottom: 0.75rem;
}

.boleto-card__title {
    margin: 0;
    font-size: 1.05rem;
    font-weight: 800;
    color: #0F172A;
}

.status-pill {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0.28rem 0.65rem;
    border-radius: 999px;
    font-size: 0.68rem;
    font-weight: 800;
    letter-spacing: 0.06em;
    text-transform: uppercase;
}

.status-pendente {
    background: #FEF3C7;
    color: #92400E;
}

.status-em-lote {
    background: #DBEAFE;
    color: #1E40AF;
}

.status-pago {
    background: #D1FAE5;
    color: #065F46;
}

.boleto-card__meta {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.75rem;
}

.meta-box {
    background: #F8FAFC;
    border: 1px solid #E2E8F0;
    border-radius: 12px;
    padding: 0.7rem 0.8rem;
}

.meta-label {
    display: block;
    font-size: 0.68rem;
    font-weight: 700;
    color: #64748B;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    margin-bottom: 0.2rem;
}

.meta-value {
    font-size: 0.95rem;
    font-weight: 700;
    color: #0F172A;
}

.value-highlight {
    font-size: 1.2rem;
    font-weight: 800;
    letter-spacing: -0.02em;
}

@media (max-width: 768px) {
    .block-container {
        padding: 0.5rem 0.5rem 2rem 0.5rem !important;
    }

    .app-header {
        flex-direction: column;
        align-items: flex-start;
        gap: 0.5rem;
    }

    .header-tag {
        align-self: flex-start;
    }

    .stTabs [data-baseweb="tab"] {
        font-size: 0.75rem !important;
        padding: 0 4px !important;
    }
}
</style>
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

    conn = get_connection()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS estabelecimentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL UNIQUE
        )
        """
    )

    estabelecimentos_count = conn.execute("SELECT COUNT(*) AS total FROM estabelecimentos").fetchone()["total"]
    if estabelecimentos_count == 0:
        conn.executemany(
            "INSERT INTO estabelecimentos (nome) VALUES (?)",
            [
                ("GP Conveniencia",),
                ("Posto Atibaia",),
                ("WP Auto posto",),
            ],
        )

    conn.execute(
        "UPDATE estabelecimentos SET nome = ? WHERE id = 1",
        ("GP Conveniencia",),
    )
    conn.execute(
        "UPDATE estabelecimentos SET nome = ? WHERE id = 2",
        ("Posto Atibaia",),
    )
    conn.execute(
        "UPDATE estabelecimentos SET nome = ? WHERE id = 3",
        ("WP Auto posto",),
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            estabelecimento_id INTEGER,
            login TEXT NOT NULL UNIQUE,
            senha TEXT NOT NULL,
            perfil TEXT NOT NULL CHECK(perfil IN ('gerente', 'financeiro'))
        )
        """
    )

    usuarios_count = conn.execute("SELECT COUNT(*) AS total FROM usuarios").fetchone()["total"]
    if usuarios_count == 0:
        conn.executemany(
            """
            INSERT INTO usuarios (estabelecimento_id, login, senha, perfil)
            VALUES (?, ?, ?, ?)
            """,
            [
                (1, "gerente1", "senha123", "gerente"),
                (2, "gerente2", "senha123", "gerente"),
                (3, "gerente3", "senha123", "gerente"),
                (None, "financeiro", "admin123", "financeiro"),
            ],
        )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS boletos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            estabelecimento_id INTEGER NOT NULL,
            caminho_arquivo TEXT NOT NULL,
            valor REAL NOT NULL,
            data_vencimento TEXT NOT NULL,
            data_envio TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Pendente',
            FOREIGN KEY (estabelecimento_id) REFERENCES estabelecimentos(id)
        )
        """
    )

    boleto_columns = conn.execute("PRAGMA table_info(boletos)").fetchall()
    tem_status = any(column["name"] == "status" for column in boleto_columns)
    if not tem_status:
        conn.execute("ALTER TABLE boletos ADD COLUMN status TEXT DEFAULT 'Pendente'")
        conn.execute("UPDATE boletos SET status = 'Pendente' WHERE status IS NULL")

    conn.commit()
    conn.close()


def parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def listar_estabelecimentos() -> list[sqlite3.Row]:
    conn = get_connection()
    rows = conn.execute("SELECT id, nome FROM estabelecimentos ORDER BY id").fetchall()
    conn.close()
    return rows


def authenticate_user(login: str, senha: str) -> sqlite3.Row | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT id, estabelecimento_id, login, perfil FROM usuarios WHERE login = ? AND senha = ?",
        (login, senha),
    ).fetchone()
    conn.close()
    return row


def obter_estabelecimento_nome(estabelecimento_id: int | None) -> str:
    if estabelecimento_id is None:
        return "Financeiro"
    conn = get_connection()
    row = conn.execute(
        "SELECT nome FROM estabelecimentos WHERE id = ?",
        (estabelecimento_id,),
    ).fetchone()
    conn.close()
    return row["nome"] if row else ""


def normalize_filename(nome: str) -> str:
    nome_limpo = (nome or "arquivo").strip()
    caracteres = [c for c in nome_limpo if c.isalnum() or c in "._- "]
    return "".join(caracteres).replace(" ", "_")


def save_attachment(file_obj, nome_original: str) -> str:
    if file_obj is None:
        raise ValueError("Nenhum arquivo foi fornecido.")

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    nome_base = normalize_filename(nome_original or "arquivo")

    if getattr(file_obj, "type", None) and file_obj.type.startswith("image/"):
        file_bytes = file_obj.read()
        image = Image.open(io.BytesIO(file_bytes)).convert("RGB")
        pdf_path = UPLOADS_DIR / f"{timestamp}_{nome_base}.pdf"
        image.save(pdf_path, "PDF")
        return str(pdf_path)

    if hasattr(file_obj, "read") and callable(file_obj.read):
        file_bytes = file_obj.read()
        ext = Path(nome_original or "arquivo").suffix.lower()
        if (not ext) or ext not in {".pdf", ".png", ".jpg", ".jpeg"}:
            ext = ".pdf" if getattr(file_obj, "type", "") == "application/pdf" else ".bin"

        target_name = f"{timestamp}_{nome_base}{ext}"
        target_path = UPLOADS_DIR / target_name
        target_path.write_bytes(file_bytes)
        return str(target_path)

    raise ValueError("Tipo de arquivo não suportado.")


def salvar_boleto(estabelecimento_id: int, caminho_arquivo: str, valor: float, data_vencimento: date) -> None:
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO boletos (estabelecimento_id, caminho_arquivo, valor, data_vencimento, data_envio, status)
        VALUES (?, ?, ?, ?, ?, 'Pendente')
        """,
        (
            estabelecimento_id,
            caminho_arquivo,
            valor,
            data_vencimento.isoformat(),
            date.today().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def atualizar_boleto(boleto_id: int, valor: float | None = None, data_vencimento: date | None = None, status: str | None = None) -> None:
    conn = get_connection()
    campos = []
    params = []

    if valor is not None:
        campos.append("valor = ?")
        params.append(valor)

    if data_vencimento is not None:
        campos.append("data_vencimento = ?")
        params.append(data_vencimento.isoformat())

    if status is not None:
        campos.append("status = ?")
        params.append(status)

    if not campos:
        conn.close()
        return

    params.append(boleto_id)
    conn.execute(
        f"UPDATE boletos SET {', '.join(campos)} WHERE id = ?",
        params,
    )
    conn.commit()
    conn.close()


def excluir_boleto(boleto_id: int) -> None:
    conn = get_connection()
    conn.execute("DELETE FROM boletos WHERE id = ?", (boleto_id,))
    conn.commit()
    conn.close()


def calcular_lote_atual() -> tuple[date, date]:
    hoje = date.today()
    inicio_semana = hoje - timedelta(days=hoje.weekday())
    lote_inicio = inicio_semana - timedelta(days=2)
    lote_fim = inicio_semana + timedelta(days=11)
    return lote_inicio, lote_fim


def listar_boletos(
    estabelecimento_id: int | None = None,
    status: str | None = None,
    data_inicio: date | None = None,
    data_fim: date | None = None,
) -> list[sqlite3.Row]:
    query = """
        SELECT b.id, b.estabelecimento_id, e.nome AS estabelecimento, b.caminho_arquivo,
               b.valor, b.data_vencimento, b.data_envio, b.status
        FROM boletos b
        INNER JOIN estabelecimentos e ON e.id = b.estabelecimento_id
    """

    filtros = []
    params = []

    if estabelecimento_id is not None:
        filtros.append("b.estabelecimento_id = ?")
        params.append(estabelecimento_id)

    if status:
        filtros.append("b.status = ?")
        params.append(status)

    if data_inicio:
        filtros.append("b.data_vencimento >= ?")
        params.append(data_inicio.isoformat())

    if data_fim:
        filtros.append("b.data_vencimento <= ?")
        params.append(data_fim.isoformat())

    if filtros:
        query += " WHERE " + " AND ".join(filtros)

    query += " ORDER BY b.data_vencimento ASC, e.nome ASC"

    conn = get_connection()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def listar_boletos_do_lote() -> list[sqlite3.Row]:
    lote_inicio, lote_fim = calcular_lote_atual()
    return listar_boletos(data_inicio=lote_inicio, data_fim=lote_fim)


def listar_boletos_urgentes_hoje() -> list[sqlite3.Row]:
    hoje = date.today()
    return listar_boletos(data_inicio=hoje, data_fim=hoje)


def criar_pagina_divisoria(estabelecimento: str) -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    c.setFillColorRGB(0.12, 0.12, 0.12)
    c.setFont("Helvetica-Bold", 20)
    c.drawCentredString(width / 2, height / 2, estabelecimento)

    c.showPage()
    c.save()
    return buffer.getvalue()


def gerar_pdf_agrupado(rows: list[sqlite3.Row], output_path: Path) -> Path:
    if not rows:
        raise ValueError("Nenhum boleto foi encontrado para gerar o PDF.")

    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[row["estabelecimento"]].append(row)

    writer = PdfWriter()

    for estabelecimento in grouped:
        divisoria = PdfReader(io.BytesIO(criar_pagina_divisoria(estabelecimento))).pages[0]
        writer.add_page(divisoria)

        for row in grouped[estabelecimento]:
            boleto_path = Path(row["caminho_arquivo"])
            if boleto_path.exists():
                boleto_reader = PdfReader(str(boleto_path))
                for page in boleto_reader.pages:
                    writer.add_page(page)

    with output_path.open("wb") as output_file:
        writer.write(output_file)

    return output_path


def gerar_lote_impressao_pdf() -> Path:
    return gerar_pdf_agrupado(listar_boletos_do_lote(), LOTE_OUTPUT_PATH)


def gerar_pdf_boletos_urgentes() -> Path:
    return gerar_pdf_agrupado(listar_boletos_urgentes_hoje(), URGENTES_OUTPUT_PATH)


def format_currency(valor: float) -> str:
    return f"R$ {float(valor):,.2f}"


def render_login() -> None:
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

    st.markdown('<div class="login-wrapper">', unsafe_allow_html=True)
    st.markdown(
        """
        <div class="login-card">
            <span class="login-badge">Portal Financeiro</span>
            <h1 class="login-title">Acesso ao Sistema</h1>
            <p class="login-sub">Digite suas credenciais para continuar</p>
        """,
        unsafe_allow_html=True,
    )

    with st.form("login_form_custom", clear_on_submit=False):
        login = st.text_input("Usuário", placeholder="ex: gerente1")
        senha = st.text_input("Senha", type="password", placeholder="••••••••")
        submitted = st.form_submit_button("Entrar no Sistema")

    if submitted:
        user = authenticate_user(login, senha)
        if user:
            st.session_state.authenticated = True
            st.session_state.user = {
                "id": user["id"],
                "login": user["login"],
                "perfil": user["perfil"],
                "estabelecimento_id": user["estabelecimento_id"],
            }
            st.rerun()
        else:
            st.error("Usuário ou senha incorretos.")

    st.markdown('</div></div>', unsafe_allow_html=True)


def render_manager_view(user: dict) -> None:
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
    st.markdown(
        f"""
        <div class="app-header">
            <div>
                <div class="header-brand">Gestão de Contas a Pagar</div>
            </div>
            <span class="header-tag">{obter_estabelecimento_nome(user['estabelecimento_id'])}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    tabs = st.tabs(["➕ Novo Envio", "📋 Minhas Contas Enviadas"])

    with tabs[0]:
        estabelecimento_nome = obter_estabelecimento_nome(user["estabelecimento_id"])
        st.markdown('<div class="section-card">', unsafe_allow_html=True)
        st.markdown('<div class="section-title">Novo cadastro</div>', unsafe_allow_html=True)

        uploaded_file = st.file_uploader(
            "Selecione um PDF, PNG ou JPG",
            type=["pdf", "png", "jpg", "jpeg"],
            key="manager_upload",
        )
        camera_photo = st.camera_input("Ou tire uma foto", key="manager_camera")

        col1, col2 = st.columns(2)
        with col1:
            valor = st.number_input("Valor (R$)", min_value=0.0, step=0.01, format="%.2f")
        with col2:
            data_vencimento = st.date_input("Data de Vencimento", min_value=date.today())

        if data_vencimento == date.today():
            mensagem = (
                f"🚨 *URGENTE: BOLETO COM VENCIMENTO PARA HOJE!* 🚨%0A%0A"
                f"*Estabelecimento:* {estabelecimento_nome}%0A"
                f"*Valor:* R$ {valor:.2f}%0A"
                f"*Data:* HOJE%0A%0A"
                "_Por favor, realize o pagamento com prioridade. O boleto já está disponível no sistema._"
            )
            url_whatsapp = f"https://web.whatsapp.com/send?text={quote(mensagem)}"
            st.markdown(
                f"""
                <div class="whatsapp-banner">
                    <div class="whatsapp-banner__text">🚨 Boletos vencendo hoje exigem atenção imediata.</div>
                    <a href="{url_whatsapp}" target="_blank" class="btn-whatsapp-touch">💬 Abrir WhatsApp Web</a>
                </div>
                """,
                unsafe_allow_html=True,
            )

        if st.button("Salvar boleto", key="salvar_boleto_gerente"):
            arquivo_final = uploaded_file or camera_photo
            if arquivo_final is None:
                st.error("Selecione um arquivo ou tire uma foto antes de salvar.")
            else:
                try:
                    caminho_arquivo = save_attachment(
                        arquivo_final,
                        getattr(arquivo_final, "name", f"boleto_{user['estabelecimento_id']}"),
                    )
                    salvar_boleto(
                        estabelecimento_id=user["estabelecimento_id"],
                        caminho_arquivo=caminho_arquivo,
                        valor=valor,
                        data_vencimento=data_vencimento,
                    )
                    st.success("Boleto salvo com sucesso!")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Erro ao salvar o boleto: {exc}")

        st.markdown('</div>', unsafe_allow_html=True)

    with tabs[1]:
        rows = listar_boletos(estabelecimento_id=user["estabelecimento_id"])

        if not rows:
            st.info("Você ainda não possui boletos enviados.")
        else:
            for row in rows:
                status_class = row["status"].lower().replace(" ", "-")
                st.markdown(
                    f"""
                    <div class="boleto-card">
                        <div class="boleto-card__header">
                            <h4 class="boleto-card__title">{row['estabelecimento']}</h4>
                            <span class="status-pill status-{status_class}">{row['status'].upper()}</span>
                        </div>
                        <div class="boleto-card__meta">
                            <div class="meta-box">
                                <span class="meta-label">Valor</span>
                                <div class="meta-value value-highlight">{format_currency(row['valor'])}</div>
                            </div>
                            <div class="meta-box">
                                <span class="meta-label">Vencimento</span>
                                <div class="meta-value">{row['data_vencimento']}</div>
                            </div>
                            <div class="meta-box" style="grid-column: 1 / -1;">
                                <span class="meta-label">Arquivo</span>
                                <div class="meta-value">{Path(row['caminho_arquivo']).name}</div>
                            </div>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

                edit_id = st.session_state.get("selected_edit_id")
                if edit_id == row["id"]:
                    with st.form(f"form_edit_manager_{row['id']}"):
                        valor_edit = st.number_input(
                            "Valor (R$)",
                            value=float(row["valor"]),
                            min_value=0.0,
                            step=0.01,
                            format="%.2f",
                            key=f"edit_valor_{row['id']}",
                        )
                        vencimento_edit = st.date_input(
                            "Data de vencimento",
                            value=parse_date(row["data_vencimento"]),
                            key=f"edit_vencimento_{row['id']}",
                        )

                        st.form_submit_button("Salvar alterações")

                        if st.session_state.get(f"submit_edit_manager_{row['id']}"):
                            atualizar_boleto(row["id"], valor=valor_edit, data_vencimento=vencimento_edit)
                            st.session_state.selected_edit_id = None
                            st.rerun()

                    st.caption("A edição fica disponível apenas quando o boleto ainda está pendente.")
                else:
                    if row["status"] == "Pendente":
                        col1, col2 = st.columns(2)
                        with col1:
                            if st.button("✏️ Editar", key=f"btn_edit_manager_{row['id']}"):
                                st.session_state.selected_edit_id = row["id"]
                                st.rerun()
                        with col2:
                            if st.button("🗑️ Excluir", key=f"btn_delete_manager_{row['id']}"):
                                excluir_boleto(row["id"])
                                st.success("Boleto excluído com sucesso.")
                                st.rerun()
                    else:
                        st.info("Este boleto não pode ser editado ou excluído porque já saiu do status pendente.")


def render_finance_view(user: dict) -> None:
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
    st.markdown(
        """
        <div class="app-header">
            <div>
                <div class="header-brand">Gestão de Contas a Pagar</div>
            </div>
            <span class="header-tag">Financeiro</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    lote_inicio, lote_fim = calcular_lote_atual()
    rows_lote = listar_boletos_do_lote()
    urgent_rows = listar_boletos_urgentes_hoje()
    total_pendente = len(listar_boletos(status="Pendente"))

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Total do Lote (R$)", format_currency(sum(float(row["valor"]) for row in rows_lote)))
    with col2:
        st.metric("Contas Urgentes Hoje (Qtd)", len(urgent_rows))
    with col3:
        st.metric("Contas Pendentes", total_pendente)

    tabs = st.tabs(["🚨 Urgentes do Dia (HOJE)", "🗓️ Lote da Semana (Sábado a Sexta)", "📊 Histórico e Status"])

    with tabs[0]:
        if not urgent_rows:
            st.warning("Nenhum boleto urgente foi encontrado para hoje.")
        else:
            st.markdown('<div class="section-card">', unsafe_allow_html=True)
            st.markdown('<div class="section-title">Boletos urgentes disponíveis para pagamento imediato</div>', unsafe_allow_html=True)

            for row in urgent_rows:
                status_class = row["status"].lower().replace(" ", "-")
                st.markdown(
                    f"""
                    <div class="boleto-card boleto-card--urgent">
                        <div class="boleto-card__header">
                            <h4 class="boleto-card__title">{row['estabelecimento']}</h4>
                            <span class="status-pill status-{status_class}">{row['status'].upper()}</span>
                        </div>
                        <div class="boleto-card__meta">
                            <div class="meta-box">
                                <span class="meta-label">Valor</span>
                                <div class="meta-value value-highlight">{format_currency(row['valor'])}</div>
                            </div>
                            <div class="meta-box">
                                <span class="meta-label">Vencimento</span>
                                <div class="meta-value">{row['data_vencimento']}</div>
                            </div>
                            <div class="meta-box" style="grid-column: 1 / -1;">
                                <span class="meta-label">Arquivo</span>
                                <div class="meta-value">{Path(row['caminho_arquivo']).name}</div>
                            </div>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

                col1, col2, col3 = st.columns(3)
                with col1:
                    if st.button("✅ Pago", key=f"urgent_pago_{row['id']}"):
                        atualizar_boleto(row["id"], status="Pago")
                        st.rerun()
                with col2:
                    if st.button("📦 Em Lote", key=f"urgent_lote_{row['id']}"):
                        atualizar_boleto(row["id"], status="Em Lote")
                        st.rerun()
                with col3:
                    if st.button("🗑️ Excluir", key=f"urgent_delete_{row['id']}"):
                        excluir_boleto(row["id"])
                        st.success("Boleto excluído.")
                        st.rerun()

            st.markdown('</div>', unsafe_allow_html=True)

            if st.button("🖨️ Gerar PDF de Boletos Urgentes (Hoje)", key="btn_generate_urgent"):
                try:
                    urgent_path = gerar_pdf_boletos_urgentes()
                    with urgent_path.open("rb") as file:
                        st.download_button(
                            label="Download do PDF de Boletos Urgentes (Hoje)",
                            data=file.read(),
                            file_name="boletos_urgentes_hoje.pdf",
                            mime="application/pdf",
                        )
                    st.success(f"Arquivo gerado: {urgent_path}")
                except Exception as exc:
                    st.error(f"Erro ao gerar o PDF urgente: {exc}")

    with tabs[1]:
        if not rows_lote:
            st.info("Nenhum boleto encontrado para o lote atual.")
        else:
            st.markdown('<div class="section-card">', unsafe_allow_html=True)
            st.markdown(f'<div class="section-title">Período do Lote Atual: {lote_inicio} até {lote_fim}</div>', unsafe_allow_html=True)
            for row in rows_lote:
                status_class = row["status"].lower().replace(" ", "-")
                st.markdown(
                    f"""
                    <div class="boleto-card">
                        <div class="boleto-card__header">
                            <h4 class="boleto-card__title">{row['estabelecimento']}</h4>
                            <span class="status-pill status-{status_class}">{row['status'].upper()}</span>
                        </div>
                        <div class="boleto-card__meta">
                            <div class="meta-box">
                                <span class="meta-label">Valor</span>
                                <div class="meta-value value-highlight">{format_currency(row['valor'])}</div>
                            </div>
                            <div class="meta-box">
                                <span class="meta-label">Vencimento</span>
                                <div class="meta-value">{row['data_vencimento']}</div>
                            </div>
                            <div class="meta-box" style="grid-column: 1 / -1;">
                                <span class="meta-label">Arquivo</span>
                                <div class="meta-value">{Path(row['caminho_arquivo']).name}</div>
                            </div>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

                col1, col2, col3 = st.columns(3)
                with col1:
                    if st.button("✅ Pago", key=f"lote_pago_{row['id']}"):
                        atualizar_boleto(row["id"], status="Pago")
                        st.rerun()
                with col2:
                    if st.button("📦 Em Lote", key=f"lote_lote_{row['id']}"):
                        atualizar_boleto(row["id"], status="Em Lote")
                        st.rerun()
                with col3:
                    if st.button("🗑️ Excluir", key=f"lote_delete_{row['id']}"):
                        excluir_boleto(row["id"])
                        st.success("Boleto excluído.")
                        st.rerun()

            st.markdown('</div>', unsafe_allow_html=True)

            if st.button("🖨️ Gerar lote para impressão", key="btn_generate_lote"):
                try:
                    lote_path = gerar_lote_impressao_pdf()
                    with lote_path.open("rb") as file:
                        st.download_button(
                            label="Download do lote_impressao.pdf",
                            data=file.read(),
                            file_name="lote_impressao.pdf",
                            mime="application/pdf",
                        )
                    st.success(f"Arquivo gerado: {lote_path}")
                except Exception as exc:
                    st.error(f"Erro ao gerar o lote PDF: {exc}")

    with tabs[2]:
        st.markdown('<div class="section-card">', unsafe_allow_html=True)
        st.markdown('<div class="section-title">Histórico e Status</div>', unsafe_allow_html=True)

        filtro_status = st.selectbox("Filtrar por Status", options=["Todos", *STATUSES], index=0)
        col1, col2 = st.columns(2)
        with col1:
            filtro_inicio = st.date_input("Data inicial")
        with col2:
            filtro_fim = st.date_input("Data final")

        rows_hist = listar_boletos(
            status=None if filtro_status == "Todos" else filtro_status,
            data_inicio=filtro_inicio,
            data_fim=filtro_fim,
        )

        if not rows_hist:
            st.info("Nenhum boleto encontrado com os filtros selecionados.")
        else:
            for row in rows_hist:
                status_class = row["status"].lower().replace(" ", "-")
                st.markdown(
                    f"""
                    <div class="boleto-card">
                        <div class="boleto-card__header">
                            <h4 class="boleto-card__title">{row['estabelecimento']}</h4>
                            <span class="status-pill status-{status_class}">{row['status'].upper()}</span>
                        </div>
                        <div class="boleto-card__meta">
                            <div class="meta-box">
                                <span class="meta-label">Valor</span>
                                <div class="meta-value value-highlight">{format_currency(row['valor'])}</div>
                            </div>
                            <div class="meta-box">
                                <span class="meta-label">Vencimento</span>
                                <div class="meta-value">{row['data_vencimento']}</div>
                            </div>
                            <div class="meta-box" style="grid-column: 1 / -1;">
                                <span class="meta-label">Arquivo</span>
                                <div class="meta-value">{Path(row['caminho_arquivo']).name}</div>
                            </div>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

                edit_id = st.session_state.get("selected_finance_edit_id")
                if edit_id == row["id"]:
                    with st.form(f"form_edit_finance_{row['id']}"):
                        valor_edit = st.number_input(
                            "Valor (R$)",
                            value=float(row["valor"]),
                            min_value=0.0,
                            step=0.01,
                            format="%.2f",
                            key=f"finance_edit_valor_{row['id']}",
                        )
                        vencimento_edit = st.date_input(
                            "Data de vencimento",
                            value=parse_date(row["data_vencimento"]),
                            key=f"finance_edit_vencimento_{row['id']}",
                        )
                        status_edit = st.selectbox(
                            "Status",
                            options=STATUSES,
                            index=STATUSES.index(row["status"]),
                            key=f"finance_edit_status_{row['id']}",
                        )

                        col1, col2 = st.columns(2)
                        with col1:
                            if st.form_submit_button("Salvar alterações"):
                                atualizar_boleto(row["id"], valor=valor_edit, data_vencimento=vencimento_edit, status=status_edit)
                                st.session_state.selected_finance_edit_id = None
                                st.success("Boleto atualizado.")
                                st.rerun()
                        with col2:
                            if st.button("Cancelar", key=f"cancel_edit_fin_{row['id']}"):
                                st.session_state.selected_finance_edit_id = None
                                st.rerun()
                else:
                    col1, col2, col3, col4 = st.columns(4)
                    with col1:
                        if st.button("✅ Pago", key=f"hist_pago_{row['id']}"):
                            atualizar_boleto(row["id"], status="Pago")
                            st.rerun()
                    with col2:
                        if st.button("📦 Em Lote", key=f"hist_lote_{row['id']}"):
                            atualizar_boleto(row["id"], status="Em Lote")
                            st.rerun()
                    with col3:
                        if st.button("✏️ Editar", key=f"hist_edit_{row['id']}"):
                            st.session_state.selected_finance_edit_id = row["id"]
                            st.rerun()
                    with col4:
                        if st.button("🗑️ Excluir", key=f"hist_delete_{row['id']}"):
                            excluir_boleto(row["id"])
                            st.success("Boleto excluído.")
                            st.rerun()

        st.markdown('</div>', unsafe_allow_html=True)


def main() -> None:
    st.set_page_config(page_title="Gestão de Contas a Pagar", layout="wide")
    init_db()

    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

    if st.sidebar.button("Sair / Logout"):
        st.session_state.authenticated = False
        st.session_state.pop("user", None)
        st.session_state.pop("selected_edit_id", None)
        st.session_state.pop("selected_finance_edit_id", None)
        st.rerun()

    if not st.session_state.get("authenticated"):
        render_login()
        return

    user = st.session_state.get("user")
    if user is None:
        st.session_state.authenticated = False
        st.rerun()

    if user["perfil"] == "gerente":
        render_manager_view(user)
    else:
        render_finance_view(user)


if __name__ == "__main__":
    main()
