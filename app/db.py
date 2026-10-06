from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent

DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Supabase Pooler (Supavisor) requer sslmode=require e não suporta
# prepared statements — adiciona ambos se ausentes na URL.
if DATABASE_URL and "supabase" in DATABASE_URL:
    if "sslmode=" not in DATABASE_URL:
        sep = "&" if "?" in DATABASE_URL else "?"
        DATABASE_URL = f"{DATABASE_URL}{sep}sslmode=require"

_USE_PG = bool(DATABASE_URL)

if not _USE_PG:
    _db_path = BASE_DIR / "uploads" / "contas.db"
    _db_path.parent.mkdir(parents=True, exist_ok=True)

# ── Supabase Storage ──────────────────────────────────────────────────────────
_supabase_url = os.getenv("SUPABASE_URL")
_supabase_key = os.getenv("SUPABASE_KEY")
supabase = None
if _supabase_url and _supabase_key:
    try:
        from supabase import create_client
        supabase = create_client(_supabase_url, _supabase_key)
    except Exception as _e:
        print(f"[db] Supabase indisponível: {_e}", flush=True)

SUPABASE_BUCKET = "boletos"

STATUSES = ["Pendente", "Em Lote", "Pago"]


# ── conexão ───────────────────────────────────────────────────────────────────

# Pool de conexões para PostgreSQL: evita usar conexões que o Neon fechou
# por inatividade (principal causa de 502). keepalives detectam conexões mortas
# antes de tentar usá-las; minconn=1/maxconn=5 adequado para free tier.
_pg_pool = None

def _get_pg_pool():
    global _pg_pool
    if _pg_pool is None:
        import psycopg2.pool
        _pg_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=5,
            dsn=DATABASE_URL,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
            # Supavisor não suporta prepared statements
            options="-c statement_timeout=30000",
        )
        # Desativa prepared statements para compatibilidade com Supavisor
        if "supabase" in DATABASE_URL:
            import psycopg2.extensions
            _pg_pool._pool  # força inicialização
    return _pg_pool


def _new_pg_conn_direct():
    """Conexão direta sem pool — usada em init_db para evitar deadlock no boot."""
    import psycopg2
    conn = psycopg2.connect(DATABASE_URL)
    if "supabase" in DATABASE_URL:
        conn.autocommit = False
    return conn


def _pg_conn():
    pool = _get_pg_pool()
    conn = pool.getconn()
    # Valida se a conexão ainda está viva; se não, reconecta automaticamente
    try:
        conn.cursor().execute("SELECT 1")
    except Exception:
        pool.putconn(conn, close=True)
        conn = pool.getconn()
    return conn


def _pg_putconn(conn, close: bool = False) -> None:
    try:
        _get_pg_pool().putconn(conn, close=close)
    except Exception:
        pass


def get_connection():
    if _USE_PG:
        return _pg_conn()
    conn = sqlite3.connect(str(_db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _ph(n: int = 1) -> str:
    """Placeholder: %s para PG, ? para SQLite."""
    ph = "%s" if _USE_PG else "?"
    return ", ".join([ph] * n)


def _fetchall(cursor) -> list[dict]:
    if _USE_PG:
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]
    return [dict(r) for r in cursor.fetchall()]


def _fetchone(cursor) -> dict | None:
    if _USE_PG:
        row = cursor.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))
    row = cursor.fetchone()
    return dict(row) if row else None


def _release(conn, error: bool = False) -> None:
    """Devolve a conexão ao pool (PG) ou fecha (SQLite)."""
    if _USE_PG:
        if error:
            try:
                conn.rollback()
            except Exception:
                pass
        _pg_putconn(conn, close=error)
    else:
        if not error:
            conn.commit()
        conn.close()


def _commit_close(conn) -> None:
    if _USE_PG:
        conn.commit()
        _pg_putconn(conn)
    else:
        conn.commit()
        conn.close()


# ── inicialização do schema ───────────────────────────────────────────────────

def init_db() -> None:
    try:
        _init_db_inner()
    except Exception as exc:
        # Erro de rede no boot não deve derrubar o Uvicorn.
        # O schema será criado na próxima requisição via get_connection().
        print(f"[db] init_db falhou (será retentado): {exc}", flush=True)


def _init_db_inner() -> None:
    # Usa conexão direta no boot para não bloquear o pool durante startup.
    conn = _new_pg_conn_direct() if _USE_PG else get_connection()
    cur = conn.cursor()

    if _USE_PG:
        serial = "SERIAL"
        blob = "BYTEA"
        pk_check = "perfil TEXT NOT NULL CHECK(perfil IN ('gerente', 'financeiro', 'dono'))"
    else:
        serial = "INTEGER PRIMARY KEY AUTOINCREMENT"
        blob = "BLOB"
        pk_check = "perfil TEXT NOT NULL CHECK(perfil IN ('gerente', 'financeiro', 'dono'))"

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS estabelecimentos (
            id {serial} {'PRIMARY KEY' if _USE_PG else ''},
            nome TEXT NOT NULL UNIQUE
        )
    """)

    cur.execute("SELECT COUNT(*) FROM estabelecimentos")
    count = cur.fetchone()
    count = count[0] if isinstance(count, tuple) else count[0]
    ph1 = "%s" if _USE_PG else "?"
    if count == 0:
        for nome in ("GP Conveniencia", "Posto Atibaia", "WP Auto posto"):
            cur.execute(f"INSERT INTO estabelecimentos (nome) VALUES ({ph1})", (nome,))
    else:
        cur.execute(f"UPDATE estabelecimentos SET nome={ph1} WHERE id=1", ("GP Conveniencia",))
        cur.execute(f"UPDATE estabelecimentos SET nome={ph1} WHERE id=2", ("Posto Atibaia",))
        cur.execute(f"UPDATE estabelecimentos SET nome={ph1} WHERE id=3", ("WP Auto posto",))

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS usuarios (
            id {serial} {'PRIMARY KEY' if _USE_PG else ''},
            estabelecimento_id INTEGER,
            login TEXT NOT NULL UNIQUE,
            senha TEXT NOT NULL,
            {pk_check}
        )
    """)

    cur.execute("SELECT COUNT(*) FROM usuarios")
    ucount = cur.fetchone()
    ucount = ucount[0] if isinstance(ucount, tuple) else ucount[0]
    if ucount == 0:
        for row in [
            (1, "gerente1", "senha123", "gerente"),
            (2, "gerente2", "senha123", "gerente"),
            (3, "gerente3", "senha123", "gerente"),
            (None, "financeiro", "admin123", "financeiro"),
            (None, "pedro", "pedro123", "dono"),
        ]:
            cur.execute(
                f"INSERT INTO usuarios (estabelecimento_id, login, senha, perfil) VALUES ({_ph(4)})",
                row,
            )
    else:
        pass  # pedro é garantido abaixo, após expansão do CHECK constraint

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS boletos (
            id {serial} {'PRIMARY KEY' if _USE_PG else ''},
            estabelecimento_id INTEGER NOT NULL,
            arquivo_nome TEXT,
            arquivo_url TEXT,
            valor REAL NOT NULL,
            data_vencimento TEXT NOT NULL,
            data_envio TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Pendente'
        )
    """)

    # migração de schema: adiciona colunas ausentes, remove BLOB pesado
    if _USE_PG:
        for col, definition in [
            ("arquivo_nome", "TEXT"),
            ("arquivo_url", "TEXT"),
            ("status", "TEXT DEFAULT 'Pendente'"),
            ("linha_digitavel", "TEXT"),
            ("pago_em", "TEXT"),
            ("pago_por", "TEXT"),
        ]:
            cur.execute(
                f"ALTER TABLE boletos ADD COLUMN IF NOT EXISTS {col} {definition}"
            )
        # Expande CHECK de perfil se necessário (PG não permite ALTER CHECK inline — usa ALTER)
        cur.execute("""
            SELECT conname FROM pg_constraint
            WHERE conrelid='usuarios'::regclass AND contype='c' AND conname LIKE '%perfil%'
        """)
        constraint = cur.fetchone()
        if constraint:
            cname = constraint[0] if isinstance(constraint, tuple) else constraint["conname"]
            cur.execute(f"ALTER TABLE usuarios DROP CONSTRAINT IF EXISTS {cname}")
        cur.execute("""
            ALTER TABLE usuarios ADD CONSTRAINT IF NOT EXISTS usuarios_perfil_check
            CHECK(perfil IN ('gerente','financeiro','dono'))
        """)
        # remove coluna BLOB se ainda existir (migração de versão anterior)
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name='boletos' AND column_name='arquivo_dados'
        """)
        if cur.fetchone():
            cur.execute("ALTER TABLE boletos DROP COLUMN arquivo_dados")
            cur.execute("ALTER TABLE boletos DROP COLUMN IF EXISTS arquivo_tipo")
    else:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(boletos)").fetchall()]
        for col, definition in [
            ("arquivo_nome", "TEXT"),
            ("arquivo_url", "TEXT"),
            ("status", "TEXT DEFAULT 'Pendente'"),
            ("linha_digitavel", "TEXT"),
            ("pago_em", "TEXT"),
            ("pago_por", "TEXT"),
        ]:
            if col not in cols:
                conn.execute(f"ALTER TABLE boletos ADD COLUMN {col} {definition}")
        conn.execute("UPDATE boletos SET status='Pendente' WHERE status IS NULL")

    conn.commit()
    conn.close()

    # Executado sempre, independente de o schema já existir
    garantir_usuario_dono()


# ── garantia do usuário dono ─────────────────────────────────────────────────

def garantir_usuario_dono() -> None:
    """Sempre executado no boot: UPSERT do pedro com perfil dono e hash correto."""
    import hashlib
    senha_hash = hashlib.sha256("pedro123".encode()).hexdigest()
    ph = "%s" if _USE_PG else "?"
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(f"SELECT id FROM usuarios WHERE login={ph}", ("pedro",))
        existe = cur.fetchone()
        if existe:
            cur.execute(
                f"UPDATE usuarios SET senha={ph}, perfil={ph} WHERE login={ph}",
                (senha_hash, "dono", "pedro"),
            )
        else:
            cur.execute(
                f"INSERT INTO usuarios (estabelecimento_id, login, senha, perfil) VALUES ({_ph(4)})",
                (None, "pedro", senha_hash, "dono"),
            )
        _commit_close(conn)
        print("Usuário Pedro garantido no banco de dados com sucesso.", flush=True)
    except Exception as exc:
        print(f"[db] garantir_usuario_dono falhou: {exc}", flush=True)


# ── autenticação ─────────────────────────────────────────────────────────────

def authenticate_user(login: str, senha: str) -> dict | None:
    import hashlib
    login = login.strip().lower()
    senha_hash = hashlib.sha256(senha.encode()).hexdigest()
    conn = get_connection()
    cur = conn.cursor()
    # Aceita senha em texto plano (legado) ou SHA-256 (novos gerentes)
    cur.execute(
        f"""SELECT id, estabelecimento_id, login, perfil FROM usuarios
            WHERE login={_ph()} AND (senha={_ph()} OR senha={_ph()})""",
        (login, senha, senha_hash),
    )
    row = _fetchone(cur)
    _release(conn)
    return row


# ── estabelecimentos ──────────────────────────────────────────────────────────

def get_estabelecimento_nome(estabelecimento_id: int | None) -> str:
    if estabelecimento_id is None:
        return "Financeiro"
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"SELECT nome FROM estabelecimentos WHERE id={_ph()}", (estabelecimento_id,))
    row = _fetchone(cur)
    _release(conn)
    return row["nome"] if row else ""


def listar_estabelecimentos() -> list[dict]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, nome FROM estabelecimentos ORDER BY id")
    rows = _fetchall(cur)
    _release(conn)
    return rows


def listar_usuarios_gerentes() -> list[dict]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """SELECT u.id, u.login, u.perfil, u.estabelecimento_id, e.nome AS estabelecimento
           FROM usuarios u
           LEFT JOIN estabelecimentos e ON e.id = u.estabelecimento_id
           WHERE u.perfil = 'gerente'
           ORDER BY e.nome, u.login"""
    )
    rows = _fetchall(cur)
    _release(conn)
    return rows


def upsert_usuario_dono(login: str, senha: str) -> None:
    """Cria ou atualiza um usuário com perfil 'dono' usando SHA-256."""
    import hashlib
    login = login.strip().lower()
    senha_hash = hashlib.sha256(senha.encode()).hexdigest()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT id FROM usuarios WHERE login={_ph()}", (login,))
        existe = cur.fetchone()
        if existe:
            cur.execute(
                f"UPDATE usuarios SET senha={_ph()}, perfil={_ph()} WHERE login={_ph()}",
                (senha_hash, "dono", login),
            )
        else:
            cur.execute(
                f"INSERT INTO usuarios (estabelecimento_id, login, senha, perfil) VALUES ({_ph(4)})",
                (None, login, senha_hash, "dono"),
            )
        _commit_close(conn)
    except Exception:
        _release(conn, error=True)
        raise


def criar_estabelecimento(nome: str) -> int:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"INSERT INTO estabelecimentos (nome) VALUES ({_ph()})", (nome.strip(),))
        if _USE_PG:
            cur.execute("SELECT lastval()")
            novo_id = cur.fetchone()[0]
        else:
            novo_id = cur.lastrowid
        _commit_close(conn)
        return novo_id
    except Exception:
        _release(conn, error=True)
        raise


def criar_gerente(login: str, senha: str, estabelecimento_id: int) -> int:
    import hashlib
    senha_hash = hashlib.sha256(senha.encode()).hexdigest()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""INSERT INTO usuarios (estabelecimento_id, login, senha, perfil)
                VALUES ({_ph(4)})""",
            (estabelecimento_id, login.strip(), senha_hash, "gerente"),
        )
        if _USE_PG:
            cur.execute("SELECT lastval()")
            novo_id = cur.fetchone()[0]
        else:
            novo_id = cur.lastrowid
        _commit_close(conn)
        return novo_id
    except Exception:
        _release(conn, error=True)
        raise


# ── boletos ───────────────────────────────────────────────────────────────────

def listar_boletos(
    estabelecimento_id: int | None = None,
    status: str | None = None,
    data_inicio: date | None = None,
    data_fim: date | None = None,
) -> list[dict]:
    ph = "%s" if _USE_PG else "?"
    query = """
        SELECT b.id, b.estabelecimento_id, e.nome AS estabelecimento,
               b.arquivo_nome, b.arquivo_url,
               b.valor, b.data_vencimento, b.data_envio, b.status,
               b.linha_digitavel, b.pago_em, b.pago_por
        FROM boletos b
        INNER JOIN estabelecimentos e ON e.id = b.estabelecimento_id
    """
    filtros, params = [], []

    if estabelecimento_id is not None:
        filtros.append(f"b.estabelecimento_id = {ph}")
        params.append(estabelecimento_id)
    if status:
        filtros.append(f"b.status = {ph}")
        params.append(status)
    if data_inicio:
        filtros.append(f"b.data_vencimento >= {ph}")
        params.append(data_inicio.isoformat())
    if data_fim:
        filtros.append(f"b.data_vencimento <= {ph}")
        params.append(data_fim.isoformat())

    if filtros:
        query += " WHERE " + " AND ".join(filtros)
    query += " ORDER BY b.data_vencimento ASC, e.nome ASC"

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(query, params)
    rows = _fetchall(cur)
    _release(conn)
    return rows


def listar_boletos_por_estabelecimento(
    ano: int | None = None,
    mes: int | None = None,
) -> dict[str, dict]:
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
    hoje = date.today()
    dias_ate_segunda = (7 - hoje.weekday()) % 7 if hoje.weekday() in (5, 6) else hoje.weekday()
    segunda = hoje - timedelta(days=dias_ate_segunda) if hoje.weekday() not in (5, 6) else hoje + timedelta(days=(7 - hoje.weekday()) % 7)
    lote_inicio = segunda - timedelta(days=2)
    lote_fim = segunda + timedelta(days=4)
    return lote_inicio, lote_fim


def listar_boletos_do_lote() -> list[dict]:
    inicio, fim = calcular_lote_atual()
    return listar_boletos(data_inicio=inicio, data_fim=fim)


def listar_boletos_urgentes_hoje() -> list[dict]:
    hoje = date.today()
    return listar_boletos(data_inicio=hoje, data_fim=hoje)


def verificar_duplicata(estabelecimento_id: int, valor: float, data_vencimento: date) -> dict | None:
    ph = "%s" if _USE_PG else "?"
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        f"""SELECT b.id, b.valor, b.data_vencimento, b.status, e.nome AS estabelecimento
           FROM boletos b JOIN estabelecimentos e ON e.id = b.estabelecimento_id
           WHERE b.estabelecimento_id = {ph} AND b.valor = {ph} AND b.data_vencimento = {ph}
             AND b.status != {ph}
           LIMIT 1""",
        (estabelecimento_id, valor, data_vencimento.isoformat(), "Pago"),
    )
    row = _fetchone(cur)
    _release(conn)
    return row


def salvar_boleto(
    estabelecimento_id: int,
    arquivo_nome: str,
    arquivo_url: str,
    valor: float,
    data_vencimento: date,
    linha_digitavel: str | None = None,
) -> int:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""INSERT INTO boletos
                (estabelecimento_id, arquivo_nome, arquivo_url,
                 valor, data_vencimento, data_envio, status, linha_digitavel)
                VALUES ({_ph(8)})""",
            (
                estabelecimento_id, arquivo_nome, arquivo_url,
                valor, data_vencimento.isoformat(), date.today().isoformat(),
                "Pendente", linha_digitavel,
            ),
        )
        if _USE_PG:
            cur.execute("SELECT lastval()")
            boleto_id = cur.fetchone()[0]
        else:
            boleto_id = cur.lastrowid
        _commit_close(conn)
        return boleto_id
    except Exception:
        _release(conn, error=True)
        raise


def atualizar_boleto(
    boleto_id: int,
    valor: float | None = None,
    data_vencimento: date | None = None,
    status: str | None = None,
    linha_digitavel: str | None = None,
    pago_em: str | None = None,
    pago_por: str | None = None,
) -> None:
    ph = "%s" if _USE_PG else "?"
    campos, params = [], []
    if valor is not None:
        campos.append(f"valor = {ph}")
        params.append(valor)
    if data_vencimento is not None:
        campos.append(f"data_vencimento = {ph}")
        params.append(data_vencimento.isoformat())
    if status is not None:
        campos.append(f"status = {ph}")
        params.append(status)
    if linha_digitavel is not None:
        campos.append(f"linha_digitavel = {ph}")
        params.append(linha_digitavel)
    if pago_em is not None:
        campos.append(f"pago_em = {ph}")
        params.append(pago_em)
    if pago_por is not None:
        campos.append(f"pago_por = {ph}")
        params.append(pago_por)
    if not campos:
        return
    params.append(boleto_id)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"UPDATE boletos SET {', '.join(campos)} WHERE id = {ph}", params)
    _commit_close(conn)


def marcar_lote_em_lote(ids: list[int]) -> None:
    if not ids:
        return
    ph = "%s" if _USE_PG else "?"
    placeholders = ", ".join([ph] * len(ids))
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        f"UPDATE boletos SET status='Em Lote' WHERE id IN ({placeholders}) AND status='Pendente'",
        ids,
    )
    _commit_close(conn)


def excluir_boleto(boleto_id: int) -> None:
    ph = "%s" if _USE_PG else "?"
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"DELETE FROM boletos WHERE id = {ph}", (boleto_id,))
    _commit_close(conn)


def get_boleto_by_id(boleto_id: int) -> dict | None:
    ph = "%s" if _USE_PG else "?"
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        f"""SELECT b.id, b.estabelecimento_id, b.arquivo_nome, b.arquivo_url,
                   b.valor, b.data_vencimento, b.data_envio, b.status,
                   b.linha_digitavel, b.pago_em, b.pago_por,
                   e.nome AS estabelecimento
            FROM boletos b JOIN estabelecimentos e ON e.id=b.estabelecimento_id
            WHERE b.id={ph}""",
        (boleto_id,),
    )
    row = _fetchone(cur)
    _release(conn)
    return row


def trocar_senha_usuario(login: str, nova_senha: str) -> bool:
    import hashlib
    senha_hash = hashlib.sha256(nova_senha.encode()).hexdigest()
    ph = "%s" if _USE_PG else "?"
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"UPDATE usuarios SET senha={ph} WHERE login={ph}", (senha_hash, login))
    affected = cur.rowcount
    _commit_close(conn)
    return affected > 0


def exportar_boletos_csv(
    estabelecimento_id: int | None = None,
    status: str | None = None,
    data_inicio: date | None = None,
    data_fim: date | None = None,
) -> str:
    import csv, io as _io
    rows = listar_boletos(
        estabelecimento_id=estabelecimento_id,
        status=status,
        data_inicio=data_inicio,
        data_fim=data_fim,
    )
    buf = _io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Loja", "Arquivo", "Valor", "Vencimento", "Status", "Pago Em", "Pago Por", "Linha Digitável"])
    for r in rows:
        writer.writerow([
            r.get("estabelecimento", ""),
            r.get("arquivo_nome", ""),
            f"{float(r['valor']):.2f}",
            r.get("data_vencimento", ""),
            r.get("status", ""),
            r.get("pago_em", "") or "",
            r.get("pago_por", "") or "",
            r.get("linha_digitavel", "") or "",
        ])
    return buf.getvalue()


def get_dashboard_stats() -> dict:
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
