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

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7525707247:AAHLVwSdes_UlaVQ5TUo72q-4mMZXE8_lfE")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "-1003564529662")
API_URL = "https://api.signals-house.com/validate/results?tableId=2&lastResult=13343863"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}
ANGOLA_TZ = pytz.timezone('Africa/Luanda')
OUTCOME_MAP = {
    "PlayerWon": "🔵", "BankerWon": "🔴", "Tie": "🟡",
    "Player": "🔵", "Banker": "🔴",
    "🔵": "🔵", "🔴": "🔴", "🟡": "🟡",
}

API_POLL_INTERVAL = 1.2          # Reduzido para detectar resultados mais rápido
SIGNAL_COOLDOWN_DURATION = 2.5   # Reduzido para permitir sinais mais próximos (ajuste se necessário)

GREEN_STICKER_ID = "CAACAgQAAxkBAAMCaanfUxV0k3upwRhvlpq9XyODGX4AAvAbAAL92lFROjONnjCocw86BA"

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)-5s | %(message)s')
logger = logging.getLogger("BacBoBot")
bot = Bot(token=TELEGRAM_BOT_TOKEN)

# =============================================
# CONFIGURAÇÕES DA ANÁLISE ESTATÍSTICA (ajuste aqui)
# =============================================
JANELA_PRINCIPAL = 36
JANELA_EMPATE = 20
JANELA_ENTROPIA = 12
MIN_DESVIO_PORCENTAGEM = 5.5
MIN_CONFANCA = 59.0
MAX_TAXA_EMPATE_RECENTE = 18.0

P_PLAYER = 44.37
P_BANKER = 44.37
P_TIE    = 11.26

state: Dict[str, Any] = {
    "history": [], "last_round_id": None, "waiting_for_result": False,
    "last_signal_color": None, "martingale_count": 0, "entrada_message_id": None,
    "martingale_message_ids": [], "greens_seguidos": 0, "total_greens": 0,
    "greens_sem_gale": 0, "greens_gale_1": 0, "greens_gale_2": 0,
    "total_empates": 0, "total_losses": 0,
    "signal_cooldown_until": 0.0, "analise_message_id": None,
    "last_reset_date": None, "last_analise_refresh": 0.0,
    "last_result_round_id": None, "player_score_last": None,
    "banker_score_last": None, "new_result_added": False,
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

async def send_error_to_channel(error_msg: str):
    timestamp = datetime.now(ANGOLA_TZ).strftime("%Y-%m-%d %H:%M:%S")
    text = f"⚠️ <b>ERRO DETECTADO</b> ⚠️\n<code>{timestamp}</code>\n\n{error_msg}"
    await send_to_channel(text)

async def delete_messages(message_ids: List[int]):
    if not message_ids:
        return
    for mid in message_ids[:]:
        try:
            await bot.delete_message(TELEGRAM_CHANNEL_ID, mid)
        except:
            pass

def should_reset_placar() -> bool:
    now = datetime.now(ANGOLA_TZ)
    if state["last_reset_date"] != now.date():
        state["last_reset_date"] = now.date()
        return True
    if state["total_losses"] >= 10:
        return True
    return False

def reset_placar_if_needed():
    if should_reset_placar():
        for k in ["total_greens", "greens_sem_gale", "greens_gale_1", "greens_gale_2",
                  "total_empates", "total_losses", "greens_seguidos"]:
            state[k] = 0
        logger.info("Placar resetado")

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
    return "🎲 <b>ANALISANDO...</b> 🎲\n<i>Aguarde sinal</i>"

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
        async with session.get(API_URL, headers=HEADERS, timeout=7) as resp:
            if resp.status == 200:
                return await resp.json()
            return None
    except:
        return None

async def update_history_from_api(session):
    reset_placar_if_needed()
    data = await fetch_api(session)
    if not data:
        return
    try:
        items = data.get("data", [])
        if isinstance(items, list) and len(items) > 0:
            latest = items[0]
            round_id = latest.get("id")
            if not round_id:
                return
            outcome_raw = latest.get("result")
            if not outcome_raw:
                return
            score = latest.get("score")
            outcome = OUTCOME_MAP.get(outcome_raw)
            if not outcome:
                s = str(outcome_raw or "").lower()
                if "player" in s: outcome = "🔵"
                elif "banker" in s: outcome = "🔴"
                elif any(x in s for x in ["tie", "empate", "draw"]): outcome = "🟡"
            if outcome and state["last_round_id"] != round_id:
                state["last_round_id"] = round_id
                state["history"].append(outcome)
                state["player_score_last"] = None
                state["banker_score_last"] = None
                if len(state["history"]) > 200:
                    state["history"].pop(0)
                logger.info(f"Resultado novo: {outcome} (round {round_id}, score={score})")
                state["new_result_added"] = True
                state["signal_cooldown_until"] = datetime.now().timestamp() + 0.5
        # ... (mantido o bloco elif para compatibilidade com outros formatos JSON)
    except Exception as e:
        logger.debug(f"Erro processando API: {e}")

# ────────────────────────────────────────────────
# NOVA LÓGICA ESTATÍSTICA (sem padrões sequenciais)
# ────────────────────────────────────────────────

def calcular_entropia_binaria(p: float) -> float:
    if p <= 0 or p >= 1:
        return 0.0
    return - (p * math.log2(p) + (1-p) * math.log2(1-p))

def proporcao_na_janela(hist: List[str], janela: int) -> tuple[float, float, float]:
    if len(hist) < 3:
        return 0.0, 0.0, 0.0
    janela_real = min(janela, len(hist))
    recorte = hist[-janela_real:]
    c = Counter(recorte)
    n = len(recorte)
    p_p = c["🔵"] / n * 100 if n > 0 else 0
    p_b = c["🔴"] / n * 100 if n > 0 else 0
    p_t = c["🟡"] / n * 100 if n > 0 else 0
    return p_p, p_b, p_t

def desvio_da_esperada(p_obs: float, p_esperada: float) -> float:
    return abs(p_obs - p_esperada)

def gerar_sinal_inteligente(
    history: List[str],
    player_score_last: Optional[int] = None,
    banker_score_last: Optional[int] = None
) -> tuple[Optional[str], Optional[str], float]:
    if len(history) < 12:
        return None, None, 0.0

    pp, pb, pt = proporcao_na_janela(history, JANELA_PRINCIPAL)
    pp_short, pb_short, pt_short = proporcao_na_janela(history, JANELA_EMPATE)

    if pt_short > MAX_TAXA_EMPATE_RECENTE:
        return "Muitos empates recentes", None, 0.0

    desv_p = desvio_da_esperada(pp, P_PLAYER)
    desv_b = desvio_da_esperada(pb, P_BANKER)

    ent = 1.0
    if len(history) >= JANELA_ENTROPIA:
        recorte = history[-JANELA_ENTROPIA:]
        c = Counter(x for x in recorte if x in ("🔵", "🔴"))
        n_bin = sum(c.values())
        if n_bin >= 6:
            p_bin = c["🔵"] / n_bin
            ent = calcular_entropia_binaria(p_bin)

    score = 0.0
    cor_favor = None

    if desv_p > MIN_DESVIO_PORCENTAGEM and pp > pb + 2:
        score += (desv_p - MIN_DESVIO_PORCENTAGEM) * 1.8
        cor_favor = "🔵"
    elif desv_b > MIN_DESVIO_PORCENTAGEM and pb > pp + 2:
        score += (desv_b - MIN_DESVIO_PORCENTAGEM) * 1.8
        cor_favor = "🔴"

    if ent < 0.78:
        score += (0.92 - ent) * 2.2

    if abs(pp_short - pb_short) < 3.5:
        score *= 0.55

    if player_score_last is not None and banker_score_last is not None:
        if player_score_last > banker_score_last + 3:
            score += 0.8
            cor_favor = cor_favor or "🔵"
        elif banker_score_last > player_score_last + 3:
            score += 0.8
            cor_favor = cor_favor or "🔴"

    if score < 1.8 or cor_favor is None:
        return "Sem força estatística suficiente", None, 0.0

    confianca = min(78.0, 52.0 + score * 4.2)
    if confianca < MIN_CONFANCA:
        return "Confiança abaixo do mínimo", None, confianca

    nome = "Desequilíbrio estatístico"
    if ent < 0.75:
        nome += " + baixa entropia"

    return nome, cor_favor, round(confianca, 1)

def gerar_sinal_estrategia(history: List[str], player_score=None, banker_score=None):
    nome, cor, confianca = gerar_sinal_inteligente(history, player_score, banker_score)
    if cor is None:
        return None, None
    return f"{nome} ({confianca}%)", cor

def main_entry_text(nome: str, color: str) -> str:
    return (
        f"🎲 ENTRADA DO CLEVER 🎲\n"
        f"APOSTA NA COR: {color}\n"
        f"PROTEJA O TIE 🟡\n"
        f"<i>{nome}</i>"
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
    if state["last_result_round_id"] == state["last_round_id"]:
        return
    if not state["history"]:
        return

    last_outcome = state["history"][-1]
    state["last_result_round_id"] = state["last_round_id"]
    target = state["last_signal_color"]
    acertou = last_outcome == target
    is_tie = last_outcome == "🟡"

    if acertou or is_tie:
        state["total_greens"] += 1
        state["greens_seguidos"] += 1
        if state["martingale_count"] == 0: state["greens_sem_gale"] += 1
        elif state["martingale_count"] == 1: state["greens_gale_1"] += 1
        elif state["martingale_count"] == 2: state["greens_gale_2"] += 1
        await send_sticker_to_channel(GREEN_STICKER_ID)
        await send_to_channel(format_placar())
        await send_to_channel(f"SEQUÊNCIA: {state['greens_seguidos']} greens 🔥")
        await clear_gale_messages()
        state.update({
            "waiting_for_result": False, "last_signal_color": None,
            "martingale_count": 0, "entrada_message_id": None,
            "signal_cooldown_until": datetime.now().timestamp() + SIGNAL_COOLDOWN_DURATION
        })
        await refresh_analise_message()
        return

    state["martingale_count"] += 1

    if state["martingale_count"] == 1:
        await send_gale_warning(1)
        return
    elif state["martingale_count"] == 2:
        await send_gale_warning(2)
        return

    if state["martingale_count"] >= 3:
        state["greens_seguidos"] = 0
        state["total_losses"] += 1
        await send_to_channel("🟥 <b>LOSS</b> 🟥")
        await send_to_channel(format_placar())
        await clear_gale_messages()
        state.update({
            "waiting_for_result": False, "last_signal_color": None,
            "martingale_count": 0, "entrada_message_id": None,
            "signal_cooldown_until": datetime.now().timestamp() + SIGNAL_COOLDOWN_DURATION
        })
        reset_placar_if_needed()
        await refresh_analise_message()

async def try_send_signal():
    now = datetime.now().timestamp()
    if state["waiting_for_result"]:
        await delete_analise_message()
        return
    if now < state["signal_cooldown_until"]:
        return
    if len(state["history"]) < 12:
        return
    if not state["new_result_added"]:
        return
    state["new_result_added"] = False

    nome, cor = gerar_sinal_estrategia(
        state["history"],
        state.get("player_score_last"),
        state.get("banker_score_last")
    )
    if not cor:
        await refresh_analise_message()
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
        logger.info(f"Sinal enviado → {cor} ({nome})")

async def api_worker():
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await update_history_from_api(session)
                await asyncio.sleep(0.3)
                await resolve_after_result()
                await try_send_signal()
            except Exception as e:
                logger.debug(f"Erro loop principal: {e}")
            await asyncio.sleep(API_POLL_INTERVAL)

async def main():
    logger.info("Bot iniciado...")
    await send_to_channel("🤖 Bot online – Gale 2 ativo + análise estatística")
    await api_worker()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot parado pelo usuário")
    except Exception as e:
        logger.critical("Erro fatal", exc_info=True)
