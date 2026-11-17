from __future__ import annotations

import os
import sqlite3
import urllib.request
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
            status TEXT NOT NULL DEFAULT 'Pendente',
            linha_digitavel TEXT,
            pago_em TEXT,
            pago_por TEXT,
            fornecedor TEXT,
            criado_por TEXT
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
            ("fornecedor", "TEXT"),
            ("criado_por", "TEXT"),
        ]:
            cur.execute(
                f"ALTER TABLE boletos ADD COLUMN IF NOT EXISTS {col} {definition}"
            )
        # Recria o CHECK de perfil de forma segura (IF NOT EXISTS não existe no ADD CONSTRAINT do PG)
        cur.execute("""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'usuarios_perfil_check') THEN
                    ALTER TABLE usuarios DROP CONSTRAINT usuarios_perfil_check;
                END IF;
                ALTER TABLE usuarios ADD CONSTRAINT usuarios_perfil_check
                    CHECK (perfil IN ('gerente', 'financeiro', 'dono', 'admin'));
            END $$;
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
            ("fornecedor", "TEXT"),
            ("criado_por", "TEXT"),
        ]:
            if col not in cols:
                conn.execute(f"ALTER TABLE boletos ADD COLUMN {col} {definition}")
        conn.execute("UPDATE boletos SET status='Pendente' WHERE status IS NULL")

    # ── importação automática de e-mails ──────────────────────────────────
    if _USE_PG:
        cur.execute("ALTER TABLE estabelecimentos ADD COLUMN IF NOT EXISTS cnpj TEXT")
    else:
        est_cols = [r["name"] for r in conn.execute("PRAGMA table_info(estabelecimentos)").fetchall()]
        if "cnpj" not in est_cols:
            conn.execute("ALTER TABLE estabelecimentos ADD COLUMN cnpj TEXT")

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS importacoes (
            id {serial} {'PRIMARY KEY' if _USE_PG else ''},
            chave TEXT NOT NULL UNIQUE,
            message_id TEXT,
            remetente TEXT,
            assunto TEXT,
            recebido_em TEXT,
            arquivo_nome TEXT,
            arquivo_url TEXT,
            estabelecimento_id INTEGER,
            fornecedor TEXT,
            valor REAL,
            data_vencimento TEXT,
            linha_digitavel TEXT,
            codigo_valido INTEGER DEFAULT 0,
            usou_ia INTEGER DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'revisao',
            motivo TEXT,
            boleto_id INTEGER,
            criado_em TEXT NOT NULL,
            resolvido_por TEXT,
            resolvido_em TEXT,
            pagador_nome TEXT,
            pagador_cnpj TEXT
        )
    """)
    if _USE_PG:
        for col in ("pagador_nome", "pagador_cnpj"):
            cur.execute(f"ALTER TABLE importacoes ADD COLUMN IF NOT EXISTS {col} TEXT")
    else:
        imp_cols = [r["name"] for r in conn.execute("PRAGMA table_info(importacoes)").fetchall()]
        for col in ("pagador_nome", "pagador_cnpj"):
            if col not in imp_cols:
                conn.execute(f"ALTER TABLE importacoes ADD COLUMN {col} TEXT")

    conn.commit()
    conn.close()

    # Executado sempre, independente de o schema já existir
    garantir_usuario_dono()


# ── garantia do usuário dono ─────────────────────────────────────────────────

def garantir_usuario_dono() -> None:
    """Executado no boot: garante que o pedro exista com perfil 'dono'.

    Se o usuário já existe, só corrige o perfil — NÃO mexe na senha. (Antes a
    senha era resetada para a padrão a cada reinício do Render, desfazendo a
    troca feita em /financeiro/admin.) A senha padrão só é usada na criação.
    """
    import hashlib
    senha_hash = hashlib.sha256(os.getenv("DONO_SENHA_INICIAL", "pedro123").encode()).hexdigest()
    ph = "%s" if _USE_PG else "?"
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(f"SELECT id FROM usuarios WHERE login={ph}", ("pedro",))
        existe = cur.fetchone()
        if existe:
            cur.execute(
                f"UPDATE usuarios SET perfil={ph} WHERE login={ph}",
                ("dono", "pedro"),
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
    cur.execute("SELECT id, nome, cnpj FROM estabelecimentos ORDER BY id")
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
               b.linha_digitavel, b.pago_em, b.pago_por,
               b.fornecedor, b.criado_por
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
    fornecedor: str | None = None,
    criado_por: str | None = None,
) -> int:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""INSERT INTO boletos
                (estabelecimento_id, arquivo_nome, arquivo_url,
                 valor, data_vencimento, data_envio, status, linha_digitavel,
                 fornecedor, criado_por)
                VALUES ({_ph(10)})""",
            (
                estabelecimento_id, arquivo_nome, arquivo_url,
                valor, data_vencimento.isoformat(), date.today().isoformat(),
                "Pendente", linha_digitavel,
                (fornecedor or "").strip() or None, criado_por,
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
    fornecedor: str | None = None,
) -> None:
    ph = "%s" if _USE_PG else "?"
    campos, params = [], []
    if fornecedor is not None:
        campos.append(f"fornecedor = {ph}")
        params.append(fornecedor.strip() or None)
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
                   b.fornecedor, b.criado_por,
                   e.nome AS estabelecimento
            FROM boletos b JOIN estabelecimentos e ON e.id=b.estabelecimento_id
            WHERE b.id={ph}""",
        (boleto_id,),
    )
    row = _fetchone(cur)
    _release(conn)
    return row


# ── arquivo do boleto (bytes) ────────────────────────────────────────────────

_DOWNLOAD_TIMEOUT = 20            # segundos por arquivo
_DOWNLOAD_MAX_BYTES = 15 * 1024 * 1024  # protege os 512 MB do Render


def _caminho_local(url: str) -> Path | None:
    """Converte '/uploads/arquivo.pdf' (fallback de dev) em caminho no disco."""
    if not url or not url.startswith("/uploads/"):
        return None
    p = (BASE_DIR / "uploads" / url.split("/uploads/", 1)[1]).resolve()
    if (BASE_DIR / "uploads").resolve() not in p.parents:
        return None
    return p


def get_boleto_arquivo(boleto_id: int) -> bytes | None:
    """Baixa os bytes do arquivo do boleto (Supabase Storage ou disco local).

    Nunca levanta exceção: em qualquer falha (rede, 404, arquivo grande demais,
    arquivo ausente) registra no log e devolve None, para que o lote continue.
    """
    try:
        b = get_boleto_by_id(boleto_id)
    except Exception as exc:
        print(f"[arquivo] boleto #{boleto_id}: erro ao consultar banco: {exc}", flush=True)
        return None
    if not b:
        return None

    url = (b.get("arquivo_url") or "").strip()

    local = _caminho_local(url)
    if local is not None:
        try:
            return local.read_bytes() if local.is_file() else None
        except OSError as exc:
            print(f"[arquivo] boleto #{boleto_id}: erro lendo disco: {exc}", flush=True)
            return None

    if url.startswith(("http://", "https://")):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "FluxoPay/1.0"})
            with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT) as resp:
                dados = resp.read(_DOWNLOAD_MAX_BYTES + 1)
            if len(dados) > _DOWNLOAD_MAX_BYTES:
                print(f"[arquivo] boleto #{boleto_id}: arquivo maior que o limite, ignorado", flush=True)
                return None
            return dados
        except Exception as exc:
            print(f"[arquivo] boleto #{boleto_id}: falha ao baixar ({type(exc).__name__}: {exc})", flush=True)
    return None


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
    writer.writerow(["Loja", "Fornecedor", "Arquivo", "Valor", "Vencimento", "Status",
                     "Lançado Por", "Pago Em", "Pago Por", "Linha Digitável"])
    for r in rows:
        writer.writerow([
            r.get("estabelecimento", ""),
            r.get("fornecedor", "") or "",
            r.get("arquivo_nome", ""),
            f"{float(r['valor']):.2f}",
            r.get("data_vencimento", ""),
            r.get("status", ""),
            r.get("criado_por", "") or "",
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


# ── CNPJ das lojas ───────────────────────────────────────────────────────────

def atualizar_cnpj_loja(estabelecimento_id: int, cnpj: str | None) -> None:
    import re as _re
    digitos = _re.sub(r"\D", "", cnpj or "") or None
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"UPDATE estabelecimentos SET cnpj={_ph()} WHERE id={_ph()}", (digitos, estabelecimento_id))
    _commit_close(conn)


# ── importações (e-mail → boleto) ────────────────────────────────────────────

IMPORTACAO_STATUS = ("importado", "revisao", "duplicado", "descartado", "ignorado")


def get_estabelecimento(estabelecimento_id: int) -> dict | None:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"SELECT id, nome, cnpj FROM estabelecimentos WHERE id={_ph()}", (estabelecimento_id,))
    row = _fetchone(cur)
    _release(conn)
    return row


def importacao_existe(chave: str) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"SELECT 1 FROM importacoes WHERE chave={_ph()}", (chave,))
    achou = cur.fetchone() is not None
    _release(conn)
    return achou


def registrar_importacao(**campos) -> int:
    campos.setdefault("criado_em", datetime.now().isoformat(timespec="seconds"))
    cols = list(campos)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"INSERT INTO importacoes ({', '.join(cols)}) VALUES ({_ph(len(cols))})",
            [campos[c] for c in cols],
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


def atualizar_importacao(importacao_id: int, **campos) -> None:
    if not campos:
        return
    sets = ", ".join(f"{k}={_ph()}" for k in campos)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"UPDATE importacoes SET {sets} WHERE id={_ph()}", [*campos.values(), importacao_id])
    _commit_close(conn)


def listar_importacoes(status: str | None = None, limite: int = 200) -> list[dict]:
    q = """SELECT i.*, e.nome AS estabelecimento
           FROM importacoes i LEFT JOIN estabelecimentos e ON e.id = i.estabelecimento_id"""
    params: list = []
    if status:
        q += f" WHERE i.status={_ph()}"
        params.append(status)
    q += f" ORDER BY i.id DESC LIMIT {int(limite)}"
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(q, params)
    rows = _fetchall(cur)
    _release(conn)
    return rows


def get_importacao(importacao_id: int) -> dict | None:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM importacoes WHERE id={_ph()}", (importacao_id,))
    row = _fetchone(cur)
    _release(conn)
    return row


def contar_importacoes_revisao() -> int:
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM importacoes WHERE status='revisao'")
        n = cur.fetchone()[0]
        _release(conn)
        return int(n or 0)
    except Exception:
        return 0


def boleto_por_linha(linha_digitavel: str) -> dict | None:
    """Procura boleto já cadastrado com o mesmo código (comparando só dígitos)."""
    import re as _re
    alvo = _re.sub(r"\D", "", linha_digitavel or "")
    if len(alvo) < 44:
        return None
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, linha_digitavel, status FROM boletos WHERE linha_digitavel IS NOT NULL")
    rows = _fetchall(cur)
    _release(conn)
    for r in rows:
        if _re.sub(r"\D", "", r["linha_digitavel"] or "") == alvo:
            return r
    return None
