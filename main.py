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
POST_RESULT_DELAY = 1.2

# ═══════════════════════════════════════════════
# CONSTANTES DE ANÁLISE ESTATÍSTICA
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

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)-5s | %(message)s')
logger = logging.getLogger("FootballStudioBot")

bot = Bot(token=TELEGRAM_BOT_TOKEN)

state: Dict[str, Any] = {
    "history": [], "last_round_id": None, "waiting_for_result": False,
    "last_signal_color": None, "martingale_count": 0, "entrada_message_id": None,
    "martingale_message_ids": [], "greens_seguidos": 0, "total_greens": 0,
    "greens_sem_gale": 0, "greens_gale_1": 0, "greens_gale_2": 0,
    "total_empates": 0, "total_losses": 0,
    "signal_cooldown_until": 0.0, "analise_message_id": None,
    "last_reset_date": None,
    "last_result_round_id": None,
    "next_signal_possible_after": 0.0,
}


def should_reset_placar() -> bool:
    now = datetime.now(ANGOLA_TZ)
    if state["last_reset_date"] != now.date():
        state["last_reset_date"] = now.date()
        return True
    if state["total_losses"] >= 10:
        return True
    if state["total_greens"] >= 500:
        return True
    return False


def reset_placar_if_needed():
    if should_reset_placar():
        for k in ["total_greens", "greens_sem_gale", "greens_gale_1", "greens_gale_2",
                  "total_empates", "total_losses", "greens_seguidos"]:
            state[k] = 0
        logger.info("Placar resetado")


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
        "🏆 <b>RESUMO</b> 🏆\n"
        f"✅ Sem gale: <b>{state['greens_sem_gale']}</b>\n"
        f"🔄 Gale 1: <b>{state['greens_gale_1']}</b>\n"
        f"🔄 Gale 2: <b>{state['greens_gale_2']}</b>\n"
        f"⛔ Losses: <b>{state['total_losses']}</b>\n"
        f"🎯 Greens: <b>{state['total_greens']}</b>  |  {acert}"
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


async def fetch_api(session: aiohttp.ClientSession) -> Optional[Dict]:
    try:
        async with session.get(API_URL, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                return await resp.json()
            return None
    except Exception as e:
        logger.debug(f"Erro fetch API: {e}")
        return None


async def update_history_from_api(session):
    reset_placar_if_needed()
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
            state["last_round_id"] = round_id
            state["history"].append(outcome)
            if len(state["history"]) > 500:
                state["history"].pop(0)
            logger.info(f"Novo resultado adicionado: {outcome} (id {round_id})")

            now = datetime.now().timestamp()
            state["next_signal_possible_after"] = now + POST_RESULT_DELAY
            return True

        return False
    except Exception as e:
        logger.debug(f"Erro processando API: {e}")
        return False


# ────────────────────────────────────────────────
# ANÁLISE ESTATÍSTICA (entropia + desvio + janelas)
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
# MENSAGENS E SINAIS
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
        f"♻️ | Fazer máximo G2\n"
        f"💻 | Abra o jogo pelo link abaixo ⤵️\n"
        f"\n"
        f'<a href="https://btt-pt.hopghpfa.com/pt/casino?partner=p8506p33116p4649#registration-bonus">👉Regista-te aqui: BETILT</a>'
    )


async def send_gale_warning(level: int):
    if level not in (1, 2):
        return
    text = f"🔄 <b>GALE {level}</b> 🔄\nContinuar na mesma cor!"
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

    now = datetime.now().timestamp()
    state["next_signal_possible_after"] = now + POST_RESULT_DELAY

    if acertou or is_tie:
        state["total_greens"] += 1
        state["greens_seguidos"] += 1
        if state["martingale_count"] == 0:
            state["greens_sem_gale"] += 1
        elif state["martingale_count"] == 1:
            state["greens_gale_1"] += 1
        elif state["martingale_count"] == 2:
            state["greens_gale_2"] += 1

        await send_sticker_to_channel(GREEN_STICKER_ID)
        await send_to_channel(format_placar())
        await send_to_channel(f"SEQUÊNCIA: {state['greens_seguidos']} greens 🔥")
        await clear_gale_messages()
        state.update({
            "waiting_for_result": False, "last_signal_color": None,
            "martingale_count": 0, "entrada_message_id": None,
        })
        await refresh_analise_message()
        return

    # Errou
    state["martingale_count"] += 1

    if state["martingale_count"] == 1:
        await send_gale_warning(1)
        return

    elif state["martingale_count"] == 2:
        await send_gale_warning(2)
        return

    # Após gale 2 errado → LOSS
    if state["martingale_count"] >= 3:
        state["greens_seguidos"] = 0
        state["total_losses"] += 1

        await send_sticker_to_channel(LOSS_STICKER_ID)
        await send_to_channel("🟥 <b>LOSS</b> 🟥")
        await send_to_channel(format_placar())
        await clear_gale_messages()
        state.update({
            "waiting_for_result": False, "last_signal_color": None,
            "martingale_count": 0, "entrada_message_id": None,
        })
        reset_placar_if_needed()
        await refresh_analise_message()


async def try_send_signal():
    now = datetime.now().timestamp()
    if state["waiting_for_result"]:
        return
    if now < state["signal_cooldown_until"]:
        return
    if now < state["next_signal_possible_after"]:
        return
    if len(state["history"]) < 12:
        return

    nome, cor = gerar_sinal_estrategia(state["history"])
    if not cor:
        return

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
        logger.info(f"Sinal enviado → {cor} ({nome}) - cooldown até {state['signal_cooldown_until']:.1f}")


async def api_worker():
    connector = aiohttp.TCPConnector(limit=5, keepalive_timeout=30)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            try:
                updated = await update_history_from_api(session)
                await resolve_after_result()
                await try_send_signal()
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
