-- Banco de dados SQLite para Larissa (Fase 1 — Memória Persistente)
-- Reabilita Odontologia Personalizada
-- Sintaxe SQLite: INTEGER PRIMARY KEY AUTOINCREMENT (sem SERIAL)

-- ==================== CONTATOS ====================
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

-- ==================== MENSAGENS ====================
CREATE TABLE IF NOT EXISTS mensagens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    numero TEXT NOT NULL,
    papel TEXT NOT NULL,
    texto TEXT NOT NULL,
    intencao TEXT DEFAULT '',
    timestamp TEXT
);

-- ==================== AGENDAMENTOS ====================
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

-- ==================== LEMBRETES (fila para Fase 3) ====================
CREATE TABLE IF NOT EXISTS lembretes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    numero_destino TEXT NOT NULL,
    tipo TEXT DEFAULT '',
    mensagem TEXT NOT NULL,
    data_hora_envio TEXT,
    executado INTEGER DEFAULT 0,
    criado_em TEXT
);

-- ==================== ÍNDICES ====================
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
