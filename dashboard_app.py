"""
dashboard_app.py — Instância separada do dashboard Reabilita para deploy em nuvem.
Serve apenas as rotas de API do dashboard + arquivos estáticos.
O banco de dados é compartilhado via Turso (TURSO_URL + TURSO_AUTH_TOKEN no .env).

Deploy sugerido: Railway ou Render (gratuito).
  - Comando de start: python dashboard_app.py
  - Variáveis de ambiente necessárias:
      TURSO_URL          → libsql://SEU-BANCO.turso.io
      TURSO_AUTH_TOKEN   → token gerado no Turso
      DASH_SENHA_DUDA    → senha da Duda
      DASH_SENHA_ADMIN   → senha do administrador
      OPENAI_API_KEY     → (opcional, somente se quiser usar recursos de IA)
"""

import os, sys
# Garante que importamos db.memory e static/ do diretório do projeto
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import secrets
from datetime import datetime, timedelta
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
import uvicorn

load_dotenv()

# ── AUTENTICAÇÃO ──────────────────────────────────────────────────────────────
_SESSOES: dict = {}
_DASH_SENHA_DUDA  = os.getenv('DASH_SENHA_DUDA', 'duda123')
_DASH_SENHA_ADMIN = os.getenv('DASH_SENHA_ADMIN', 'admin123')
_COOKIE_NAME = 'larissa_session'
_SESSION_TTL = 12  # horas

def _criar_sessao(perfil: str) -> str:
    token = secrets.token_hex(32)
    _SESSOES[token] = {'perfil': perfil, 'expira': datetime.now() + timedelta(hours=_SESSION_TTL)}
    return token

def _validar_sessao(request: Request):
    token = request.cookies.get(_COOKIE_NAME)
    if not token: return None
    sess = _SESSOES.get(token)
    if not sess or datetime.now() > sess['expira']:
        _SESSOES.pop(token, None); return None
    return sess['perfil']

# ── IMPORTS DO BANCO ──────────────────────────────────────────────────────────
from db.memory import (
    init_db, obter_contato, listar_leads_recentes, obter_historico,
    atualizar_contato, atualizar_funil, atualizar_flag_atrito,
    marcar_dashboard_lido, salvar_mensagem,
    entrar_cadencia, cancelar_cadencia_completa,
    obter_funil_resumo, listar_por_funil,
)

app = FastAPI(title="Reabilita Dashboard", docs_url=None, redoc_url=None)
init_db()

# Arquivos estáticos
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── AUTH ──────────────────────────────────────────────────────────────────────
@app.post("/api/auth/login")
async def auth_login(request: Request, response: Response):
    body = await request.json()
    perfil, senha = body.get('perfil',''), body.get('senha','')
    if perfil == 'duda' and senha == _DASH_SENHA_DUDA: pass
    elif perfil == 'admin' and senha == _DASH_SENHA_ADMIN: pass
    else: raise HTTPException(status_code=401, detail="Senha incorreta")
    token = _criar_sessao(perfil)
    response.set_cookie(key=_COOKIE_NAME, value=token, httponly=True, samesite='lax', max_age=_SESSION_TTL*3600)
    return {"ok": True, "perfil": perfil}

@app.post("/api/auth/logout")
async def auth_logout(request: Request, response: Response):
    token = request.cookies.get(_COOKIE_NAME)
    if token: _SESSOES.pop(token, None)
    response.delete_cookie(_COOKIE_NAME)
    return {"ok": True}

@app.get("/api/auth/me")
async def auth_me(request: Request):
    perfil = _validar_sessao(request)
    if not perfil: raise HTTPException(status_code=401, detail="Não autenticado")
    return {"perfil": perfil}

# ── API LEADS ─────────────────────────────────────────────────────────────────
@app.get("/api/leads")
async def listar_leads():
    leads = listar_leads_recentes(limite=50)
    return JSONResponse(content={"total": len(leads), "leads": leads}, headers={"Cache-Control":"no-store"})

@app.get("/api/leads/{numero}")
async def obter_lead(numero: str):
    contato = obter_contato(numero)
    if not contato: raise HTTPException(status_code=404, detail="Lead não encontrado")
    return JSONResponse(content={"lead": contato, "historico": obter_historico(numero, limite=100)}, headers={"Cache-Control":"no-store"})

@app.patch("/api/leads/{numero}/funil")
async def api_funil(numero: str, body: dict):
    status = body.get('status','')
    ok = atualizar_funil(numero, status)
    if not ok: raise HTTPException(status_code=400, detail="Status inválido ou lead não encontrado")
    return {"ok": True, "status": status}

@app.post("/api/leads/{numero}/marcar-lido")
async def api_marcar_lido(numero: str):
    marcar_dashboard_lido(numero)
    return {"ok": True}

@app.patch("/api/leads/{numero}/cadencia/toggle")
async def api_cadencia_toggle(numero: str, body: dict):
    ativar = body.get('ativar', True)
    if ativar:
        entrar_cadencia(numero)
        return {"ok": True, "cadencia": True}
    else:
        cancelar_cadencia_completa(numero)
        return {"ok": True, "cadencia": False}

# ── ENVIO MANUAL (dashboard na nuvem nao envia via WhatsApp — apenas salva) ──
@app.post("/api/leads/{numero}/mensagem")
async def enviar_mensagem_nuvem(numero: str, request: Request):
    dados = await request.json()
    mensagem = dados.get('mensagem', '').strip()
    if not mensagem: raise HTTPException(status_code=400, detail="Mensagem vazia")
    salvar_mensagem(numero, 'ATENDENTE', mensagem)
    # Nota: o dashboard hospedado não envia via WhatsApp Bridge (roda no PC local).
    return {"ok": True, "enviado_whatsapp": False, "aviso": "Dashboard em nuvem: mensagem salva, envio WhatsApp feito pelo PC local."}

# ── PÁGINAS ───────────────────────────────────────────────────────────────────
@app.get("/dashboard")
async def dashboard_page(request: Request):
    perfil = _validar_sessao(request)
    if not perfil: return FileResponse("static/login.html")
    return FileResponse("static/index.html")

@app.get("/")
async def root():
    return {"nome": "Reabilita Dashboard", "status": "online", "dashboard": "/dashboard"}

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    print(f"🌐 Dashboard hospedado rodando na porta {port}")
    uvicorn.run("dashboard_app:app", host="0.0.0.0", port=port, reload=False)
