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

load_dotenv()

# Configurações (mantidas)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7592335545:AAGjbyAZYG33LC42xvCDOaxBgrM-jXW5XXQ")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "-1002629616421")

API_URL = "https://api-cs.casino.org/svc-evolution-game-events/api/bacbo/latest"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

ANGOLA_TZ = pytz.timezone('Africa/Luanda')

OUTCOME_MAP = {
    "PlayerWon": "🔵", "BankerWon": "🔴", "Tie": "🟡",
    "🔵": "🔵", "🔴": "🔴", "🟡": "🟡",
}

API_POLL_INTERVAL = 3
SIGNAL_CYCLE_INTERVAL = 5
ANALISE_REFRESH_INTERVAL = 15
COOLDOWN_AFTER_LOSS = 4
COOLDOWN_AFTER_TIE = 3
MIN_HISTORY_FOR_SIGNAL = 10

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)-5s | %(message)s')
logger = logging.getLogger("BacBoBot")

bot = Bot(token=TELEGRAM_BOT_TOKEN)

state: Dict[str, Any] = {
    "history": [],  # 🔵 🔴 🟡
    "last_round_id": None,
    "waiting_for_result": False,
    "last_signal_color": None,
    "martingale_count": 0,
    "entrada_message_id": None,
    "martingale_message_ids": [],
    "greens_seguidos": 0,
    "total_greens": 0,
    "total_empates": 0,
    "total_losses": 0,
    "last_signal_pattern": None,
    "last_signal_sequence": None,
    "last_signal_round_id": None,
    "signal_cooldown": False,
    "cooldown_counter": 0,  # novo: cooldown após loss/tie
    "analise_message_id": None,
    "last_reset_date": None,
    "last_analise_refresh": 0.0,
    "last_result_round_id": None,
    "player_score_last": None,
    "banker_score_last": None,
}

# Funções auxiliares (mantidas, com pequenas melhorias)
async def send_to_channel(text: str, parse_mode="HTML") -> Optional[int]:
    try:
        msg = await bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=text, parse_mode=parse_mode, disable_web_page_preview=True)
        return msg.message_id
    except Exception as e:
        logger.error(f"Erro ao enviar: {e}")
        return None

async def send_error_to_channel(error_msg: str):
    timestamp = datetime.now(ANGOLA_TZ).strftime("%Y-%m-%d %H:%M:%S")
    text = f"⚠️ <b>ERRO</b> ⚠️\n<code>{timestamp}</code>\n\n{error_msg}"
    await send_to_channel(text)

async def delete_messages(message_ids: List[int]):
    for mid in message_ids[:]:
        try:
            await bot.delete_message(TELEGRAM_CHANNEL_ID, mid)
        except:
            pass

def should_reset_placar() -> bool:
    now = datetime.now(ANGOLA_TZ)
    if state["last_reset_date"] != now.date() or state["total_losses"] >= 8:
        state["last_reset_date"] = now.date()
        return True
    return False

def reset_placar_if_needed():
    if should_reset_placar():
        state.update({"total_greens": 0, "total_empates": 0, "total_losses": 0, "greens_seguidos": 0})
        logger.info("Placar resetado")

def calcular_acertividade() -> str:
    total = state["total_greens"] + state["total_losses"]
    return f"{(state['total_greens']/total*100):.1f}%" if total else "—"

def format_placar() -> str:
    return f"🏆 <b>DEERY PLACAR</b> 🏆\n✅ {state['total_greens']} | 🤝 {state['total_empates']} | ⛔ {state['total_losses']}\n🎯 {calcular_acertividade()}"

def format_analise_text() -> str:
    return "🎲 <b>ANALISANDO PADRÕES...</b> 🎲\n<i>Aguardando alta confiança</i>"

# ── ESTRATÉGIAS OTIMIZADAS ──
def oposto(cor: str) -> str:
    return "🔵" if cor == "🔴" else "🔴"

def get_current_streak(hist: List[str]) -> tuple:
    if not hist:
        return None, 0
    last = hist[-1]
    if last not in ("🔵", "🔴"):
        return None, 0
    streak = 1
    for i in range(2, len(hist)+1):
        if hist[-i] == last:
            streak += 1
        else:
            break
    return last, streak

def estrategia_repeticao(hist):
    if len(hist) >= 3 and hist[-1] == hist[-2] == hist[-3] != "🟡":
        return ("Rep 3x", hist[-1], 2)  # +2 votos se forte

def estrategia_alternancia(hist):
    if len(hist) >= 4:
        last4 = hist[-4:]
        if all(x in ("🔵", "🔴") for x in last4) and last4[0] == last4[2] != last4[1] == last4[3]:
            return ("Alt ABAB", oposto(last4[-1]), 1)

def estrategia_streak_continuation(hist):
    color, streak = get_current_streak(hist)
    if color and streak >= 3 and streak <= 5:  # continua streak curta-média
        return ("Streak Cont", color, 2 if streak >=4 else 1)

def estrategia_chop(hist):
    color, streak = get_current_streak(hist)
    if streak == 1 and len(hist) >=4:
        last4 = hist[-4:]
        if last4[0] == oposto(last4[1]) == last4[2] == oposto(last4[3]):
            return ("Chop Cont", oposto(last4[-1]), 1)

def estrategia_maj5(hist):
    window = [x for x in hist[-5:] if x in ("🔵", "🔴")]
    if len(window) >= 4:
        cnt = Counter(window)
        most, count = cnt.most_common(1)[0]
        if count >= 3:
            return ("Maj 5", most, 1)

def estrategia_paridade(p_score, b_score):
    if p_score is None or b_score is None:
        return None
    try:
        ps, bs = int(p_score), int(b_score)
        if ps % 2 == 1 and bs % 2 == 0:
            return ("Paridade", "🔵", 1)
        if bs % 2 == 1 and ps % 2 == 0:
            return ("Paridade", "🔴", 1)
    except:
        pass

def gerar_sinal_estrategia(history: List[str], p_score=None, b_score=None):
    if len(history) < MIN_HISTORY_FOR_SIGNAL:
        return None, None

    funcs = [
        estrategia_repeticao, estrategia_alternancia, estrategia_streak_continuation,
        estrategia_chop, estrategia_maj5
    ]

    votes = {"🔵": 0, "🔴": 0}
    applied_strats = []

    for func in funcs:
        res = func(history)
        if res:
            name, cor, weight = res
            votes[cor] += weight
            applied_strats.append(name)

    par = estrategia_paridade(p_score, b_score)
    if par:
        name, cor, weight = par
        votes[cor] += weight
        applied_strats.append(name)

    total_votes = sum(votes.values())
    if total_votes < 3:
        return None, None

    best_cor = max(votes, key=votes.get)
    best_count = votes[best_cor]

    # Preferência por Banker em empate
    if votes["🔵"] == votes["🔴"] and best_count > 0:
        best_cor = "🔴"
        best_count = votes[best_cor]

    # Check streak longa: não sinaliza se streak >5
    _, streak = get_current_streak(history)
    if streak > 5 and best_cor == history[-1]:
        return None, None

    logger.info(f"Sinal possível: {best_cor} | Votos: {votes} | Estrats: {applied_strats}")
    return f"Alta Confiança ({best_count} votos)", best_cor

# ── TEXTOS ──
def main_entry_text(color: str) -> str:
    nome = "AZUL" if color == "🔵" else "VERMELHO"
    return (
        f"🎲 <b>DEERY SINAL ALTA CONFIANÇA</b> 🎲\n"
        f"🧠 APOSTA: <b>{color} {nome}</b>\n"
        f"🛡️ Proteja TIE 🟡\n"
        f"<b>FAZER ATÉ 2 GALE</b>\n"
        f"🤑 ENTRADA FORTE 🤑"
    )

def green_text(greens: int) -> str:
    return f"✅ <b>ACERTAMOS ({greens} seguidos)</b> ✅\n🎲 Foco total!"

# ── LÓGICA PRINCIPAL ──
async def refresh_analise_message():
    now = datetime.now().timestamp()
    if now - state["last_analise_refresh"] < ANALISE_REFRESH_INTERVAL:
        return
    await delete_messages([state["analise_message_id"]] if state["analise_message_id"] else [])
    msg_id = await send_to_channel(format_analise_text())
    if msg_id:
        state["analise_message_id"] = msg_id
        state["last_analise_refresh"] = now

async def fetch_api(session):
    try:
        async with session.get(API_URL, headers=HEADERS, timeout=12) as r:
            if r.status == 200:
                return await r.json()
    except Exception as e:
        await send_error_to_channel(f"API erro: {e}")
    return None

async def update_history_from_api(session):
    reset_placar_if_needed()
    data = await fetch_api(session)
    if not data or "data" not in data:
        return

    data = data["data"]
    round_id = data.get("id")
    outcome_raw = (data.get("result") or {}).get("outcome")
    outcome = OUTCOME_MAP.get(outcome_raw, None)
    if not outcome:
        s = str(outcome_raw).lower()
        outcome = "🔵" if "player" in s else "🔴" if "banker" in s else "🟡" if any(w in s for w in ["tie","empate"]) else None

    if not outcome or state["last_round_id"] == round_id:
        return

    state["last_round_id"] = round_id
    state["history"].append(outcome)
    if len(state["history"]) > 300:
        state["history"].pop(0)

    # Scores
    result = data.get("result", {})
    p = result.get("player") or result.get("playerDice", {})
    b = result.get("banker") or result.get("bankerDice", {})
    for k in ("score", "sum", "total", "points"):
        if k in p: state["player_score_last"] = p[k]
        if k in b: state["banker_score_last"] = b[k]

    logger.info(f"Resultado: {outcome} | Round {round_id}")
    state["signal_cooldown"] = False

async def resolve_after_result():
    if not state["waiting_for_result"] or state["last_signal_round_id"] == state["last_round_id"]:
        return

    state["last_result_round_id"] = state["last_round_id"]
    last_outcome = state["history"][-1]
    target = state["last_signal_color"]

    placar = format_placar()

    if last_outcome == "🟡":
        state["total_empates"] += 1
        state["greens_seguidos"] = 0
        state["cooldown_counter"] = COOLDOWN_AFTER_TIE
    elif last_outcome == target:
        state["total_greens"] += 1
        state["greens_seguidos"] += 1
        await send_to_channel(green_text(state["greens_seguidos"]))
        await send_to_channel(placar)
        await clear_gale_messages()
        state.update({"waiting_for_result": False, "last_signal_color": None, "martingale_count": 0, "cooldown_counter": 0})
        return
    else:
        state["martingale_count"] += 1
        if state["martingale_count"] <= 2:
            await send_gale_warning(state["martingale_count"])
        if state["martingale_count"] >= 3:
            state["total_losses"] += 1
            state["greens_seguidos"] = 0
            await send_to_channel("🟥 <b>LOSS</b> 🟥")
            await send_to_channel(placar)
            await clear_gale_messages()
            state.update({"waiting_for_result": False, "last_signal_color": None, "martingale_count": 0})
            state["cooldown_counter"] = COOLDOWN_AFTER_LOSS
            return

    reset_placar_if_needed()
    await refresh_analise_message()

async def try_send_signal():
    if state["waiting_for_result"] or state["signal_cooldown"] or state["cooldown_counter"] > 0:
        if state["cooldown_counter"] > 0:
            state["cooldown_counter"] -= 1
        await refresh_analise_message()
        return

    if len(state["history"]) < MIN_HISTORY_FOR_SIGNAL:
        await refresh_analise_message()
        return

    seq_str = "".join(state["history"][-10:])
    if state["last_signal_sequence"] == seq_str:
        await refresh_analise_message()
        return

    padrao, cor = gerar_sinal_estrategia(
        state["history"], state.get("player_score_last"), state.get("banker_score_last")
    )
    if not cor:
        await refresh_analise_message()
        return

    await delete_messages([state["analise_message_id"]] if state["analise_message_id"] else [])
    state["martingale_message_ids"] = []

    msg_id = await send_to_channel(main_entry_text(cor))
    if msg_id:
        state["entrada_message_id"] = msg_id
        state["waiting_for_result"] = True
        state["last_signal_color"] = cor
        state["martingale_count"] = 0
        state["last_signal_pattern"] = padrao
        state["last_signal_sequence"] = seq_str
        state["last_signal_round_id"] = state["last_round_id"]
        logger.info(f"Sinal enviado: {cor} | {padrao}")

async def api_worker():
    async with aiohttp.ClientSession() as session:
        while True:
            await update_history_from_api(session)
            await resolve_after_result()
            await asyncio.sleep(API_POLL_INTERVAL)

async def scheduler_worker():
    await asyncio.sleep(5)
    while True:
        await refresh_analise_message()
        await try_send_signal()
        await asyncio.sleep(SIGNAL_CYCLE_INTERVAL)

async def main():
    logger.info("Bot otimizado iniciado...")
    await send_to_channel("🤖 Bot otimizado v2 - alta confiança apenas")
    await asyncio.gather(api_worker(), scheduler_worker())

if __name__ == "__main__":
    asyncio.run(main())
