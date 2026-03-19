import os
import asyncio
import logging
from typing import List, Optional, Dict, Any
from datetime import datetime
import pytz
from collections import Counter
import aiohttp
from telegram import Bot
from telegram.error import TelegramError
from dotenv import load_dotenv
import math
import random

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7891678107:AAEmt_oN2Safe_2gEPS7x7XNeP8AA4hQXCI")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "-1003779669370")
API_URL = "https://api.signals-house.com/validate/results?tableId=27&lastResult=13382685"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

ANGOLA_TZ = pytz.timezone('Africa/Luanda')

OUTCOME_MAP = {
    "Casa": "🔴",
    "Visitante": "🔵",
    "Tie": "🟡",
    "Empate": "🟡",
}

# ─── STICKERS ───
GREEN_STICKER_ID = "CAACAgEAAxkBAAMGablwQw7_e6LQPpkPLMGUT_7XHlsAApECAAJAHLhE5HwaQw6L5SA6BA"
LOSS_STICKER_ID = "CAACAgEAAxkBAAMHablwRJ3yTERtooEJKzCbGMCfvv8AAucCAALGBLlEm0eHrOWqoe06BA"

# ═══════════════════════════════════════════════
# INTERVALOS
# ═══════════════════════════════════════════════
API_POLL_INTERVAL = 0.5
SIGNAL_COOLDOWN_DURATION = 4.5

# ═══════════════════════════════════════════════
# CONSTANTES DE ANÁLISE ESTATÍSTICA (do script 2)
# ═══════════════════════════════════════════════
JANELA_PRINCIPAL = 36
JANELA_EMPATE = 20
JANELA_ENTROPIA = 12
MIN_DESVIO_PORCENTAGEM = 4.8
MIN_CONFANCA = 59.0
MAX_TAXA_EMPATE_RECENTE = 14.0
P_CASA = 44.5
P_VISITANTE = 44.5
P_TIE = 11.0

# ═══════════════════════════════════════════════
# CONTROLE DE TIMING INTELIGENTE
# ═══════════════════════════════════════════════
MARGEM_SEGURANCA_ANTES = 3.0    # enviar sinal pelo menos 3s antes da próxima rodada
MARGEM_SEGURANCA_DEPOIS = 8.0   # no máximo 8s antes
MAX_TIMESTAMPS_GUARDADOS = 20   # quantos timestamps guardar para calcular intervalo médio

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)-5s | %(message)s')
logger = logging.getLogger("FootballStudioBot")

bot = Bot(token=TELEGRAM_BOT_TOKEN)

state: Dict[str, Any] = {
    "history": [], "last_round_id": None, "waiting_for_result": False,
    "last_signal_color": None, "martingale_count": 0, "entrada_message_id": None,
    "martingale_message_ids": [], "greens_seguidos": 0, "total_greens": 0,
    "greens_sem_gale": 0, "greens_gale_1": 0,
    "total_empates": 0, "total_losses": 0,
    "signal_cooldown_until": 0.0, "analise_message_id": None,
    "last_result_round_id": None,
    # Timing inteligente
    "result_timestamps": [],        # timestamps dos últimos resultados
    "avg_round_interval": None,     # intervalo médio entre rodadas (segundos)
    "last_result_time": 0.0,        # timestamp do último resultado recebido
    "signal_scheduled": False,      # se há sinal agendado para enviar
    "signal_scheduled_cor": None,
    "signal_scheduled_nome": None,
    "signal_send_at": 0.0,          # quando enviar o sinal agendado
    "signal_for_after_round_id": None,  # round_id após o qual o sinal é válido
}


async def send_to_channel(text: str, parse_mode="HTML") -> Optional[int]:
    try:
        msg = await bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=text, parse_mode=parse_mode, disable_web_page_preview=True)
        return msg.message_id
    except Exception as e:
        logger.error(f"Erro ao enviar texto: {e}")
        return None


async def send_sticker_to_channel(sticker_id: str) -> Optional[int]:
    try:
        msg = await bot.send_sticker(chat_id=TELEGRAM_CHANNEL_ID, sticker=sticker_id)
        return msg.message_id
    except Exception as e:
        logger.error(f"Erro ao enviar sticker: {e}")
        return None


async def delete_messages(message_ids: List[int]):
    if not message_ids:
        return
    for mid in message_ids[:]:
        try:
            await bot.delete_message(TELEGRAM_CHANNEL_ID, mid)
        except:
            pass


def calcular_acertividade() -> str:
    total = state["total_greens"] + state["total_losses"]
    return "—" if total == 0 else f"{(state['total_greens'] / total * 100):.1f}%"


def format_placar() -> str:
    acert = calcular_acertividade()
    return (
        f"📊 Placar atual 🟢 {state['total_greens']} 🔴 {state['total_losses']}\n"
        f"✅ Assertividade {acert}\n"
        f"🏆 {state['greens_seguidos']} Greens seguidos"
    )


def format_analise_text() -> str:
    return "⚽ <b>ANALISANDO FOOTBALL STUDIO...</b> ⚽\n<i>Aguarde sinal</i>"


async def refresh_analise_message():
    await delete_analise_message()
    msg_id = await send_to_channel(format_analise_text())
    if msg_id:
        state["analise_message_id"] = msg_id


async def delete_analise_message():
    if state["analise_message_id"] is not None:
        await delete_messages([state["analise_message_id"]])
        state["analise_message_id"] = None


def reset_placar_if_needed():
    """Reseta o placar quando atinge 500 greens."""
    if state["total_greens"] >= 500:
        for k in ["total_greens", "greens_sem_gale", "greens_gale_1",
                   "total_empates", "total_losses", "greens_seguidos"]:
            state[k] = 0
        logger.info("Placar resetado (500 greens atingidos)")


async def fetch_api(session: aiohttp.ClientSession) -> Optional[Dict]:
    try:
        async with session.get(API_URL, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                return await resp.json()
            return None
    except Exception as e:
        logger.debug(f"Erro fetch API: {e}")
        return None


def atualizar_intervalo_medio():
    """Calcula o intervalo médio entre rodadas com base nos timestamps recentes."""
    ts_list = state["result_timestamps"]
    if len(ts_list) < 2:
        state["avg_round_interval"] = None
        return
    intervalos = [ts_list[i] - ts_list[i - 1] for i in range(1, len(ts_list))]
    # Remover outliers (intervalos maiores que 120s provavelmente são pausas)
    intervalos_filtrados = [i for i in intervalos if i < 120]
    if not intervalos_filtrados:
        state["avg_round_interval"] = None
        return
    state["avg_round_interval"] = sum(intervalos_filtrados) / len(intervalos_filtrados)
    logger.info(f"Intervalo médio entre rodadas: {state['avg_round_interval']:.1f}s")


async def update_history_from_api(session):
    data = await fetch_api(session)
    if not data:
        return False
    try:
        items = data.get("data", [])
        if not isinstance(items, list) or len(items) == 0:
            return False
        latest = items[0]
        round_id = latest.get("id")
        if not round_id or round_id == state["last_round_id"]:
            return False
        outcome_raw = latest.get("result")
        if not outcome_raw:
            return False
        outcome = OUTCOME_MAP.get(outcome_raw)
        if not outcome:
            s = str(outcome_raw or "").lower()
            if "casa" in s: outcome = "🔴"
            elif "visitante" in s: outcome = "🔵"
            elif "tie" in s or "empate" in s: outcome = "🟡"
        if outcome:
            now = datetime.now().timestamp()
            state["last_round_id"] = round_id
            state["history"].append(outcome)
            if len(state["history"]) > 500:
                state["history"].pop(0)
            # Registrar timestamp para cálculo de intervalo
            state["result_timestamps"].append(now)
            if len(state["result_timestamps"]) > MAX_TIMESTAMPS_GUARDADOS:
                state["result_timestamps"].pop(0)
            state["last_result_time"] = now
            atualizar_intervalo_medio()
            logger.info(f"Novo resultado adicionado: {outcome} (id {round_id})")
            return True
        return False
    except Exception as e:
        logger.debug(f"Erro processando API: {e}")
        return False


# ────────────────────────────────────────────────
# ANÁLISE ESTATÍSTICA (estratégias do script 2)
# ────────────────────────────────────────────────

def calcular_entropia_binaria(p: float) -> float:
    if p <= 0 or p >= 1:
        return 0.0
    return - (p * math.log2(p) + (1 - p) * math.log2(1 - p))


def proporcao_na_janela(hist: List[str], janela: int) -> tuple[float, float, float]:
    if len(hist) < 3:
        return 0.0, 0.0, 0.0
    janela_real = min(janela, len(hist))
    recorte = hist[-janela_real:]
    c = Counter(recorte)
    n = len(recorte)
    p_c = c["🔴"] / n * 100 if n > 0 else 0
    p_v = c["🔵"] / n * 100 if n > 0 else 0
    p_t = c["🟡"] / n * 100 if n > 0 else 0
    return p_c, p_v, p_t


def desvio_da_esperada(p_obs: float, p_esperada: float) -> float:
    return abs(p_obs - p_esperada)


def gerar_sinal_inteligente(
    history: List[str]
) -> tuple[Optional[str], Optional[str], float]:
    if len(history) < 12:
        return None, None, 0.0

    p_c, p_v, p_t = proporcao_na_janela(history, JANELA_PRINCIPAL)
    p_c_short, p_v_short, p_t_short = proporcao_na_janela(history, JANELA_EMPATE)

    if p_t_short > MAX_TAXA_EMPATE_RECENTE:
        return "Muitos empates recentes", None, 0.0

    desv_c = desvio_da_esperada(p_c, P_CASA)
    desv_v = desvio_da_esperada(p_v, P_VISITANTE)

    ent = 1.0
    if len(history) >= JANELA_ENTROPIA:
        recorte = history[-JANELA_ENTROPIA:]
        c = Counter(x for x in recorte if x in ("🔴", "🔵"))
        n_bin = sum(c.values())
        if n_bin >= 6:
            p_bin = c["🔴"] / n_bin
            ent = calcular_entropia_binaria(p_bin)

    score = 0.0
    cor_favor = None

    if desv_c > MIN_DESVIO_PORCENTAGEM and p_c > p_v + 2:
        score += (desv_c - MIN_DESVIO_PORCENTAGEM) * 1.8
        cor_favor = "🔴"
    elif desv_v > MIN_DESVIO_PORCENTAGEM and p_v > p_c + 2:
        score += (desv_v - MIN_DESVIO_PORCENTAGEM) * 1.8
        cor_favor = "🔵"

    if ent < 0.78:
        score += (0.92 - ent) * 2.2

    if abs(p_c_short - p_v_short) < 3.5:
        score *= 0.55

    if score < 1.6 or cor_favor is None:
        return "Sem força estatística suficiente", None, 0.0

    confianca = min(78.0, 52.0 + score * 4.2)
    if confianca < MIN_CONFANCA:
        return "Confiança abaixo do mínimo", None, confianca

    nome = "Desequilíbrio estatístico"
    if ent < 0.75:
        nome += " + baixa entropia"

    return nome, cor_favor, round(confianca, 1)


def gerar_sinal_estrategia(history: List[str]):
    nome, cor, confianca = gerar_sinal_inteligente(history)
    if cor is None:
        return None, None
    return f"{nome} ({confianca}%)", cor


# ────────────────────────────────────────────────
# MENSAGENS E SINAIS (formato do script 1)
# ────────────────────────────────────────────────

def main_entry_text(nome: str, color: str) -> str:
    if color == "🔴":
        lado = "CASA 🔴"
    else:
        lado = "VISITANTE 🔵"
    return (
        f"🧠 | Sinal confirmado\n"
        f"⚽️ | Mesa Football Studio\n"
        f"⚔️ | Aposte no {lado} + 🟠\n"
        f"♻️ | Fazer máximo G1\n"
        f"💻 | Abra o jogo pelo link abaixo ⤵️\n"
        f"\n"
        f'<a href="https://btt-pt.hopghpfa.com/pt/casino?partner=p8506p33116p4649#registration-bonus">👉Regista-te aqui: BETILT</a>'
    )


async def send_gale_warning():
    text = "🔄 <b>GALE 1</b> 🔄\nContinuar na mesma cor!"
    msg_id = await send_to_channel(text)
    if msg_id:
        state["martingale_message_ids"].append(msg_id)


async def clear_gale_messages():
    await delete_messages(state["martingale_message_ids"])
    state["martingale_message_ids"] = []


async def resolve_after_result():
    if not state.get("waiting_for_result") or not state.get("last_signal_color"):
        return
    if not state["history"]:
        return
    if state["last_result_round_id"] == state["last_round_id"]:
        return

    state["last_result_round_id"] = state["last_round_id"]
    last_outcome = state["history"][-1]
    target = state["last_signal_color"]
    acertou = last_outcome == target
    is_tie = last_outcome == "🟡"

    if acertou or is_tie:
        state["total_greens"] += 1
        state["greens_seguidos"] += 1
        if state["martingale_count"] == 0:
            state["greens_sem_gale"] += 1
        elif state["martingale_count"] == 1:
            state["greens_gale_1"] += 1

        await send_sticker_to_channel(GREEN_STICKER_ID)
        await send_to_channel(format_placar())
        await clear_gale_messages()
        state.update({
            "waiting_for_result": False, "last_signal_color": None,
            "martingale_count": 0, "entrada_message_id": None,
        })
        reset_placar_if_needed()
        await refresh_analise_message()
        return

    # Errou
    state["martingale_count"] += 1
    if state["martingale_count"] == 1:
        await send_gale_warning()
        return

    # Após gale 1 errado → LOSS
    if state["martingale_count"] >= 2:
        state["greens_seguidos"] = 0
        state["total_losses"] += 1
        await send_sticker_to_channel(LOSS_STICKER_ID)
        await send_to_channel(format_placar())
        await clear_gale_messages()
        state.update({
            "waiting_for_result": False, "last_signal_color": None,
            "martingale_count": 0, "entrada_message_id": None,
        })
        reset_placar_if_needed()
        await refresh_analise_message()


def cancelar_sinal_agendado():
    """Cancela qualquer sinal pendente."""
    if state["signal_scheduled"]:
        logger.info("Sinal agendado cancelado (novo resultado já saiu)")
    state["signal_scheduled"] = False
    state["signal_scheduled_cor"] = None
    state["signal_scheduled_nome"] = None
    state["signal_send_at"] = 0.0
    state["signal_for_after_round_id"] = None


def agendar_sinal():
    """Após novo resultado, analisa e agenda sinal para a próxima rodada."""
    now = datetime.now().timestamp()

    if state["waiting_for_result"]:
        return
    if now < state["signal_cooldown_until"]:
        return
    if len(state["history"]) < 12:
        return

    nome, cor = gerar_sinal_estrategia(state["history"])
    if not cor:
        return

    avg = state["avg_round_interval"]
    if avg and avg > 10:
        # Enviar entre 3 e 8 segundos antes da próxima rodada estimada
        tempo_ate_proxima = avg
        margem = random.uniform(MARGEM_SEGURANCA_ANTES, min(MARGEM_SEGURANCA_DEPOIS, tempo_ate_proxima * 0.5))
        delay = max(0.5, tempo_ate_proxima - margem)
    else:
        # Sem dados suficientes, enviar após um pequeno delay
        delay = random.uniform(2.0, 5.0)

    send_at = now + delay
    state["signal_scheduled"] = True
    state["signal_scheduled_cor"] = cor
    state["signal_scheduled_nome"] = nome
    state["signal_send_at"] = send_at
    state["signal_for_after_round_id"] = state["last_round_id"]
    logger.info(f"Sinal agendado: {cor} em {delay:.1f}s (intervalo médio: {avg:.1f}s)" if avg else f"Sinal agendado: {cor} em {delay:.1f}s")


async def try_send_scheduled_signal():
    """Envia o sinal agendado se estiver na janela segura."""
    if not state["signal_scheduled"]:
        return
    if state["waiting_for_result"]:
        cancelar_sinal_agendado()
        return

    now = datetime.now().timestamp()

    # Verificar se o round_id mudou desde o agendamento (resultado já saiu)
    if state["signal_for_after_round_id"] != state["last_round_id"]:
        logger.info("Sinal cancelado: novo resultado já saiu antes do envio")
        cancelar_sinal_agendado()
        return

    # Ainda não chegou a hora de enviar
    if now < state["signal_send_at"]:
        return

    # Verificar janela segura: se o intervalo médio existe, checar se ainda estamos
    # dentro do tempo esperado antes da próxima rodada
    avg = state["avg_round_interval"]
    if avg:
        tempo_desde_ultimo = now - state["last_result_time"]
        # Se já passou mais tempo que o intervalo médio, o resultado pode sair a qualquer momento
        if tempo_desde_ultimo > avg + 2.0:
            logger.info("Sinal cancelado: fora da janela segura (tempo expirado)")
            cancelar_sinal_agendado()
            return

    cor = state["signal_scheduled_cor"]
    nome = state["signal_scheduled_nome"]
    cancelar_sinal_agendado()

    await delete_analise_message()
    state["martingale_message_ids"] = []
    texto = main_entry_text(nome, cor)
    msg_id = await send_to_channel(texto)
    if msg_id:
        state["entrada_message_id"] = msg_id
        state["waiting_for_result"] = True
        state["last_signal_color"] = cor
        state["martingale_count"] = 0
        state["signal_cooldown_until"] = now + SIGNAL_COOLDOWN_DURATION
        logger.info(f"Sinal enviado → {cor} ({nome})")


async def api_worker():
    connector = aiohttp.TCPConnector(limit=5, keepalive_timeout=30)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            try:
                updated = await update_history_from_api(session)

                if updated:
                    # Se havia sinal agendado e novo resultado saiu, cancelar
                    if state["signal_scheduled"] and state["signal_for_after_round_id"] != state["last_round_id"]:
                        cancelar_sinal_agendado()

                    # 1. Validar sinal anterior
                    await resolve_after_result()

                    # 2. Agendar novo sinal para próxima rodada
                    if not state["waiting_for_result"]:
                        agendar_sinal()

                # 3. Tenta enviar sinal agendado se a hora chegou
                await try_send_scheduled_signal()

            except Exception as e:
                logger.debug(f"Erro no loop principal: {e}")

            await asyncio.sleep(API_POLL_INTERVAL)


async def main():
    logger.info("Bot Football Studio iniciado...")
    await send_to_channel("🤖 CLEVER BOT INICIADO 🤖")
    await refresh_analise_message()
    await api_worker()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot parado pelo usuário")
    except Exception as e:
        logger.critical("Erro fatal", exc_info=True)
