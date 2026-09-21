from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
raw_data_dir = os.getenv("DATA_DIR")

if raw_data_dir and not raw_data_dir.startswith("/app"):
    DATA_DIR = Path(raw_data_dir)
else:
    DATA_DIR = BASE_DIR / "uploads"

try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except PermissionError:
    DATA_DIR = BASE_DIR / "uploads"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "contas.db"
UPLOADS_DIR = DATA_DIR

STATUSES = ["Pendente", "Em Lote", "Pago"]


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_connection()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS estabelecimentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL UNIQUE
        )
    """)

    if conn.execute("SELECT COUNT(*) FROM estabelecimentos").fetchone()[0] == 0:
        conn.executemany("INSERT INTO estabelecimentos (nome) VALUES (?)", [
            ("GP Conveniencia",), ("Posto Atibaia",), ("WP Auto posto",),
        ])
    else:
        conn.execute("UPDATE estabelecimentos SET nome='GP Conveniencia' WHERE id=1")
        conn.execute("UPDATE estabelecimentos SET nome='Posto Atibaia' WHERE id=2")
        conn.execute("UPDATE estabelecimentos SET nome='WP Auto posto' WHERE id=3")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            estabelecimento_id INTEGER,
            login TEXT NOT NULL UNIQUE,
            senha TEXT NOT NULL,
            perfil TEXT NOT NULL CHECK(perfil IN ('gerente', 'financeiro'))
        )
    """)

    if conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO usuarios (estabelecimento_id, login, senha, perfil) VALUES (?,?,?,?)",
            [
                (1, "gerente1", "senha123", "gerente"),
                (2, "gerente2", "senha123", "gerente"),
                (3, "gerente3", "senha123", "gerente"),
                (None, "financeiro", "admin123", "financeiro"),
            ],
        )

    conn.execute("""
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
    """)

    cols = [r["name"] for r in conn.execute("PRAGMA table_info(boletos)").fetchall()]
    if "status" not in cols:
        conn.execute("ALTER TABLE boletos ADD COLUMN status TEXT DEFAULT 'Pendente'")
        conn.execute("UPDATE boletos SET status='Pendente' WHERE status IS NULL")

    conn.commit()
    conn.close()


def authenticate_user(login: str, senha: str) -> dict | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT id, estabelecimento_id, login, perfil FROM usuarios WHERE login=? AND senha=?",
        (login, senha),
    ).fetchone()
    conn.close()
    if row:
        return dict(row)
    return None


def get_estabelecimento_nome(estabelecimento_id: int | None) -> str:
    if estabelecimento_id is None:
        return "Financeiro"
    conn = get_connection()
    row = conn.execute("SELECT nome FROM estabelecimentos WHERE id=?", (estabelecimento_id,)).fetchone()
    conn.close()
    return row["nome"] if row else ""


def listar_estabelecimentos() -> list[dict]:
    conn = get_connection()
    rows = conn.execute("SELECT id, nome FROM estabelecimentos ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def listar_boletos(
    estabelecimento_id: int | None = None,
    status: str | None = None,
    data_inicio: date | None = None,
    data_fim: date | None = None,
) -> list[dict]:
    query = """
        SELECT b.id, b.estabelecimento_id, e.nome AS estabelecimento,
               b.caminho_arquivo, b.valor, b.data_vencimento, b.data_envio, b.status
        FROM boletos b
        INNER JOIN estabelecimentos e ON e.id = b.estabelecimento_id
    """
    filtros, params = [], []

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
    return [dict(r) for r in rows]


def listar_boletos_por_estabelecimento(
    ano: int | None = None,
    mes: int | None = None,
) -> dict[str, dict]:
    """
    Retorna dict keyed por nome do estabelecimento, cada valor sendo:
      { "id": int, "nome": str, "boletos": [...], "total": float, "qtd": int }
    Filtrado por mês/ano quando fornecidos.
    """
    ests = listar_estabelecimentos()
    resultado: dict[str, dict] = {}
    for est in ests:
        di = date(ano, mes, 1) if (ano and mes) else None
        if di:
            import calendar
            ultimo_dia = calendar.monthrange(ano, mes)[1]
            df = date(ano, mes, ultimo_dia)
        else:
            df = None
        rows = listar_boletos(estabelecimento_id=est["id"], data_inicio=di, data_fim=df)
        resultado[est["nome"]] = {
            "id": est["id"],
            "nome": est["nome"],
            "boletos": rows,
            "total": sum(float(r["valor"]) for r in rows),
            "qtd": len(rows),
        }
    return resultado


def calcular_lote_atual() -> tuple[date, date]:
    """
    Janela do lote de Segunda-feira:
      início = Sábado imediatamente anterior à Segunda (segunda - 2 dias)
      fim    = Sexta-feira da mesma semana da Segunda (segunda + 4 dias)

    weekday(): segunda=0 … domingo=6
    Se hoje for sábado (5) ou domingo (6), a "próxima segunda" é usada
    para que o lote já apareça no fim de semana de preparação.
    """
    hoje = date.today()
    dias_ate_segunda = (7 - hoje.weekday()) % 7 if hoje.weekday() in (5, 6) else hoje.weekday()
    segunda = hoje - timedelta(days=dias_ate_segunda) if hoje.weekday() not in (5, 6) else hoje + timedelta(days=(7 - hoje.weekday()) % 7)
    lote_inicio = segunda - timedelta(days=2)   # Sábado anterior
    lote_fim = segunda + timedelta(days=4)       # Sexta-feira
    return lote_inicio, lote_fim


def listar_boletos_do_lote() -> list[dict]:
    inicio, fim = calcular_lote_atual()
    return listar_boletos(data_inicio=inicio, data_fim=fim)


def listar_boletos_urgentes_hoje() -> list[dict]:
    hoje = date.today()
    return listar_boletos(data_inicio=hoje, data_fim=hoje)


def verificar_duplicata(estabelecimento_id: int, valor: float, data_vencimento: date) -> dict | None:
    """Retorna o boleto existente se houver um ativo com mesmos est+valor+vencimento."""
    conn = get_connection()
    row = conn.execute(
        """SELECT b.id, b.valor, b.data_vencimento, b.status, e.nome AS estabelecimento
           FROM boletos b JOIN estabelecimentos e ON e.id = b.estabelecimento_id
           WHERE b.estabelecimento_id = ? AND b.valor = ? AND b.data_vencimento = ?
             AND b.status != 'Pago'
           LIMIT 1""",
        (estabelecimento_id, valor, data_vencimento.isoformat()),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def salvar_boleto(estabelecimento_id: int, caminho_arquivo: str, valor: float, data_vencimento: date) -> int:
    conn = get_connection()
    cursor = conn.execute(
        "INSERT INTO boletos (estabelecimento_id, caminho_arquivo, valor, data_vencimento, data_envio, status) VALUES (?,?,?,?,?,'Pendente')",
        (estabelecimento_id, caminho_arquivo, valor, data_vencimento.isoformat(), date.today().isoformat()),
    )
    boleto_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return boleto_id


def atualizar_boleto(boleto_id: int, valor: float | None = None, data_vencimento: date | None = None, status: str | None = None) -> None:
    campos, params = [], []
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
        return
    params.append(boleto_id)
    conn = get_connection()
    conn.execute(f"UPDATE boletos SET {', '.join(campos)} WHERE id = ?", params)
    conn.commit()
    conn.close()


def marcar_lote_em_lote(ids: list[int]) -> None:
    """Atualiza o status de Pendente → Em Lote para os ids informados."""
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    conn = get_connection()
    conn.execute(
        f"UPDATE boletos SET status='Em Lote' WHERE id IN ({placeholders}) AND status='Pendente'",
        ids,
    )
    conn.commit()
    conn.close()


def excluir_boleto(boleto_id: int) -> None:
    conn = get_connection()
    conn.execute("DELETE FROM boletos WHERE id = ?", (boleto_id,))
    conn.commit()
    conn.close()


def get_boleto_by_id(boleto_id: int) -> dict | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT b.*, e.nome AS estabelecimento FROM boletos b JOIN estabelecimentos e ON e.id=b.estabelecimento_id WHERE b.id=?",
        (boleto_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_dashboard_stats() -> dict:
    hoje = date.today()
    lote_inicio, lote_fim = calcular_lote_atual()
    rows_lote = listar_boletos_do_lote()
    urgent = listar_boletos_urgentes_hoje()
    pendentes = listar_boletos(status="Pendente")
    pagos = listar_boletos(status="Pago")

    return {
        "total_lote_valor": sum(float(r["valor"]) for r in rows_lote),
        "total_lote_qtd": len(rows_lote),
        "urgentes_hoje": len(urgent),
        "pendentes_qtd": len(pendentes),
        "pagos_qtd": len(pagos),
        "lote_inicio": lote_inicio,
        "lote_fim": lote_fim,
    }
