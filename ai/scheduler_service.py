"""
Scheduler de confirmações e lembretes automáticos (Fase 3 / Item 4).
Roda em background, verificando periodicamente:
- Entre 14h-17h do dia anterior: envia confirmação para consultas de amanhã
- ~1h antes da consulta: envia lembrete "estamos aguardando"
- A cada 30s: despacha mensagens agendadas (Bloco C) e lembretes cruzados (Item 1)

Item 4: usa agenda interna SQLite — não depende do Google Calendar.
"""

import time
import datetime
import pytz
import requests
from threading import Thread
from db.memory import (
    obter_lembretes_pendentes,
    marcar_lembrete_executado,
    listar_slots_amanha_ocupados,
    listar_slots_hoje_ocupados,
    atualizar_status_slot_por_id,
    marcar_nao_confirmado,
    marcar_atendidos_passados,
    # Cadência de reengajamento
    listar_cadencias_pendentes,
    listar_para_ativar_cadencia,
    listar_nao_compareceu_pendentes,
    entrar_cadencia,
    avancar_toque_cadencia,
    cancelar_callback_cadencia,
    atualizar_funil,
    obter_historico,
    # Fila de importação
    ativar_proximos_importados,
)

TZ = pytz.timezone('America/Belem')
WHATSAPP_BRIDGE_URL = "http://localhost:3001/enviar"
INTERVALO_VERIFICACAO_MIN = 15   # confirmações/lembretes a cada 15 min
INTERVALO_MSG_AGENDADA_SEG = 30  # mensagens agendadas a cada 30 s


def enviar_mensagem_direta(numero, mensagem):
    """
    Envia mensagem via whatsapp_bridge SEM passar pela IA.
    Usado para confirmações e lembretes automáticos.
    """
    try:
        resp = requests.post(WHATSAPP_BRIDGE_URL,
                             json={'phone': numero, 'message': mensagem},
                             timeout=10)
        if resp.status_code == 200:
            print(f"📤 Mensagem automática enviada para {numero}: {mensagem[:50]}...")
            return True
        else:
            print(f"⚠️ Falha ao enviar mensagem: HTTP {resp.status_code}")
            return False
    except Exception as e:
        print(f"❌ Erro ao enviar mensagem via bridge: {e}")
        return False


def verificar_e_enviar_confirmacoes():
    """
    Entre 14h-17h, busca slots OCUPADOS de amanhã e envia confirmação.
    Após enviar, muda o status do slot para 'nao_confirmado'.
    Item 4: usa listar_slots_amanha_ocupados() em vez do Google Calendar.
    """
    agora = datetime.datetime.now(TZ)
    hora_atual = agora.hour

    # Só roda entre 14h e 17h
    if not (14 <= hora_atual < 17):
        return

    slots = listar_slots_amanha_ocupados()
    if not slots:
        return

    print(f"📋 {len(slots)} slot(s) de amanhã precisam de confirmação")

    for slot in slots:
        telefone = slot.get('paciente_numero', '')
        nome = slot.get('paciente_nome', '') or 'Paciente'
        hora = slot.get('hora_inicio', '')
        slot_id = slot.get('id')

        if not telefone:
            print(f"⚠️ Slot id={slot_id} ({nome} às {hora}) sem telefone — pulando confirmação")
            continue

        # Calcula data de amanhã + dia da semana em português
        amanha_dt = datetime.datetime.now(TZ) + datetime.timedelta(days=1)
        data_fmt = amanha_dt.strftime('%d/%m/%Y')
        dias_semana_pt = ['Segunda-feira', 'Terça-feira', 'Quarta-feira',
                          'Quinta-feira', 'Sexta-feira', 'Sábado', 'Domingo']
        dia_semana_str = dias_semana_pt[amanha_dt.weekday()]

        # Saudação baseada no horário atual
        hora_atual = datetime.datetime.now(TZ).hour
        if hora_atual < 12:
            saudacao = 'Bom dia'
        elif hora_atual < 18:
            saudacao = 'Boa tarde'
        else:
            saudacao = 'Boa noite'

        primeiro_nome = nome.split()[0] if nome else 'Paciente'
        mensagem = (
            f"*Confirmação de Consulta* ⚠️\n\n"
            f"{saudacao}, {primeiro_nome}! 😊\n\n"
            f"Sua consulta na *Reabilita!* ❤️ está agendada para amanhã:\n"
            f"*Data:* {data_fmt} - *{dia_semana_str}*\n"
            f"*Hora:* {hora}\n\n"
            f"Já estamos ansiosos!!! 🥰\n\n"
            f"Digite: *1* para *Confirmar*\n"
            f"Digite: *2* para *Reagendar*"
        )

        if enviar_mensagem_direta(telefone, mensagem):
            # Marca 'agendado'/'ocupado' → 'nao_confirmado' (pedido enviado,
            # aguardando resposta do paciente).
            marcar_nao_confirmado(slot_id)
            print(f"✅ Confirmação enviada + status→nao_confirmado: {nome} ({hora})")


def verificar_e_enviar_lembretes():
    """
    Verifica slots de hoje que começam em ~1h e ainda estão ocupados/confirmado.
    Envia lembrete "estamos aguardando você".
    Item 4: usa listar_slots_hoje_ocupados() em vez do Google Calendar.
    """
    agora = datetime.datetime.now(TZ)
    slots = listar_slots_hoje_ocupados()

    for slot in slots:
        hora_str = slot.get('hora_inicio', '')
        if not hora_str:
            continue

        try:
            h, m = hora_str.split(':')
            hoje = agora.date()
            inicio = TZ.localize(
                datetime.datetime.combine(hoje, datetime.time(int(h), int(m)))
            )
        except Exception:
            continue

        # Janela: entre 50 e 70 minutos no futuro
        diff_min = (inicio - agora).total_seconds() / 60
        if not (50 <= diff_min <= 70):
            continue

        telefone = slot.get('paciente_numero', '')
        nome = slot.get('paciente_nome', '') or 'Paciente'

        if not telefone:
            print(f"⚠️ Slot {hora_str} sem telefone — pulando lembrete")
            continue

        primeiro_nome = nome.split()[0] if nome else 'Paciente'
        mensagem = (
            f"Oi {primeiro_nome}! Sua consulta na *Reabilita!* ❤️ é daqui a 1 hora! ⏰\n\n"
            f"Já preparamos tudo pra te receber com delicioso café gourmet ☕\n\n"
            f"Digite *1* - Estou indo e vou precisar de vaga estacionamento\n"
            f"Digite *2* - Estou indo mas não preciso de vaga\n\n"
            f"Qualquer outra situação pode me falar por aqui."
        )

        if enviar_mensagem_direta(telefone, mensagem):
            print(f"🔔 Lembrete 1h enviado: {nome} ({hora_str})")


def verificar_e_enviar_mensagens_agendadas():
    """
    Verifica lembretes do banco (feedback_agendado + lembrete_cruzado) que
    passaram da hora de envio. Envia via bridge e marca como executado.
    """
    try:
        pendentes = obter_lembretes_pendentes()
    except Exception as e:
        print(f"❌ Erro ao buscar lembretes pendentes: {e}")
        return

    for lembrete in pendentes:
        tipo = lembrete.get('tipo')
        if tipo not in ('feedback_agendado', 'lembrete_cruzado'):
            continue

        numero = lembrete.get('numero_destino', '')
        mensagem = lembrete.get('mensagem', '')
        lembrete_id = lembrete.get('id')

        if not numero or not mensagem:
            marcar_lembrete_executado(lembrete_id)
            continue

        print(f"📤 Enviando mensagem agendada ({tipo}) para {numero}: {mensagem[:50]}...")
        if enviar_mensagem_direta(numero, mensagem):
            marcar_lembrete_executado(lembrete_id)
            print(f"✅ Mensagem agendada enviada e marcada (id={lembrete_id})")

            # Item 1 — lembrete cruzado: confirma ao solicitante que o disparo ocorreu.
            if tipo == 'lembrete_cruzado':
                solicitante = lembrete.get('solicitante_numero', '')
                nome_dest = lembrete.get('nome_destinatario') or 'a pessoa'
                if solicitante:
                    confirmacao = (f"✅ Enviei seu lembrete para *{nome_dest}*:\n_{mensagem}_")
                    enviar_mensagem_direta(solicitante, confirmacao)
                    print(f"📨 Confirmação de lembrete cruzado enviada p/ {solicitante}")
        else:
            print(f"⚠️ Falha ao enviar mensagem agendada (id={lembrete_id}) — retentará no próximo ciclo")


def _em_janela_cadencia(agora):
    """Retorna True se o horário atual está na janela de envio de cadência.
    Janelas: 10h–11h30 ou 18h30–20h30, de segunda a sexta."""
    dia_semana = agora.weekday()  # 0=seg, 6=dom
    if dia_semana >= 5:           # sábado/domingo fora da janela
        return False
    hora_min = agora.hour * 60 + agora.minute
    manha = 10 * 60 <= hora_min <= (11 * 60 + 30)
    noite = (18 * 60 + 30) <= hora_min <= (20 * 60 + 30)
    return manha or noite


def ativar_cadencias_automaticas():
    """Ativa cadência para leads que ainda não têm cadência ativa mas deveriam ter."""
    pendentes = listar_para_ativar_cadencia()
    for lead in pendentes:
        try:
            numero = lead['numero']
            status_funil = lead.get('status_funil', 'em_cadencia')
            total = 5 if status_funil == 'em_cadencia' else 3
            ok = entrar_cadencia(numero, total)
            if ok:
                print(f"🔄 [CADÊNCIA] Ativada para {numero} ({status_funil}, {total} toques)")
        except Exception as e:
            print(f"⚠️ [CADÊNCIA] Erro ao ativar para {lead.get('numero')}: {e}")


def processar_cadencias_pendentes():
    """Envia o próximo toque de cadência para leads com envio pendente."""
    # FIX (cadencia): gerar_mensagem_cadencia e uma FUNCAO de modulo, nao um
    # metodo da classe Larissa. Antes o scheduler chamava
    # `larissa.gerar_mensagem_cadencia(...)`, o que lancava AttributeError a cada
    # ciclo (engolido pelo try/except) e NENHUM toque era enviado. Importamos e
    # chamamos a funcao diretamente.
    from ai.larissa import gerar_mensagem_cadencia
    agora = datetime.datetime.now(TZ)
    if not _em_janela_cadencia(agora):
        return

    pendentes = listar_cadencias_pendentes()

    for lead in pendentes:
        numero = lead['numero']
        toque = lead.get('cadencia_toque_atual', 1)
        total = lead.get('cadencia_total_toques', 5)
        status_funil = lead.get('status_funil', 'em_cadencia')
        # É este toque um CALLBACK (dia que a Larissa combinou de voltar a falar)?
        eh_callback = bool(lead.get('cadencia_callback_em'))

        try:
            historico = obter_historico(numero)
            # Verifica que última mensagem recebida foi há >24h (anti-spam)
            ultima_recebida = None
            for msg in reversed(historico or []):
                if msg.get('remetente') == 'PACIENTE':
                    # obter_historico() devolve a data no campo 'timestamp'
                    # (o 'criado_em' antigo nunca existia, entao o anti-spam
                    # ficava desligado — corrigido aqui).
                    ultima_recebida = msg.get('timestamp') or msg.get('criado_em')
                    break
            if ultima_recebida:
                try:
                    ts = datetime.datetime.fromisoformat(ultima_recebida.replace('Z', '+00:00'))
                    if (agora - ts.astimezone(TZ)).total_seconds() < 86400:
                        print(f"⏭️  [CADÊNCIA] {numero} respondeu há menos de 24h — pulando toque {toque}")
                        continue
                except Exception:
                    pass

            texto = gerar_mensagem_cadencia(
                numero=numero,
                toque=toque,
                total=total,
                status_funil=status_funil,
                historico=historico or []
            )
            if not texto:
                print(f"⚠️  [CADÊNCIA] Larissa retornou texto vazio para {numero} toque {toque}")
                continue

            enviar_mensagem_direta(numero, texto)
            # Se este era o disparo do CALLBACK combinado, limpa a data de callback
            # ANTES de avancar — assim ele nao redispara a cada ciclo dentro da
            # janela. A pessoa passa a seguir os toques normais de cadencia (se nao
            # agendar), exatamente como combinado.
            if eh_callback:
                try:
                    cancelar_callback_cadencia(numero)
                    print(f"📅 [CADÊNCIA] Callback de {numero} disparado e limpo — "
                          f"segue na cadência normal se não agendar.")
                except Exception as _e_cb:
                    print(f"⚠️  [CADÊNCIA] Erro ao limpar callback de {numero}: {_e_cb}")
            avancar_toque_cadencia(numero)
            print(f"✅ [CADÊNCIA] Toque {toque}/{total} enviado para {numero}")

        except Exception as e:
            print(f"❌ [CADÊNCIA] Erro ao processar {numero}: {e}")


def processar_nao_compareceu():
    """Para leads marcados como não compareceu: envia acolhimento e inicia cadência."""
    pendentes = listar_nao_compareceu_pendentes()
    for lead in pendentes:
        numero = lead['numero']
        nome = lead.get('nome') or 'paciente'
        try:
            msg = (
                f"Oi {nome.split()[0]}! 😊 Notamos que você não pôde comparecer hoje "
                "à sua consulta na Reabilita. Fique tranquilo(a), essas coisas acontecem! "
                "Quando quiser, é só me chamar aqui que a gente encontra um novo horário pra você. 💙"
            )
            enviar_mensagem_direta(numero, msg)
            atualizar_funil(numero, 'em_cadencia')
            entrar_cadencia(numero, 5)
            print(f"💌 [NÃO COMPARECEU] Acolhimento enviado para {numero} — cadência iniciada")
        except Exception as e:
            print(f"❌ [NÃO COMPARECEU] Erro ao processar {numero}: {e}")


def ativar_importados_dia():
    """
    Job diário das 09h: ativa os próximos 10 leads da fila de importação.
    Cada lead ativado entra com status_funil='importado' e cadência de 3 toques.
    Após esgotar os 3 toques sem resposta, avancar_toque_cadencia() move para 'banco'.
    """
    try:
        ativados = ativar_proximos_importados(10)
        if ativados:
            print(f"📥 [IMPORTAR] {len(ativados)} lead(s) ativado(s) da fila: {ativados}")
        else:
            print("📥 [IMPORTAR] Nenhum lead novo na fila de importação.")
    except Exception as e:
        print(f"❌ [IMPORTAR] Erro ao ativar importados: {e}")


def loop_scheduler():
    """
    Loop infinito em thread separada.
    """
    print(f"🕐 Scheduler iniciado — msgs agendadas a cada {INTERVALO_MSG_AGENDADA_SEG}s, "
          f"confirmações/lembretes a cada {INTERVALO_VERIFICACAO_MIN} min")

    time.sleep(60)  # aguarda o servidor subir

    ultima_verificacao_pesada = 0.0
    ultima_marcacao_atendidos = None   # data (date) em que rodou hoje
    ultima_ativacao_importados = None  # data (date) em que ativou importados hoje

    while True:
        try:
            agora = datetime.datetime.now(TZ)

            # 1) Mensagens agendadas: apenas banco local, roda com frequência
            verificar_e_enviar_mensagens_agendadas()

            # 2) Confirmações e lembretes: roda a cada INTERVALO_VERIFICACAO_MIN min
            if (time.time() - ultima_verificacao_pesada) >= INTERVALO_VERIFICACAO_MIN * 60:
                print(f"🔍 [{agora.strftime('%H:%M')}] Verificando confirmações e lembretes...")
                verificar_e_enviar_confirmacoes()
                verificar_e_enviar_lembretes()
                # 2b) Cadência: ativa automáticas + processa toques pendentes + não compareceu
                try:
                    ativar_cadencias_automaticas()
                    processar_cadencias_pendentes()
                    processar_nao_compareceu()
                except Exception as e_cad:
                    print(f"⚠️ Erro nos jobs de cadência: {e_cad}")
                ultima_verificacao_pesada = time.time()

            # 3) Job noturno de funil: marca atendidos passados (roda 1x/dia, entre 1h-2h)
            hoje = agora.date()
            if (ultima_marcacao_atendidos != hoje) and (1 <= agora.hour < 2):
                print(f"🌙 [{agora.strftime('%H:%M')}] Job noturno: marcando consultas passadas como 'atendido'...")
                try:
                    n = marcar_atendidos_passados()
                    print(f"✅ Funil noturno: {n} contato(s) atualizados para 'atendido'")
                except Exception as e_funil:
                    print(f"⚠️ Erro no job noturno de funil: {e_funil}")
                ultima_marcacao_atendidos = hoje

            # 4) Job das 09h: ativa próximos 10 da fila de importação
            if (ultima_ativacao_importados != hoje) and (9 <= agora.hour < 10):
                print(f"📥 [{agora.strftime('%H:%M')}] Ativando leads da fila de importação...")
                ativar_importados_dia()
                ultima_ativacao_importados = hoje

            time.sleep(INTERVALO_MSG_AGENDADA_SEG)

        except Exception as e:
            print(f"❌ Erro no scheduler: {e}")
            time.sleep(60)


def iniciar_scheduler():
    """
    Inicia o scheduler em background (chamado pelo main.py).
    Item 4: sempre inicia — não depende do Google Calendar.
    """
    thread = Thread(target=loop_scheduler, daemon=True)
    thread.start()
    print("✅ Scheduler de confirmações/lembretes iniciado em background (agenda interna)")
