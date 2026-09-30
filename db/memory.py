"""
memory.py — Memória persistente da Larissa (Fase 1)
Banco de dados SQLite local (db/larissa.db)

Não requer servidor externo — ideal para rodar em Windows (C:\\Larissa).
Todas as funções abrem/fecham a própria conexão (thread-safe para FastAPI).
"""

import os
import sqlite3
import requests
from datetime import datetime, timedelta
import pytz

# ==================== CAMINHOS ====================
# Caminho relativo ao próprio arquivo memory.py (funciona em qualquer OS)
_DIR = os.path.dirname(os.path.abspath(__file__))
# Permite apontar para um banco de TESTE via variavel de ambiente
# (LARISSA_DB_PATH). Em producao a variavel nao existe → usa larissa.db normal.
DB_PATH = os.environ.get('LARISSA_DB_PATH') or os.path.join(_DIR, 'larissa.db')
SCHEMA_PATH = os.path.join(_DIR, 'schema.sql')

TZ = pytz.timezone('America/Belem')

# ── TURSO (opcional) ──────────────────────────────────────────────────────────
# Se TURSO_URL e TURSO_AUTH_TOKEN estiverem no .env, usa o banco remoto Turso
# via HTTP API (sem instalar nada extra — só requests).
# Caso contrário, usa SQLite local normalmente.
_TURSO_URL   = os.environ.get('TURSO_URL', '').replace('libsql://', 'https://')
_TURSO_TOKEN = os.environ.get('TURSO_AUTH_TOKEN', '')
_USAR_TURSO  = bool(_TURSO_URL and _TURSO_TOKEN)

# ── Wrapper HTTP para o Turso (emula API do sqlite3) ─────────────────────────

def _turso_val(v):
    """Converte valor Python para formato da API HTTP do Turso."""
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": "1" if v else "0"}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": str(v)}
    return {"type": "text", "value": str(v)}


class _TursoRow:
    """Emula sqlite3.Row: acesso por índice e por chave. Suporta dict()."""
    def __init__(self, keys, values):
        self._keys = list(keys)
        self._values = list(values)
        self._d = dict(zip(self._keys, self._values))

    def keys(self):
        return self._keys

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._d[key]

    def get(self, key, default=None):
        return self._d.get(key, default)

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    # Suporte a dict(row)
    def items(self):
        return self._d.items()

    def __contains__(self, key):
        return key in self._d


class _TursoCursor:
    """Cursor emulado sobre a HTTP API do Turso."""
    def __init__(self, pipeline_url, headers):
        self._url = pipeline_url
        self._headers = headers
        self._rows = []
        self._keys = []
        self.lastrowid = None
        self.rowcount = 0

    def _parse_result(self, result):
        cols = result.get("cols", [])
        self._keys = [c["name"] for c in cols]
        rows_raw = result.get("rows", [])
        self._rows = []
        for row in rows_raw:
            vals = []
            for cell in row:
                t = cell.get("type")
                v = cell.get("value")
                if t == "null" or v is None:
                    vals.append(None)
                elif t == "integer":
                    vals.append(int(v))
                elif t == "float":
                    vals.append(float(v))
                else:
                    vals.append(v)
            self._rows.append(_TursoRow(self._keys, vals))
        self.lastrowid = result.get("last_insert_rowid")
        self.rowcount = result.get("affected_row_count", 0)

    def execute(self, sql, params=None):
        stmt = {"sql": sql}
        if params:
            stmt["args"] = [_turso_val(a) for a in params]
        payload = {"requests": [{"type": "execute", "stmt": stmt}, {"type": "close"}]}
        r = requests.post(self._url, headers=self._headers, json=payload, timeout=30)
        if r.status_code != 200:
            raise Exception(f"Turso HTTP {r.status_code}: {r.text[:300]}")
        resultado = r.json()
        resp = resultado.get("results", [{}])[0]
        if resp.get("type") == "error":
            raise Exception(resp.get("error", {}).get("message", "Erro Turso"))
        self._parse_result(resp.get("response", {}).get("result", {}))
        return self

    def executemany(self, sql, list_params):
        reqs = []
        for params in list_params:
            stmt = {"sql": sql, "args": [_turso_val(a) for a in params]}
            reqs.append({"type": "execute", "stmt": stmt})
        reqs.append({"type": "close"})
        payload = {"requests": reqs}
        r = requests.post(self._url, headers=self._headers, json=payload, timeout=60)
        if r.status_code != 200:
            raise Exception(f"Turso HTTP {r.status_code}: {r.text[:300]}")
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _TursoConn:
    """Conexão emulada com o Turso via HTTP (mesma API do sqlite3)."""
    def __init__(self, url, token):
        self._pipeline = f"{url}/v2/pipeline"
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self.row_factory = None
        self.total_changes = 0

    def cursor(self):
        return _TursoCursor(self._pipeline, self._headers)

    def execute(self, sql, params=None):
        return self.cursor().execute(sql, params)

    def executemany(self, sql, list_params):
        return self.cursor().executemany(sql, list_params)

    def executescript(self, script):
        """Executa múltiplos SQLs separados por ; (usado no init_db)."""
        stmts = [s.strip() for s in script.split(';') if s.strip()]
        reqs = [{"type": "execute", "stmt": {"sql": s}} for s in stmts]
        reqs.append({"type": "close"})
        payload = {"requests": reqs}
        r = requests.post(self._pipeline, headers=self._headers,
                          json=payload, timeout=60)
        if r.status_code != 200:
            raise Exception(f"Turso HTTP {r.status_code}: {r.text[:300]}")

    def commit(self):
        pass  # Turso auto-commit por request

    def close(self):
        pass  # sem estado persistente


if _USAR_TURSO:
    print(f"🌐 [DB] Turso ativo (HTTP) → {_TURSO_URL}")


def _agora():
    """Retorna timestamp atual (Belém-PA) em ISO."""
    return datetime.now(TZ).isoformat()


def _conectar():
    """Abre conexão com o banco (Turso remoto via HTTP se configurado, SQLite local caso contrário).
    API compatível com sqlite3: execute/commit/close/row_factory/total_changes.
    """
    if _USAR_TURSO:
        return _TursoConn(_TURSO_URL, _TURSO_TOKEN)
    # ── SQLite local (padrão) ──────────────────────────────────────────────────
    # timeout=10: evita deadlock permanente — levanta OperationalError após 10s.
    # check_same_thread=False: necessário quando chamado de thread pool.
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _row_para_dict(row):
    """Converte sqlite3.Row ou _TursoRow em dict (ou {} se None)."""
    if row is None:
        return {}
    if isinstance(row, _TursoRow):
        return dict(row.items())
    return dict(row)


# ==================== INICIALIZAÇÃO ====================
def init_db():
    """
    Cria as tabelas automaticamente na primeira execução.
    Usa o schema.sql se existir; caso contrário, cria as tabelas inline.
    """
    conn = _conectar()
    try:
        if os.path.exists(SCHEMA_PATH):
            with open(SCHEMA_PATH, 'r', encoding='utf-8') as f:
                conn.executescript(f.read())
        else:
            conn.executescript(_SCHEMA_INLINE)
        conn.commit()
        print(f"✅ Banco de memória pronto: {DB_PATH}")
    finally:
        conn.close()
    # Migração incremental (adiciona colunas novas SEM apagar nenhum dado)
    _migrar_schema()


def _colunas_da_tabela(cur, tabela):
    """Retorna o conjunto de nomes de coluna de uma tabela."""
    cur.execute(f"PRAGMA table_info({tabela})")
    return {row['name'] for row in cur.fetchall()}


def _migrar_schema():
    """
    Adiciona colunas novas em tabelas existentes de forma idempotente.
    ALTER TABLE ADD COLUMN nunca apaga dados — só acrescenta a coluna com
    o valor default. Roda toda vez que o app sobe; se a coluna já existe,
    simplesmente pula.
    """
    conn = _conectar()
    try:
        cur = conn.cursor()

        # --- contatos: suporte a pacientes fictícios (Item 3) ---
        cols_contatos = _colunas_da_tabela(cur, 'contatos')
        novas_contatos = {
            'ficticio': "INTEGER DEFAULT 0",
            'criado_por': "TEXT DEFAULT ''",
            'numero_roteamento': "TEXT DEFAULT ''",
        }
        for col, tipo in novas_contatos.items():
            if col not in cols_contatos:
                cur.execute(f"ALTER TABLE contatos ADD COLUMN {col} {tipo}")

        # --- lembretes: suporte a lembretes cruzados (Item 1) ---
        cols_lembretes = _colunas_da_tabela(cur, 'lembretes')
        novas_lembretes = {
            'remetente': "TEXT DEFAULT ''",            # quem pediu (ex: 'Janderson')
            'solicitante_numero': "TEXT DEFAULT ''",   # p/ confirmar o disparo
            'nome_destinatario': "TEXT DEFAULT ''",    # como chamar (ex: 'Dra', 'Tati')
        }
        for col, tipo in novas_lembretes.items():
            if col not in cols_lembretes:
                cur.execute(f"ALTER TABLE lembretes ADD COLUMN {col} {tipo}")

        # --- horarios_agenda: calendário interno (Item 4 — substitui Google Calendar) ---
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tabelas_existentes = {r['name'] for r in cur.fetchall()}
        if 'horarios_agenda' not in tabelas_existentes:
            cur.execute("""CREATE TABLE IF NOT EXISTS horarios_agenda (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data TEXT NOT NULL,
                hora_inicio TEXT NOT NULL,
                hora_fim TEXT NOT NULL,
                status TEXT DEFAULT 'livre',
                paciente_numero TEXT DEFAULT '',
                paciente_nome TEXT DEFAULT '',
                paciente_nascimento TEXT DEFAULT '',
                observacao TEXT DEFAULT '',
                criado_em TEXT DEFAULT '',
                atualizado_em TEXT DEFAULT ''
            )""")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ha_data ON horarios_agenda(data)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ha_status ON horarios_agenda(status)")

        # --- Migração de status: 'ocupado' (legado) -> 'agendado' ---
        # A tabela sempre existe neste ponto (criada acima se ausente).
        # Idempotente: se não houver registros 'ocupado', não altera nada.
        cur.execute(
            "UPDATE horarios_agenda SET status='agendado' WHERE status='ocupado'"
        )

        # --- conversa_estado: máquina de estados do fluxo de agendamento ---
        # Controla, por número, em que etapa do agendamento o paciente está.
        # Estados possíveis:
        #   'inicial'                -> ainda não há horário proposto
        #   'horario_proposto'       -> Larissa ofereceu horário(s) reais
        #   'aguardando_confirmacao' -> paciente escolheu, falta confirmar/dados
        #   'coletando_dados'        -> coletando nome/nascimento
        #   'aguardando_pagamento'   -> link de pagamento enviado (libera [AGENDAR])
        cur.execute("""CREATE TABLE IF NOT EXISTS conversa_estado (
            numero TEXT PRIMARY KEY,
            estado TEXT DEFAULT 'inicial',
            data_proposta TEXT DEFAULT '',
            hora_proposta TEXT DEFAULT '',
            nome_coletado TEXT DEFAULT '',
            nasc_coletado TEXT DEFAULT '',
            atualizado_em TEXT DEFAULT ''
        )""")

        # --- status_funil: CRM de funil de vendas ---
        # Valores: agendado | atendido | em_cadencia | cancelado | nao_compareceu | perdido | banco
        # Adicionado como ALTER TABLE ADD COLUMN para não apagar dados existentes.
        try:
            cur.execute("ALTER TABLE contatos ADD COLUMN status_funil TEXT DEFAULT 'em_cadencia'")
        except Exception:
            pass  # coluna já existe — ignorar

        # Migra legado 'sumido' → 'banco' (sumido não existe mais no funil)
        cur.execute("UPDATE contatos SET status_funil='banco' WHERE status_funil='sumido'")

        # --- Flag de intervenção humana ---
        # 1 = Larissa sinalizou que não consegue resolver → nome vermelho no dashboard.
        # Zera automaticamente quando atendente envia mensagem manual.
        try:
            cur.execute("ALTER TABLE contatos ADD COLUMN precisa_humano INTEGER DEFAULT 0")
        except Exception:
            pass  # coluna já existe

        # --- Flag de atrito/insatisfação ---
        # NULL = sem sinal | 'leve' = insatisfação leve (amarelo) | 'grave' = insatisfação grave (vermelho)
        try:
            cur.execute("ALTER TABLE contatos ADD COLUMN flag_atrito TEXT DEFAULT NULL")
        except Exception:
            pass  # coluna já existe

        # --- Campos de cadência de reengajamento ---
        for col_def in [
            "cadencia_ativa INTEGER DEFAULT 0",
            "cadencia_toque_atual INTEGER DEFAULT 0",
            "cadencia_total_toques INTEGER DEFAULT 5",
            "cadencia_proximo_envio TEXT DEFAULT NULL",
            "cadencia_callback_em TEXT DEFAULT NULL",
        ]:
            try:
                cur.execute(f"ALTER TABLE contatos ADD COLUMN {col_def}")
            except Exception:
                pass  # coluna já existe

        # Classificação inicial dos contatos já existentes com base no histórico.
        # Roda apenas uma vez (quando status_funil é NULL ou ainda não foi definido).
        cur.execute("""
            UPDATE contatos SET status_funil = 'agendado'
            WHERE numero IN (
                SELECT DISTINCT a.numero FROM agendamentos a
                WHERE a.status IN ('agendado','confirmado')
                  AND date(a.data_hora) >= date('now')
            ) AND status_funil = 'em_cadencia'
        """)
        cur.execute("""
            UPDATE contatos SET status_funil = 'atendido'
            WHERE numero IN (
                SELECT DISTINCT a.numero FROM agendamentos a
                WHERE a.status IN ('agendado','confirmado','atendido')
                  AND date(a.data_hora) < date('now')
            ) AND status_funil NOT IN ('agendado')
            AND status_funil = 'em_cadencia'
        """)
        cur.execute("""
            UPDATE contatos SET status_funil = 'cancelado'
            WHERE numero IN (
                SELECT DISTINCT a.numero FROM agendamentos a
                WHERE a.status = 'cancelado'
            ) AND numero NOT IN (
                SELECT DISTINCT numero FROM agendamentos
                WHERE status IN ('agendado','confirmado')
                  AND date(data_hora) >= date('now')
            ) AND status_funil = 'em_cadencia'
        """)

        # --- fila_importacao: fila de leads importados aguardando ativação ---
        # Controla a entrada de 10 leads/dia no funil de cadência importado.
        cur.execute("""CREATE TABLE IF NOT EXISTS fila_importacao (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero TEXT NOT NULL UNIQUE,
            nome TEXT DEFAULT '',
            interesse TEXT DEFAULT '',
            adicionado_em TEXT NOT NULL,
            ativado_em TEXT DEFAULT NULL
        )""")

        conn.commit()
    except Exception as e:
        print(f"⚠️  Falha na migração incremental do schema: {e}")
    finally:
        conn.close()


# Schema inline (fallback caso schema.sql não seja encontrado)
_SCHEMA_INLINE = """
CREATE TABLE IF NOT EXISTS contatos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    numero TEXT UNIQUE NOT NULL,
    raw_id TEXT DEFAULT '',
    nome TEXT DEFAULT '',
    interesse TEXT DEFAULT '',
    status_lead TEXT DEFAULT 'novo',
    anotacoes TEXT DEFAULT '',
    primeiro_contato TEXT,
    ultimo_contato TEXT,
    bot_ativo INTEGER DEFAULT 1,
    dashboard_visto_ate TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS mensagens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    numero TEXT NOT NULL,
    papel TEXT NOT NULL,
    texto TEXT NOT NULL,
    intencao TEXT DEFAULT '',
    timestamp TEXT
);
CREATE TABLE IF NOT EXISTS agendamentos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    numero TEXT NOT NULL,
    nome_paciente TEXT DEFAULT '',
    procedimento TEXT DEFAULT '',
    data_hora TEXT,
    google_event_id TEXT DEFAULT '',
    status TEXT DEFAULT 'agendado',
    criado_em TEXT,
    atualizado_em TEXT
);
CREATE TABLE IF NOT EXISTS lembretes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    numero_destino TEXT NOT NULL,
    tipo TEXT DEFAULT '',
    mensagem TEXT NOT NULL,
    data_hora_envio TEXT,
    executado INTEGER DEFAULT 0,
    criado_em TEXT
);
CREATE INDEX IF NOT EXISTS idx_contatos_numero ON contatos(numero);
CREATE INDEX IF NOT EXISTS idx_contatos_status ON contatos(status_lead);
CREATE INDEX IF NOT EXISTS idx_contatos_ultimo ON contatos(ultimo_contato);
CREATE INDEX IF NOT EXISTS idx_mensagens_numero ON mensagens(numero);
CREATE INDEX IF NOT EXISTS idx_mensagens_timestamp ON mensagens(timestamp);
CREATE INDEX IF NOT EXISTS idx_agendamentos_numero ON agendamentos(numero);
CREATE INDEX IF NOT EXISTS idx_agendamentos_data ON agendamentos(data_hora);
CREATE INDEX IF NOT EXISTS idx_agendamentos_status ON agendamentos(status);
CREATE INDEX IF NOT EXISTS idx_lembretes_envio ON lembretes(data_hora_envio);
CREATE INDEX IF NOT EXISTS idx_lembretes_executado ON lembretes(executado);
"""


# ==================== CONTATOS ====================
def salvar_contato(numero, raw_id='', nome=''):
    """
    Upsert de contato.
    - Cria se não existe (primeiro_contato = ultimo_contato = agora).
    - Se existe: atualiza ultimo_contato, e preenche raw_id/nome só se estiverem vazios.
    Retorna dict do contato.
    """
    if not numero:
        return {}
    agora = _agora()
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM contatos WHERE numero = ?", (numero,))
        existente = cur.fetchone()

        if existente is None:
            cur.execute(
                """INSERT INTO contatos (numero, raw_id, nome, primeiro_contato, ultimo_contato, status_funil)
                   VALUES (?, ?, ?, ?, ?, 'em_cadencia')""",
                (numero, raw_id or '', nome or '', agora, agora)
            )
        else:
            ex = dict(existente)
            novo_raw = ex['raw_id'] if ex['raw_id'] else (raw_id or '')
            novo_nome = ex['nome'] if ex['nome'] else (nome or '')
            cur.execute(
                """UPDATE contatos
                   SET ultimo_contato = ?, raw_id = ?, nome = ?
                   WHERE numero = ?""",
                (agora, novo_raw, novo_nome, numero)
            )
        conn.commit()

        cur.execute("SELECT * FROM contatos WHERE numero = ?", (numero,))
        return _row_para_dict(cur.fetchone())
    finally:
        conn.close()


def obter_contato(numero):
    """Retorna dict com todos os campos do contato, ou {} se não existir."""
    if not numero:
        return {}
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM contatos WHERE numero = ?", (numero,))
        return _row_para_dict(cur.fetchone())
    finally:
        conn.close()


def atualizar_contato(numero, **kwargs):
    """
    Atualiza campos específicos do contato.
    Campos permitidos: interesse, status_lead, anotacoes, bot_ativo, nome, raw_id, interesse.
    Retorna dict atualizado do contato.
    """
    if not numero or not kwargs:
        return obter_contato(numero)

    permitidos = {'interesse', 'status_lead', 'anotacoes', 'bot_ativo', 'nome', 'raw_id', 'precisa_humano'}
    campos = {k: v for k, v in kwargs.items() if k in permitidos}
    if not campos:
        return obter_contato(numero)

    set_clause = ", ".join(f"{k} = ?" for k in campos.keys())
    valores = list(campos.values())
    valores.append(numero)

    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(f"UPDATE contatos SET {set_clause} WHERE numero = ?", valores)
        conn.commit()
        cur.execute("SELECT * FROM contatos WHERE numero = ?", (numero,))
        return _row_para_dict(cur.fetchone())
    finally:
        conn.close()


# ==================== MENSAGENS ====================
def salvar_mensagem(numero, papel, texto, intencao=''):
    """
    Insere mensagem no histórico.
    papel = 'PACIENTE' ou 'LARISSA'.
    Retorna id da mensagem.
    """
    if not numero or not texto:
        return None
    conn = _conectar()
    try:
        cur = conn.cursor()
        agora = _agora()
        cur.execute(
            """INSERT INTO mensagens (numero, papel, texto, intencao, timestamp)
               VALUES (?, ?, ?, ?, ?)""",
            (numero, papel, texto, intencao or '', agora)
        )
        # Atualiza ultimo_contato do lead p/ a conversa subir ao topo da lista,
        # inclusive quando quem falou foi a Larissa (scheduler/resposta automática).
        # Só atualiza se o contato já existir (não cria registro aqui).
        cur.execute(
            "UPDATE contatos SET ultimo_contato = ? WHERE numero = ?",
            (agora, numero)
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def obter_historico(numero, limite=20):
    """
    Retorna lista de dicts {remetente, texto, timestamp} em ordem cronológica.
    'remetente' usa a mesma convenção do larissa.py (PACIENTE/LARISSA).
    """
    if not numero:
        return []
    conn = _conectar()
    try:
        cur = conn.cursor()
        # Pega as N mais recentes e depois inverte para ordem cronológica
        cur.execute(
            """SELECT papel, texto, timestamp, intencao FROM mensagens
               WHERE numero = ?
               ORDER BY id DESC
               LIMIT ?""",
            (numero, limite)
        )
        linhas = cur.fetchall()[::-1]
        return [
            {
                'remetente': l['papel'],
                'texto': l['texto'],
                'timestamp': l['timestamp'],
                'intencao': l['intencao'],
            }
            for l in linhas
        ]
    finally:
        conn.close()


def obter_resumo_conversa(numero):
    """
    Retorna string formatada das últimas 20 mensagens para injetar no prompt.
    Formato: 'PACIENTE: ...' / 'LARISSA: ...' por linha.
    """
    historico = obter_historico(numero, limite=20)
    if not historico:
        return ""
    linhas = []
    for msg in historico:
        remetente = msg['remetente']
        linhas.append(f"{remetente}: {msg['texto']}")
    return "\n".join(linhas)


# ==================== AGENDAMENTOS ====================
def salvar_agendamento(numero, nome_paciente, procedimento, data_hora, google_event_id=''):
    """Insere agendamento e retorna o id."""
    if not numero:
        return None
    agora = _agora()
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO agendamentos
               (numero, nome_paciente, procedimento, data_hora, google_event_id, status, criado_em, atualizado_em)
               VALUES (?, ?, ?, ?, ?, 'agendado', ?, ?)""",
            (numero, nome_paciente or '', procedimento or '', data_hora or '',
             google_event_id or '', agora, agora)
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def obter_agendamento_ativo(numero):
    """
    Retorna dict do agendamento mais recente NÃO cancelado, ou {}.
    """
    if not numero:
        return {}
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT * FROM agendamentos
               WHERE numero = ? AND status != 'cancelado'
               ORDER BY id DESC
               LIMIT 1""",
            (numero,)
        )
        return _row_para_dict(cur.fetchone())
    finally:
        conn.close()


def atualizar_status_agendamento(agendamento_id, status):
    """
    Atualiza status do agendamento.
    status válido: agendado / nao_confirmado / confirmado / cancelado.
    Retorna True se atualizou.
    """
    if not agendamento_id:
        return False
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE agendamentos SET status = ?, atualizado_em = ? WHERE id = ?",
            (status, _agora(), agendamento_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def atualizar_status_por_event_id(google_event_id, status):
    """
    Atualiza o status do agendamento local que tem o google_event_id dado.
    Usado quando o admin cancela um evento pelo horario (sabemos o event_id
    do Calendar, mas nao o id local). Retorna True se algum registro mudou.
    """
    if not google_event_id:
        return False
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE agendamentos SET status = ?, atualizado_em = ? WHERE google_event_id = ?",
            (status, _agora(), google_event_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ==================== LEMBRETES (fila para Fase 3) ====================
def criar_lembrete(numero_destino, tipo, mensagem, data_hora_envio,
                   remetente='', solicitante_numero='', nome_destinatario=''):
    """
    Insere lembrete pendente. Retorna id.
    Campos extras (Item 1 — lembretes cruzados):
    - remetente: nome de quem pediu (ex: 'Janderson') — aparece na mensagem.
    - solicitante_numero: numero de quem pediu, p/ receber a confirmacao de disparo.
    - nome_destinatario: como chamar o destinatario (ex: 'Dra', 'Tati').
    """
    if not numero_destino or not mensagem:
        return None
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO lembretes
               (numero_destino, tipo, mensagem, data_hora_envio, executado, criado_em,
                remetente, solicitante_numero, nome_destinatario)
               VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)""",
            (numero_destino, tipo or '', mensagem, data_hora_envio or '', _agora(),
             remetente or '', solicitante_numero or '', nome_destinatario or '')
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def obter_lembretes_pendentes():
    """
    Retorna lista de lembretes com data_hora_envio <= agora e executado=0.
    """
    agora = _agora()
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT * FROM lembretes
               WHERE executado = 0 AND data_hora_envio <= ?
               ORDER BY data_hora_envio ASC""",
            (agora,)
        )
        return [dict(l) for l in cur.fetchall()]
    finally:
        conn.close()


def marcar_lembrete_executado(lembrete_id):
    """Marca lembrete como executado. Retorna True se atualizou."""
    if not lembrete_id:
        return False
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE lembretes SET executado = 1 WHERE id = ?", (lembrete_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ==================== RELATÓRIOS (Janderson / Tati) ====================
def estatisticas_geral():
    """
    Retorna dict com:
    - total_contatos
    - por_status (dict {status: qtd})
    - total_mensagens
    - agendamentos_hoje
    - agendamentos_semana
    """
    conn = _conectar()
    try:
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) AS c FROM contatos")
        total_contatos = cur.fetchone()['c']

        cur.execute("SELECT status_lead, COUNT(*) AS c FROM contatos GROUP BY status_lead")
        por_status = {row['status_lead']: row['c'] for row in cur.fetchall()}

        cur.execute("SELECT COUNT(*) AS c FROM mensagens")
        total_mensagens = cur.fetchone()['c']

        hoje = datetime.now(TZ)
        hoje_str = hoje.strftime('%Y-%m-%d')
        inicio_semana = (hoje - timedelta(days=hoje.weekday())).strftime('%Y-%m-%d')
        fim_semana = (hoje + timedelta(days=(6 - hoje.weekday()))).strftime('%Y-%m-%d')

        cur.execute(
            """SELECT COUNT(*) AS c FROM agendamentos
               WHERE status != 'cancelado' AND substr(data_hora, 1, 10) = ?""",
            (hoje_str,)
        )
        agendamentos_hoje = cur.fetchone()['c']

        cur.execute(
            """SELECT COUNT(*) AS c FROM agendamentos
               WHERE status != 'cancelado'
               AND substr(data_hora, 1, 10) >= ? AND substr(data_hora, 1, 10) <= ?""",
            (inicio_semana, fim_semana)
        )
        agendamentos_semana = cur.fetchone()['c']

        return {
            'total_contatos': total_contatos,
            'por_status': por_status,
            'total_mensagens': total_mensagens,
            'agendamentos_hoje': agendamentos_hoje,
            'agendamentos_semana': agendamentos_semana,
        }
    finally:
        conn.close()


def buscar_contato_por_nome(nome):
    """Busca parcial (LIKE) por nome. Retorna lista de dicts de contatos."""
    if not nome:
        return []
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM contatos WHERE nome LIKE ? ORDER BY ultimo_contato DESC",
            (f"%{nome}%",)
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def listar_agendamentos_dia(data):
    """
    Lista agendamentos (não cancelados) de um dia.
    data = string 'YYYY-MM-DD'.
    """
    if not data:
        return []
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT * FROM agendamentos
               WHERE status != 'cancelado' AND substr(data_hora, 1, 10) = ?
               ORDER BY data_hora ASC""",
            (data,)
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def listar_leads_recentes(limite=20):
    """Lista os contatos mais recentes por ultimo_contato, com contagem de não-lidas."""
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM contatos ORDER BY ultimo_contato DESC LIMIT ?",
            (limite,)
        )
        leads = [dict(r) for r in cur.fetchall()]
        # Para cada lead, conta mensagens do PACIENTE após dashboard_visto_ate
        for lead in leads:
            visto_ate = lead.get('dashboard_visto_ate') or ''
            numero = lead['numero']
            if visto_ate:
                cur.execute(
                    "SELECT COUNT(*) FROM mensagens WHERE numero=? AND papel='PACIENTE' AND timestamp > ?",
                    (numero, visto_ate)
                )
            else:
                cur.execute(
                    "SELECT COUNT(*) FROM mensagens WHERE numero=? AND papel='PACIENTE'",
                    (numero,)
                )
            lead['mensagens_nao_lidas'] = cur.fetchone()[0]
        return leads
    finally:
        conn.close()


def marcar_dashboard_lido(numero):
    """Marca o timestamp de quando o dashboard abriu a conversa (zera não-lidas).

    Usa _agora() (horário de Belém, -03:00) para ficar no MESMO formato de
    timezone das mensagens no banco. Se usasse UTC, a comparação lexicográfica
    de strings no SQLite poderia contar não-lidas erradas.
    """
    agora = _agora()
    conn = _conectar()
    try:
        conn.execute(
            "UPDATE contatos SET dashboard_visto_ate=? WHERE numero=?",
            (agora, numero)
        )
        conn.commit()
    finally:
        conn.close()


def listar_agendamentos_por_numero(numero, incluir_cancelados=False):
    """Lista os agendamentos de um contato (mais recentes primeiro)."""
    if not numero:
        return []
    conn = _conectar()
    try:
        cur = conn.cursor()
        if incluir_cancelados:
            cur.execute(
                "SELECT * FROM agendamentos WHERE numero = ? ORDER BY data_hora DESC",
                (numero,)
            )
        else:
            cur.execute(
                "SELECT * FROM agendamentos WHERE numero = ? AND status != 'cancelado' "
                "ORDER BY data_hora DESC",
                (numero,)
            )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# ==================== PACIENTES FICTÍCIOS (Item 3 — testes) ====================
def criar_contato_ficticio(numero_persona, nome, criado_por, numero_roteamento):
    """
    Cria um contato FICTÍCIO (para testes do admin).
    - numero_persona: chave única interna da ficha (ex: 'persona_joao_559...').
    - nome: nome de exibição (aparece como o nome do WhatsApp do paciente).
    - criado_por: número do admin que criou (para permissão de exclusão).
    - numero_roteamento: número REAL para onde enviar mensagens deste fictício
      (normalmente o número do próprio admin criador).
    Retorna dict do contato criado (ou existente, se já houver).
    """
    if not numero_persona or not criado_por:
        return {}
    agora = _agora()
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM contatos WHERE numero = ?", (numero_persona,))
        existente = cur.fetchone()
        if existente is None:
            cur.execute(
                """INSERT INTO contatos
                   (numero, raw_id, nome, status_lead, primeiro_contato, ultimo_contato,
                    bot_ativo, ficticio, criado_por, numero_roteamento)
                   VALUES (?, '', ?, 'ficticio', ?, ?, 1, 1, ?, ?)""",
                (numero_persona, nome or 'Paciente Teste', agora, agora,
                 criado_por, numero_roteamento or criado_por)
            )
            conn.commit()
        cur.execute("SELECT * FROM contatos WHERE numero = ?", (numero_persona,))
        return _row_para_dict(cur.fetchone())
    finally:
        conn.close()


def listar_contatos_ficticios(criado_por=None):
    """
    Lista os pacientes fictícios. Se criado_por for informado, filtra só os
    criados por aquele admin.
    """
    conn = _conectar()
    try:
        cur = conn.cursor()
        if criado_por:
            cur.execute(
                "SELECT * FROM contatos WHERE ficticio = 1 AND criado_por = ? "
                "ORDER BY ultimo_contato DESC",
                (criado_por,)
            )
        else:
            cur.execute(
                "SELECT * FROM contatos WHERE ficticio = 1 ORDER BY ultimo_contato DESC"
            )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def excluir_contato_ficticio(numero_persona, criado_por):
    """
    Exclui TODOS os dados de um paciente fictício (contato + mensagens +
    agendamentos + lembretes). SÓ funciona se o contato for ficticio=1 e
    tiver sido criado pelo mesmo admin (proteção contra apagar dados reais).
    Retorna dict {sucesso, mensagem}.
    """
    if not numero_persona or not criado_por:
        return {'sucesso': False, 'mensagem': 'Dados insuficientes.'}
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM contatos WHERE numero = ?", (numero_persona,))
        c = cur.fetchone()
        if c is None:
            return {'sucesso': False, 'mensagem': 'Paciente fictício não encontrado.'}
        c = dict(c)
        if not c.get('ficticio'):
            return {'sucesso': False, 'mensagem': 'Esse contato NÃO é fictício — não posso excluir dados reais.'}
        if str(c.get('criado_por') or '') != str(criado_por):
            return {'sucesso': False, 'mensagem': 'Só quem criou o fictício pode excluí-lo.'}

        cur.execute("DELETE FROM mensagens WHERE numero = ?", (numero_persona,))
        cur.execute("DELETE FROM agendamentos WHERE numero = ?", (numero_persona,))
        cur.execute("DELETE FROM lembretes WHERE numero_destino = ?", (numero_persona,))
        cur.execute("DELETE FROM contatos WHERE numero = ? AND ficticio = 1", (numero_persona,))
        conn.commit()
        return {'sucesso': True, 'mensagem': f"Paciente fictício '{c.get('nome')}' e todos os dados dele foram apagados."}
    finally:
        conn.close()


def obter_contato_ficticio_por_nome(nome, criado_por):
    """Acha um fictício do admin pelo nome (parcial). Retorna dict ou {}."""
    if not nome or not criado_por:
        return {}
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM contatos WHERE ficticio = 1 AND criado_por = ? AND nome LIKE ? "
            "ORDER BY ultimo_contato DESC LIMIT 1",
            (criado_por, f"%{nome}%")
        )
        return _row_para_dict(cur.fetchone())
    finally:
        conn.close()


# ==================== AGENDA INTERNA (Item 4 — substitui Google Calendar) ====================

def adicionar_slot(data, hora_inicio, hora_fim, observacao=''):
    """
    Cria um slot livre na agenda interna.
    data='YYYY-MM-DD', hora_inicio='HH:MM', hora_fim='HH:MM'.
    Retorna id do slot criado.
    """
    if not data or not hora_inicio or not hora_fim:
        return None
    agora = _agora()
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO horarios_agenda
               (data, hora_inicio, hora_fim, status, observacao, criado_em, atualizado_em)
               VALUES (?, ?, ?, 'livre', ?, ?, ?)""",
            (data, hora_inicio, hora_fim, observacao or '', agora, agora)
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def listar_slots_dia(data):
    """
    Retorna TODOS os slots (qualquer status) de um dia, em ordem de hora_inicio.
    data='YYYY-MM-DD'. Retorna lista de dicts.
    """
    if not data:
        return []
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM horarios_agenda WHERE data = ? ORDER BY hora_inicio ASC",
            (data,)
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def listar_slots_livres_dia(data):
    """
    Retorna lista de strings 'HH:MM' dos slots LIVRES de um dia.
    """
    if not data:
        return []
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT hora_inicio FROM horarios_agenda "
            "WHERE data = ? AND status IN ('livre', 'cancelado') "
            "ORDER BY hora_inicio ASC",
            (data,)
        )
        horarios = [r['hora_inicio'] for r in cur.fetchall()]
    finally:
        conn.close()
    # FIX (horário no passado): se 'data' é HOJE, nunca oferecer horários que já
    # passaram (ex.: eram 10:00 e a agenda tinha 09:00 livre). Só filtra o dia de
    # hoje — dias futuros ficam intactos. Os mapas de disponibilidade (próximos
    # dias) começam em AMANHÃ, então não são afetados por isto.
    try:
        agora = datetime.now(TZ)
        if data == agora.strftime('%Y-%m-%d'):
            hhmm_agora = agora.strftime('%H:%M')
            horarios = [h for h in horarios if h > hhmm_agora]
    except Exception:
        pass
    return horarios


def listar_slots_por_turno(data, turno=None):
    """
    Retorna lista de strings 'HH:MM' de slots LIVRES do dia, filtrados por turno.
    turno='manha' → hora_inicio < '13:00', turno='tarde' → >= '13:00'.
    Se turno=None ou 'qualquer', retorna todos os livres.
    """
    livres = listar_slots_livres_dia(data)
    if not turno or turno == 'qualquer':
        return livres
    if turno == 'manha':
        return [h for h in livres if h < '13:00']
    if turno == 'tarde':
        return [h for h in livres if h >= '13:00']
    return livres


def ocupar_slot_por_hora(data, hora, paciente_numero, paciente_nome,
                         paciente_nascimento=''):
    """
    Marca um slot LIVRE como 'agendado' e preenche dados do paciente.
    NÃO cria slot on-the-fly: se não houver slot LIVRE naquela data/hora,
    retorna None e loga um aviso. Isso impede agendamentos em horários que
    a Duda não liberou.
    Retorna o id do slot (int) ou None se falhar.
    """
    if not data or not hora or not paciente_numero:
        return None
    agora = _agora()
    conn = _conectar()
    try:
        cur = conn.cursor()
        # Só agenda se existir slot LIVRE nessa data/hora
        # Aceita 'livre' e 'cancelado': slot cancelado = vaga devolvida, disponível
        # para nova pessoa. Antes so aceitava 'livre', entao horarios cancelados
        # apareciam como disponiveis mas nao podiam ser agendados (bug).
        cur.execute(
            "SELECT * FROM horarios_agenda "
            "WHERE data = ? AND hora_inicio = ? AND status IN ('livre', 'cancelado')",
            (data, hora)
        )
        slot = cur.fetchone()
        if slot:
            # Salva nascimento tanto no campo dedicado quanto na observacao (visivel no dashboard)
            obs_nasc = f"Nasc: {paciente_nascimento}" if paciente_nascimento else ''
            cur.execute(
                """UPDATE horarios_agenda
                   SET status='agendado', paciente_numero=?, paciente_nome=?,
                       paciente_nascimento=?, observacao=?, atualizado_em=?
                   WHERE id=?""",
                (paciente_numero, paciente_nome or '', paciente_nascimento or '',
                 obs_nasc, agora, slot['id'])
            )
            conn.commit()
            return slot['id']
        else:
            # Sem slot livre: não cria nada, apenas avisa.
            print(f"⚠️ Slot não disponível: {data} {hora}")
            return None
    finally:
        conn.close()


def cancelar_slot_por_hora(data, hora):
    """
    Marca o slot OCUPADO/AGENDADO da hora dada como 'cancelado'.
    Retorna True se alterou algum registro.
    """
    if not data or not hora:
        return False
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE horarios_agenda
               SET status='cancelado', atualizado_em=?
               WHERE data=? AND hora_inicio=? AND status NOT IN ('livre', 'cancelado')""",
            (_agora(), data, hora)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def reativar_slot_por_hora(data, hora):
    """
    Reverte status de 'cancelado' → 'agendado' para o slot da hora dada.
    Retorna True se alterou.
    """
    if not data or not hora:
        return False
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE horarios_agenda
               SET status='agendado', atualizado_em=?
               WHERE data=? AND hora_inicio=? AND status='cancelado'""",
            (_agora(), data, hora)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def atualizar_status_slot_por_id(slot_id, status):
    """Atualiza o status de um slot pelo id. Retorna True se alterou."""
    if not slot_id:
        return False
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE horarios_agenda SET status=?, atualizado_em=? WHERE id=?",
            (status, _agora(), slot_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def deletar_slot(slot_id):
    """Apaga um slot da agenda. Retorna True se apagou."""
    if not slot_id:
        return False
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM horarios_agenda WHERE id=?", (slot_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def obter_slot(slot_id):
    """Retorna dict de um slot pelo id, ou {}."""
    if not slot_id:
        return {}
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM horarios_agenda WHERE id=?", (slot_id,))
        return _row_para_dict(cur.fetchone())
    finally:
        conn.close()


def obter_slot_por_hora(data, hora, status=None):
    """
    Retorna dict do slot que começa na hora dada (e data), ou {}.
    Se status for informado, filtra pelo status.
    """
    if not data or not hora:
        return {}
    conn = _conectar()
    try:
        cur = conn.cursor()
        if status:
            cur.execute(
                "SELECT * FROM horarios_agenda WHERE data=? AND hora_inicio=? AND status=? LIMIT 1",
                (data, hora, status)
            )
        else:
            cur.execute(
                "SELECT * FROM horarios_agenda WHERE data=? AND hora_inicio=? "
                "ORDER BY id DESC LIMIT 1",
                (data, hora)
            )
        return _row_para_dict(cur.fetchone())
    finally:
        conn.close()


def listar_slots_amanha_ocupados():
    """
    Retorna lista de slots OCUPADOS de amanhã (para scheduler de confirmações).
    """
    amanha = (datetime.now(TZ) + timedelta(days=1)).strftime('%Y-%m-%d')
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM horarios_agenda WHERE data=? AND status IN ('agendado','ocupado') "
            "ORDER BY hora_inicio ASC",
            (amanha,)
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def listar_slots_hoje_ocupados():
    """
    Retorna lista de slots OCUPADOS de hoje (para scheduler de lembretes).
    """
    hoje = datetime.now(TZ).strftime('%Y-%m-%d')
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM horarios_agenda WHERE data=? AND status IN ('agendado','ocupado','nao_confirmado','confirmado') "
            "ORDER BY hora_inicio ASC",
            (hoje,)
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def atualizar_slot(slot_id, **kwargs):
    """
    Atualiza campos do slot (observacao, status, hora_inicio, hora_fim, etc.).
    Retorna True se alterou.
    """
    if not slot_id or not kwargs:
        return False
    permitidos = {'status', 'observacao', 'hora_inicio', 'hora_fim',
                  'paciente_numero', 'paciente_nome', 'paciente_nascimento'}
    campos = {k: v for k, v in kwargs.items() if k in permitidos}
    if not campos:
        return False
    campos['atualizado_em'] = _agora()
    set_clause = ", ".join(f"{k} = ?" for k in campos.keys())
    valores = list(campos.values()) + [slot_id]
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(f"UPDATE horarios_agenda SET {set_clause} WHERE id=?", valores)
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def confirmar_slot(slot_id):
    """
    Confirma a presenca do paciente: muda o status do slot para 'confirmado'.
    Aceita slots 'agendado', 'nao_confirmado' OU 'ocupado' (legado).
    'nao_confirmado' e o status que o scheduler deixa apos enviar o pedido de
    confirmacao do dia anterior; 'agendado' e o novo status de reserva.
    Retorna True se alterou algum registro.
    """
    if not slot_id:
        return False
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE horarios_agenda SET status='confirmado', atualizado_em=? "
            "WHERE id=? AND status IN ('agendado', 'nao_confirmado', 'ocupado')",
            (_agora(), slot_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def marcar_nao_confirmado(slot_id):
    """
    Muda o status de um slot 'agendado' (ou 'ocupado' legado) para
    'nao_confirmado'. Chamado pelo scheduler ao enviar a mensagem de
    confirmacao do dia anterior — significa: pedido enviado, aguardando
    resposta do paciente.
    Retorna True se alterou algum registro.
    """
    if not slot_id:
        return False
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE horarios_agenda SET status='nao_confirmado', atualizado_em=? "
            "WHERE id=? AND status IN ('agendado', 'ocupado')",
            (_agora(), slot_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def atualizar_slot_cancelamento(slot_id, novo_status, observacao):
    """
    Atualiza status + observacao do slot num unico UPDATE (cancelamento).
    - Se novo_status == 'livre': limpa os dados do paciente (o horario volta a
      ficar disponivel para outra pessoa — cancelamento com antecedencia).
    - Caso contrario (ex.: 'cancelado'): mantem os dados do paciente para
      historico/registro (cancelamento no mesmo dia).
    Retorna True se alterou algum registro.
    """
    if not slot_id:
        return False
    conn = _conectar()
    try:
        cur = conn.cursor()
        if novo_status == 'livre':
            cur.execute(
                "UPDATE horarios_agenda SET status=?, observacao=?, "
                "paciente_numero='', paciente_nome='', paciente_nascimento='', "
                "atualizado_em=? WHERE id=?",
                (novo_status, observacao or '', _agora(), slot_id)
            )
        else:
            cur.execute(
                "UPDATE horarios_agenda SET status=?, observacao=?, atualizado_em=? "
                "WHERE id=?",
                (novo_status, observacao or '', _agora(), slot_id)
            )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()



# ============================================================
# Máquina de estados do fluxo de agendamento (conversa_estado)
# ============================================================
def obter_estado_conversa(numero):
    """Retorna o estado da conversa para um número como dict.
    Se não houver registro, devolve o estado padrão 'inicial'.
    """
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM conversa_estado WHERE numero = ? LIMIT 1",
            (numero,)
        )
        row = cur.fetchone()
        if row:
            return _row_para_dict(row)
        return {
            'numero': numero,
            'estado': 'inicial',
            'data_proposta': '',
            'hora_proposta': '',
            'nome_coletado': '',
            'nasc_coletado': '',
            'atualizado_em': ''
        }
    finally:
        conn.close()


def salvar_estado_conversa(numero, estado, data_proposta='', hora_proposta='',
                           nome_coletado='', nasc_coletado=''):
    """Faz upsert do estado da conversa para um número."""
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO conversa_estado
               (numero, estado, data_proposta, hora_proposta,
                nome_coletado, nasc_coletado, atualizado_em)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(numero) DO UPDATE SET
                   estado=excluded.estado,
                   data_proposta=excluded.data_proposta,
                   hora_proposta=excluded.hora_proposta,
                   nome_coletado=excluded.nome_coletado,
                   nasc_coletado=excluded.nasc_coletado,
                   atualizado_em=excluded.atualizado_em""",
            (numero, estado, data_proposta or '', hora_proposta or '',
             nome_coletado or '', nasc_coletado or '', _agora())
        )
        conn.commit()
        return True
    finally:
        conn.close()


def resetar_estado_conversa(numero):
    """Volta o estado da conversa para 'inicial' e limpa os campos temporários."""
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO conversa_estado
               (numero, estado, data_proposta, hora_proposta,
                nome_coletado, nasc_coletado, atualizado_em)
               VALUES (?, 'inicial', '', '', '', '', ?)
               ON CONFLICT(numero) DO UPDATE SET
                   estado='inicial',
                   data_proposta='',
                   hora_proposta='',
                   nome_coletado='',
                   nasc_coletado='',
                   atualizado_em=excluded.atualizado_em""",
            (numero, _agora())
        )
        conn.commit()
        return True
    finally:
        conn.close()


def apagar_paciente_completo(numero):
    """
    HARD DELETE: apaga TODOS os registros de um paciente do sistema, para que a
    proxima mensagem dele comece do zero (usado por admin em numeros de teste).

    Apaga:
      - mensagens (historico completo)
      - conversa_estado (etapa/horario proposto)
      - agendamentos (consultas do paciente)
      - lembretes (callbacks/confirmacoes pendentes)
      - contato (registro mestre: nome, funil, cadencia)
    E LIBERA (volta para 'livre') os slots da agenda que estavam ocupados por ele,
    limpando nome/numero/nascimento — assim o horario fica disponivel de novo e o
    nome do paciente apagado nao aparece mais no dashboard.

    Retorna dict com a contagem do que foi apagado, ou None se numero vazio.
    NUNCA apaga slots da agenda em si (sao horarios da Duda) — apenas os libera.
    """
    if not numero:
        return None
    conn = _conectar()
    try:
        cur = conn.cursor()

        cur.execute("DELETE FROM mensagens WHERE numero = ?", (numero,))
        n_msgs = cur.rowcount

        cur.execute("DELETE FROM conversa_estado WHERE numero = ?", (numero,))
        n_estado = cur.rowcount

        cur.execute("DELETE FROM agendamentos WHERE numero = ?", (numero,))
        n_agend = cur.rowcount

        cur.execute("DELETE FROM lembretes WHERE numero_destino = ?", (numero,))
        n_lembretes = cur.rowcount

        # Libera slots ocupados por este paciente (volta a ficar 'livre').
        cur.execute(
            """UPDATE horarios_agenda
               SET status='livre', paciente_numero='', paciente_nome='',
                   paciente_nascimento='', observacao='', atualizado_em=?
               WHERE paciente_numero = ? AND status != 'livre'""",
            (_agora(), numero)
        )
        n_slots = cur.rowcount

        cur.execute("DELETE FROM contatos WHERE numero = ?", (numero,))
        n_contato = cur.rowcount

        conn.commit()
        return {
            'mensagens': n_msgs,
            'conversa_estado': n_estado,
            'agendamentos': n_agend,
            'lembretes': n_lembretes,
            'slots_liberados': n_slots,
            'contato': n_contato,
        }
    finally:
        conn.close()



# ─────────────────────────────────────────────────────────────────────────────
# FUNIL DE VENDAS (status_funil na tabela contatos)
# Valores: agendado | atendido | em_cadencia | cancelado | nao_compareceu | perdido | banco
# ─────────────────────────────────────────────────────────────────────────────

_FUNIL_VALIDOS = {'agendado', 'atendido', 'em_cadencia', 'cancelado', 'nao_compareceu', 'perdido', 'banco', 'importado'}

# Intervalo em dias entre cada toque da cadência (índice = toque-1)
# [toque1, toque2, toque3, toque4, toque5]
_CADENCIA_INTERVALOS_5 = [2, 5, 12, 25, 45]   # em_cadencia / banco retorno
_CADENCIA_INTERVALOS_3 = [1, 4, 10]            # cancelado / banco-retorno-curto


def atualizar_funil(numero: str, status: str) -> bool:
    """Define o status_funil de um contato. Retorna True se atualizou."""
    if status not in _FUNIL_VALIDOS:
        return False
    conn = _conectar()
    try:
        conn.execute(
            "UPDATE contatos SET status_funil=? WHERE numero=?",
            (status, numero)
        )
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def atualizar_flag_atrito(numero: str, nivel: str) -> bool:
    """Define o flag_atrito de um contato. nivel: 'leve', 'grave' ou None para limpar."""
    niveis_validos = {'leve', 'grave', None}
    if nivel not in niveis_validos:
        return False
    conn = _conectar()
    try:
        conn.execute(
            "UPDATE contatos SET flag_atrito=? WHERE numero=?",
            (nivel, numero)
        )
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def obter_funil_resumo() -> dict:
    """Retorna contagem por status_funil (exclui contatos fictícios e admins)."""
    conn = _conectar()
    try:
        rows = conn.execute("""
            SELECT COALESCE(status_funil,'em_cadencia') AS sf, COUNT(*) AS n
            FROM contatos
            WHERE ficticio = 0
              AND numero NOT IN ('5591991490412','5591984308592')
            GROUP BY sf
        """).fetchall()
        totais = {r[0]: r[1] for r in rows}
        # garante todos os status mesmo se zerado
        for s in _FUNIL_VALIDOS:
            totais.setdefault(s, 0)
        totais['total'] = sum(totais[s] for s in _FUNIL_VALIDOS)
        return totais
    finally:
        conn.close()


def listar_por_funil(status: str, limite: int = 50) -> list:
    """Retorna lista de contatos para um status do funil, incluindo dados de cadência."""
    conn = _conectar()
    try:
        rows = conn.execute("""
            SELECT numero, nome, ultimo_contato,
                   cadencia_ativa, cadencia_toque_atual, cadencia_total_toques
            FROM contatos
            WHERE COALESCE(status_funil,'em_cadencia') = ?
              AND ficticio = 0
              AND numero NOT IN ('5591991490412','5591984308592')
            ORDER BY ultimo_contato DESC
            LIMIT ?
        """, (status, limite)).fetchall()
        return [{
            'numero': r[0],
            'nome': r[1] or r[0],
            'ultimo_contato': r[2],
            'cadencia_ativa': r[3],
            'cadencia_toque_atual': r[4],
            'cadencia_total_toques': r[5],
        } for r in rows]
    finally:
        conn.close()


def marcar_atendidos_passados() -> int:
    """
    Job noturno: agendamentos passados sem cancelamento → status_funil='atendido'.
    Retorna quantos contatos foram atualizados.
    """
    conn = _conectar()
    try:
        conn.execute("""
            UPDATE contatos SET status_funil = 'atendido'
            WHERE numero IN (
                SELECT DISTINCT a.numero FROM agendamentos a
                WHERE a.status IN ('agendado','confirmado')
                  AND datetime(a.data_hora) < datetime('now','-3 hours')
            ) AND COALESCE(status_funil,'em_cadencia') = 'agendado'
        """)
        conn.commit()
        return conn.total_changes
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# CADÊNCIA DE REENGAJAMENTO
# ─────────────────────────────────────────────────────────────────────────────

import datetime as _dt

_ADMINS_EXCLUIDOS = {'5591991490412', '5591984308592'}
_TZ_BELEM = None  # lazy-loaded

def _tz_belem():
    global _TZ_BELEM
    if _TZ_BELEM is None:
        import pytz
        _TZ_BELEM = pytz.timezone('America/Belem')
    return _TZ_BELEM

def _agora_belem():
    return _dt.datetime.now(_tz_belem())

def _proximo_envio_iso(dias_offset: int, toque_numero: int) -> str:
    """
    Calcula o próximo timestamp de envio em ISO-8601 respeitando:
    - offset em dias a partir de agora
    - horário preferido por toque (ímpares = noite 19h, pares = manhã 10h30)
    - dias úteis Ter–Qui prioritários (empurra se cair em Dom/Seg/Sex-tarde/Sab)
    """
    agora = _agora_belem()
    alvo = agora + _dt.timedelta(days=dias_offset)
    # Horário preferido alternado
    hora_pref = 19 if (toque_numero % 2 == 1) else 10
    minuto_pref = 0 if hora_pref == 19 else 30

    # Empurra para dia útil (evita sáb=5, dom=6)
    for _ in range(7):
        wd = alvo.weekday()  # 0=seg, 1=ter, ... 6=dom
        if wd in (5, 6):     # sáb ou dom → seg
            alvo += _dt.timedelta(days=(7 - wd) % 7 or 1)
            continue
        break

    alvo = alvo.replace(hour=hora_pref, minute=minuto_pref, second=0, microsecond=0)
    return alvo.isoformat()


def entrar_cadencia(numero: str, total_toques: int = 5, status_funil_novo: str = None) -> bool:
    """
    Ativa a cadência de reengajamento para um contato.
    - total_toques: 5 para em_cadencia, 3 para cancelado ou banco-retorno
    - status_funil_novo: se fornecido, atualiza também o status_funil
    Retorna True se atualizou.
    """
    if numero in _ADMINS_EXCLUIDOS:
        return False
    intervalos = _CADENCIA_INTERVALOS_5 if total_toques == 5 else _CADENCIA_INTERVALOS_3
    proximo = _proximo_envio_iso(intervalos[0], 1)
    conn = _conectar()
    try:
        campos = {
            'cadencia_ativa': 1,
            'cadencia_toque_atual': 0,
            'cadencia_total_toques': total_toques,
            'cadencia_proximo_envio': proximo,
            'cadencia_callback_em': None,
        }
        if status_funil_novo:
            campos['status_funil'] = status_funil_novo
        set_clause = ', '.join(f"{k}=?" for k in campos)
        vals = list(campos.values()) + [numero]
        conn.execute(f"UPDATE contatos SET {set_clause} WHERE numero=?", vals)
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def avancar_toque_cadencia(numero: str) -> dict:
    """
    Marca o toque atual como enviado e agenda o próximo.
    Retorna {'toque': N, 'total': M, 'encerrado': bool, 'proximo_envio': iso_str}
    Quando toque == total → encerra cadência e muda status para 'banco'.
    """
    conn = _conectar()
    try:
        row = conn.execute(
            "SELECT cadencia_toque_atual, cadencia_total_toques FROM contatos WHERE numero=?",
            (numero,)
        ).fetchone()
        if not row:
            return {'encerrado': True, 'toque': 0, 'total': 0}

        toque_feito = (row[0] or 0) + 1
        total = row[1] or 5
        intervalos = _CADENCIA_INTERVALOS_5 if total == 5 else _CADENCIA_INTERVALOS_3

        if toque_feito >= total:
            # Esgotou — vai para banco
            conn.execute("""
                UPDATE contatos SET
                    cadencia_toque_atual=?, cadencia_ativa=0,
                    cadencia_proximo_envio=NULL, status_funil='banco'
                WHERE numero=?
            """, (toque_feito, numero))
            conn.commit()
            return {'toque': toque_feito, 'total': total, 'encerrado': True, 'proximo_envio': None}
        else:
            proximo_idx = toque_feito  # próximo toque (0-based index)
            proximo_iso = _proximo_envio_iso(intervalos[proximo_idx], toque_feito + 1)
            conn.execute("""
                UPDATE contatos SET
                    cadencia_toque_atual=?, cadencia_proximo_envio=?
                WHERE numero=?
            """, (toque_feito, proximo_iso, numero))
            conn.commit()
            return {'toque': toque_feito, 'total': total, 'encerrado': False, 'proximo_envio': proximo_iso}
    finally:
        conn.close()


def pausar_cadencia(numero: str) -> bool:
    """Pausa a cadência (liga/desliga manual pela Duda)."""
    conn = _conectar()
    try:
        conn.execute("UPDATE contatos SET cadencia_ativa=0 WHERE numero=?", (numero,))
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def retomar_cadencia(numero: str) -> bool:
    """Retoma cadência pausada (mantém toque_atual e proximo_envio)."""
    conn = _conectar()
    try:
        conn.execute("UPDATE contatos SET cadencia_ativa=1 WHERE numero=?", (numero,))
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def cancelar_cadencia_completa(numero: str) -> bool:
    """Cancela e zera cadência (usado quando paciente agenda ou vai para perdido)."""
    conn = _conectar()
    try:
        conn.execute("""
            UPDATE contatos SET
                cadencia_ativa=0, cadencia_toque_atual=0,
                cadencia_proximo_envio=NULL, cadencia_callback_em=NULL
            WHERE numero=?
        """, (numero,))
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def registrar_callback_cadencia(numero: str, data_hora_iso: str) -> bool:
    """
    Registra um callback (data combinada com o paciente): a Larissa combinou de
    voltar a falar com a pessoa num dia futuro.

    FIX (callback nao disparava): antes esta funcao so gravava cadencia_callback_em,
    mas listar_cadencias_pendentes() exige cadencia_ativa=1 — entao o callback NUNCA
    era disparado a menos que a cadencia ja estivesse ativa. Agora ativamos a
    cadencia (5 toques, toque 0) e apontamos APENAS o callback como proximo envio.
    No dia combinado o scheduler envia a mensagem; se nao resultar em agendamento,
    o callback e limpo e a pessoa segue nos toques normais de cadencia.

    Se a pessoa ja estiver com consulta ativa (agendado/atendido/confirmado), NAO
    mexemos no funil dela — so registramos a data para nao atropelar um agendamento.
    """
    if numero in _ADMINS_EXCLUIDOS:
        return False
    # Normaliza: se veio so a data (AAAA-MM-DD), fixa 10:00 (janela da manha) para
    # o callback disparar de manha no dia combinado.
    cb = (data_hora_iso or '').strip()
    if len(cb) == 10:                      # 'AAAA-MM-DD'
        cb = cb + 'T10:00:00'
    conn = _conectar()
    try:
        row = conn.execute(
            "SELECT status_funil FROM contatos WHERE numero=?", (numero,)
        ).fetchone()
        status_atual = (row['status_funil'] if row else None) or 'em_cadencia'
        preserva = status_atual in ('agendado', 'atendido', 'confirmado')
        if preserva:
            conn.execute("""
                UPDATE contatos SET
                    cadencia_callback_em=?, cadencia_ativa=1,
                    cadencia_proximo_envio=NULL
                WHERE numero=?
            """, (cb, numero))
        else:
            conn.execute("""
                UPDATE contatos SET
                    cadencia_callback_em=?, cadencia_ativa=1,
                    cadencia_toque_atual=0, cadencia_total_toques=5,
                    cadencia_proximo_envio=NULL, status_funil='em_cadencia'
                WHERE numero=?
            """, (cb, numero))
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def cancelar_callback_cadencia(numero: str) -> bool:
    """Cancela o callback pendente (paciente entrou em contato antes)."""
    conn = _conectar()
    try:
        conn.execute(
            "UPDATE contatos SET cadencia_callback_em=NULL WHERE numero=?",
            (numero,)
        )
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def marcar_nao_compareceu(numero: str) -> bool:
    """
    Duda marca manualmente que paciente não compareceu.
    Status muda para nao_compareceu; cadência será ativada pelo scheduler no dia seguinte.
    """
    conn = _conectar()
    try:
        conn.execute("""
            UPDATE contatos SET
                status_funil='nao_compareceu',
                cadencia_ativa=0,
                cadencia_toque_atual=0,
                cadencia_proximo_envio=NULL,
                cadencia_callback_em=NULL
            WHERE numero=?
        """, (numero,))
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def listar_cadencias_pendentes() -> list:
    """
    Retorna contatos com cadência ativa cujo próximo envio ou callback já passou.
    Usado pelo scheduler a cada 30s.
    """
    agora = _agora_belem().isoformat()
    conn = _conectar()
    try:
        rows = conn.execute("""
            SELECT numero, nome, status_funil,
                   cadencia_toque_atual, cadencia_total_toques,
                   cadencia_proximo_envio, cadencia_callback_em,
                   ultimo_contato
            FROM contatos
            WHERE ficticio = 0
              AND numero NOT IN ('5591991490412','5591984308592')
              AND cadencia_ativa = 1
              AND (
                  (cadencia_callback_em IS NOT NULL AND cadencia_callback_em <= ?)
                  OR
                  (cadencia_callback_em IS NULL AND cadencia_proximo_envio IS NOT NULL
                   AND cadencia_proximo_envio <= ?)
              )
        """, (agora, agora)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def listar_para_ativar_cadencia() -> list:
    """
    Retorna contatos em em_cadencia com cadência ainda não ativa e sem mensagem
    nos últimos 2 dias. O scheduler ativa a cadência automaticamente para eles.
    """
    corte = (_agora_belem() - _dt.timedelta(days=2)).isoformat()
    conn = _conectar()
    try:
        rows = conn.execute("""
            SELECT numero, nome, ultimo_contato
            FROM contatos
            WHERE ficticio = 0
              AND numero NOT IN ('5591991490412','5591984308592')
              AND status_funil = 'em_cadencia'
              AND cadencia_ativa = 0
              AND cadencia_toque_atual = 0
              AND (ultimo_contato IS NULL OR ultimo_contato < ?)
        """, (corte,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def listar_nao_compareceu_pendentes() -> list:
    """
    Retorna contatos marcados como nao_compareceu com cadência ainda não iniciada.
    O scheduler envia a mensagem de acolhimento no dia seguinte.
    """
    conn = _conectar()
    try:
        rows = conn.execute("""
            SELECT numero, nome, ultimo_contato
            FROM contatos
            WHERE ficticio = 0
              AND numero NOT IN ('5591991490412','5591984308592')
              AND status_funil = 'nao_compareceu'
              AND cadencia_ativa = 0
              AND cadencia_toque_atual = 0
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def obter_cadencia_info(numero: str) -> dict:
    """Retorna os campos de cadência de um contato específico."""
    conn = _conectar()
    try:
        row = conn.execute("""
            SELECT cadencia_ativa, cadencia_toque_atual, cadencia_total_toques,
                   cadencia_proximo_envio, cadencia_callback_em, status_funil
            FROM contatos WHERE numero=?
        """, (numero,)).fetchone()
        if not row:
            return {}
        return dict(row)
    finally:
        conn.close()


def ultima_mensagem_recebida(numero: str) -> str | None:
    """Retorna o timestamp ISO da última mensagem do PACIENTE (para checar 24h)."""
    conn = _conectar()
    try:
        row = conn.execute("""
            SELECT timestamp FROM mensagens
            WHERE numero=? AND papel='PACIENTE'
            ORDER BY timestamp DESC LIMIT 1
        """, (numero,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()



# ─────────────────────────────────────────────────────────────────────────────
# FILA DE IMPORTAÇÃO (status 'importado') — cadência de 3 toques: dia 3/12/28
# Fluxo: CSV/lista → fila_importacao → ativar 10/dia → cadência → banco (final)
# ─────────────────────────────────────────────────────────────────────────────

_CADENCIA_IMPORTADO_DIAS = [3, 12, 28]  # dias do 1°/2°/3° toque desde o 1° contato


def adicionar_importados(leads: list[dict]) -> dict:
    """
    Adiciona leads à fila de importação.
    Cada item: {'numero': str, 'nome': str (opt), 'interesse': str (opt)}.
    Ignora duplicatas (número já na fila ou já no banco).
    Retorna {'adicionados': N, 'ignorados': N, 'erros': [...]}.
    """
    adicionados = 0
    ignorados   = 0
    erros       = []
    agora       = _agora()
    conn        = _conectar()
    try:
        for lead in leads:
            numero    = (lead.get('numero') or '').strip()
            nome      = (lead.get('nome') or '').strip()
            interesse = (lead.get('interesse') or '').strip()
            if not numero:
                erros.append('Número vazio ignorado')
                continue
            try:
                cur2 = conn.execute(
                    "INSERT OR IGNORE INTO fila_importacao (numero, nome, interesse, adicionado_em) VALUES (?,?,?,?)",
                    (numero, nome, interesse, agora)
                )
                if cur2.rowcount > 0:
                    adicionados += 1
                else:
                    ignorados += 1
            except Exception as e:
                erros.append(f"{numero}: {e}")
        conn.commit()
    finally:
        conn.close()
    return {'adicionados': adicionados, 'ignorados': ignorados, 'erros': erros}


def ativar_proximos_importados(limite: int = 10) -> list[str]:
    """
    Ativa até `limite` leads da fila (os mais antigos ainda não ativados).
    Para cada um:
      1. Cria ou atualiza o contato com status_funil='importado'
      2. Inicia cadência de 3 toques (dias 3/12/28)
      3. Marca como ativado na fila
    Retorna lista de números ativados.
    """
    conn   = _conectar()
    agora  = _agora()
    ativados = []
    try:
        rows = conn.execute(
            "SELECT numero, nome, interesse FROM fila_importacao WHERE ativado_em IS NULL ORDER BY adicionado_em LIMIT ?",
            (limite,)
        ).fetchall()

        for row in rows:
            numero    = row[0] if isinstance(row, (list, tuple)) else row['numero']
            nome      = row[1] if isinstance(row, (list, tuple)) else row['nome']
            interesse = row[2] if isinstance(row, (list, tuple)) else row['interesse']
            try:
                # Cria contato se não existir
                conn.execute("""
                    INSERT INTO contatos (numero, nome, interesse, status_funil, bot_ativo, ficticio, adicionado_em, ultimo_contato)
                    VALUES (?,?,?,'importado',1,0,?,?)
                    ON CONFLICT(numero) DO UPDATE SET
                        status_funil='importado',
                        nome=CASE WHEN excluded.nome!='' THEN excluded.nome ELSE nome END,
                        bot_ativo=1
                """, (numero, nome, interesse, agora, agora))
                # Inicia cadência de 3 toques com intervalos 3/12/28
                from datetime import date, timedelta
                primeiro_envio = (date.today() + timedelta(days=_CADENCIA_IMPORTADO_DIAS[0])).isoformat()
                conn.execute("""
                    UPDATE contatos SET
                        cadencia_ativa=1,
                        cadencia_toque_atual=0,
                        cadencia_total_toques=3,
                        cadencia_proximo_envio=?,
                        cadencia_callback_em=NULL
                    WHERE numero=?
                """, (primeiro_envio, numero))
                # Marca como ativado
                conn.execute(
                    "UPDATE fila_importacao SET ativado_em=? WHERE numero=?",
                    (agora, numero)
                )
                ativados.append(numero)
            except Exception as e:
                print(f"⚠️  [IMPORTAR] Erro ao ativar {numero}: {e}")

        conn.commit()
    finally:
        conn.close()
    return ativados


def contar_fila_importacao() -> dict:
    """Retorna quantos leads estão na fila (aguardando e ativados)."""
    conn = _conectar()
    try:
        aguardando = conn.execute("SELECT COUNT(*) FROM fila_importacao WHERE ativado_em IS NULL").fetchone()[0]
        ativados   = conn.execute("SELECT COUNT(*) FROM fila_importacao WHERE ativado_em IS NOT NULL").fetchone()[0]
        return {'aguardando': aguardando, 'ativados': ativados, 'total': aguardando + ativados}
    finally:
        conn.close()



# ─────────────────────────────────────────────────────────────────────────────
# RESET DO BANCO (admin only) — apaga dados, mantém estrutura
# ─────────────────────────────────────────────────────────────────────────────

def resetar_banco() -> dict:
    """
    Apaga TODOS os dados do banco preservando as tabelas e seus esquemas.
    USE COM CUIDADO — operação irreversível.
    Retorna {'tabelas_limpas': [...], 'ok': True}.
    """
    tabelas = [
        'mensagens',
        'agendamentos',
        'lembretes',
        'conversa_estado',
        'horarios_agenda',
        'fila_importacao',
        'contatos',          # por último (FK reference)
    ]
    conn = _conectar()
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        for tabela in tabelas:
            try:
                conn.execute(f"DELETE FROM {tabela}")
                print(f"🗑️  [RESET] Tabela '{tabela}' limpa.")
            except Exception as e:
                print(f"⚠️  [RESET] Erro ao limpar '{tabela}': {e}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.commit()
        return {'tabelas_limpas': tabelas, 'ok': True}
    finally:
        conn.close()
