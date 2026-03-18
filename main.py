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
SIGNAL_DELAY_MIN = 2       # Delay mínimo antes de enviar sinal (mais rápido agora)
SIGNAL_DELAY_MAX = 5        # Delay máximo antes de enviar sinal (mais rápido agora)

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
    "pending_signal_analysis": False,
    "signal_send_after": 0.0,
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
                state["history"] = state["history"][-500:]
            logger.info(f"Novo resultado adicionado: {outcome} (id {round_id})")
            return True

        return False
    except Exception as e:
        logger.debug(f"Erro processando API: {e}")
        return False


# ════════════════════════════════════════════════════════════════
# NOVAS ESTRATÉGIAS DE PADRÕES — rápidas e assertivas
# ════════════════════════════════════════════════════════════════

def filtrar_empates(hist: List[str]) -> List[str]:
    """Remove empates para análise de padrão binário."""
    return [x for x in hist if x in ("🔴", "🔵")]


def detectar_padrao(history: List[str]) -> tuple[Optional[str], Optional[str]]:
    """
    Analisa os últimos 3-6 resultados (sem empates) e retorna (nome_padrão, cor_sinal).
    Retorna (None, None) se nenhum padrão forte for encontrado.
    """
    sem_empate = filtrar_empates(history)

    if len(sem_empate) < 2:
        return None, None

    ultimos6 = sem_empate[-6:]
    ultimos5 = sem_empate[-5:] if len(sem_empate) >= 5 else None
    ultimos4 = sem_empate[-4:] if len(sem_empate) >= 4 else None
    ultimos3 = sem_empate[-3:] if len(sem_empate) >= 3 else None
    ultimos2 = sem_empate[-2:]

    # ──────────────────────────────────────────────
    # PADRÃO 1: Sequência longa (4-6 iguais) → Quebra forte
    # ──────────────────────────────────────────────
    if len(ultimos6) >= 5 and all(x == "🔴" for x in ultimos6[-5:]):
        return "Sequência longa 🔴×5 → quebra", "🔵"
    if len(ultimos6) >= 5 and all(x == "🔵" for x in ultimos6[-5:]):
        return "Sequência longa 🔵×5 → quebra", "🔴"

    if len(ultimos6) >= 4 and all(x == "🔴" for x in ultimos6[-4:]):
        return "Sequência forte 🔴×4 → quebra", "🔵"
    if len(ultimos6) >= 4 and all(x == "🔵" for x in ultimos6[-4:]):
        return "Sequência forte 🔵×4 → quebra", "🔴"

    # ──────────────────────────────────────────────
    # PADRÃO 2: Tripla (3 iguais) → Quebra
    # ──────────────────────────────────────────────
    if ultimos3 and ultimos3 == ["🔴", "🔴", "🔴"]:
        return "Tripla 🔴🔴🔴 → quebra", "🔵"
    if ultimos3 and ultimos3 == ["🔵", "🔵", "🔵"]:
        return "Tripla 🔵🔵🔵 → quebra", "🔴"

    # ──────────────────────────────────────────────
    # PADRÃO 3: Dupla (2 iguais) → Quebra
    # ──────────────────────────────────────────────
    if ultimos2 == ["🔴", "🔴"]:
        return "Dupla 🔴🔴 → quebra", "🔵"
    if ultimos2 == ["🔵", "🔵"]:
        return "Dupla 🔵🔵 → quebra", "🔴"

    # ──────────────────────────────────────────────
    # PADRÃO 4: Alternância perfeita (zigzag) → continuar padrão
    # ──────────────────────────────────────────────
    if ultimos4 and ultimos4 == ["🔴", "🔵", "🔴", "🔵"]:
        return "Alternância 🔴🔵🔴🔵 → continua", "🔴"
    if ultimos4 and ultimos4 == ["🔵", "🔴", "🔵", "🔴"]:
        return "Alternância 🔵🔴🔵🔴 → continua", "🔵"

    # ──────────────────────────────────────────────
    # PADRÃO 5: Pressão (2 de 1 cor após 1 da outra) → cor dominante
    # ──────────────────────────────────────────────
    if ultimos3 and ultimos3 == ["🔴", "🔵", "🔵"]:
        return "Pressão 🔵🔵 após 🔴 → continua", "🔴"
    if ultimos3 and ultimos3 == ["🔵", "🔴", "🔴"]:
        return "Pressão 🔴🔴 após 🔵 → continua", "🔵"

    # ──────────────────────────────────────────────
    # PADRÃO 6: Retorno após quebra → volta à cor dominante
    # ──────────────────────────────────────────────
    if ultimos4 and ultimos4 == ["🔴", "🔴", "🔵", "🔴"]:
        return "Retorno 🔴 após quebra", "🔴"
    if ultimos4 and ultimos4 == ["🔵", "🔵", "🔴", "🔵"]:
        return "Retorno 🔵 após quebra", "🔵"

    # ──────────────────────────────────────────────
    # PADRÃO 7: Sanduíche (ABA) → repetir A
    # ──────────────────────────────────────────────
    if ultimos3 and ultimos3[0] == ultimos3[2] and ultimos3[0] != ultimos3[1]:
        cor = ultimos3[0]
        return f"Sanduíche {cor} → repete", cor

    # ──────────────────────────────────────────────
    # PADRÃO 8: Dominância recente (4 de 5 da mesma cor)
    # ──────────────────────────────────────────────
    if ultimos5:
        count_red = ultimos5.count("🔴")
        count_blue = ultimos5.count("🔵")
        if count_red >= 4:
            return "Dominância 🔴 (4/5) → quebra", "🔵"
        if count_blue >= 4:
            return "Dominância 🔵 (4/5) → quebra", "🔴"

    # ──────────────────────────────────────────────
    # PADRÃO 9: Recuperação (após perder domínio, cor volta)
    # Ex: 🔴🔴🔵🔵🔴 → 🔴 recupera
    # ──────────────────────────────────────────────
    if ultimos5 and ultimos5 == ["🔴", "🔴", "🔵", "🔵", "🔴"]:
        return "Recuperação 🔴 após pressão 🔵", "🔴"
    if ultimos5 and ultimos5 == ["🔵", "🔵", "🔴", "🔴", "🔵"]:
        return "Recuperação 🔵 após pressão 🔴", "🔵"

    # ──────────────────────────────────────────────
    # PADRÃO 10: Espelho duplo (AABB → B continua)
    # ──────────────────────────────────────────────
    if ultimos4 and ultimos4[:2] == ultimos4[2:]:
        # AABB onde AA==BB → improvável, skip
        pass
    elif ultimos4 and ultimos4[0] == ultimos4[1] and ultimos4[2] == ultimos4[3] and ultimos4[0] != ultimos4[2]:
        cor = ultimos4[2]
        oposta = "🔴" if cor == "🔵" else "🔵"
        return f"Espelho AABB → quebra {cor}", oposta

    return None, None


def verificar_excesso_empates(history: List[str], janela: int = 10) -> bool:
    """Verifica se há muitos empates recentes (>20%), o que invalida sinais."""
    if len(history) < janela:
        recorte = history
    else:
        recorte = history[-janela:]
    if len(recorte) == 0:
        return False
    empates = recorte.count("🟡")
    return (empates / len(recorte) * 100) > 20.0


def gerar_sinal_estrategia(history: List[str]):
    """Gera sinal baseado em padrões claros dos últimos resultados."""
    if len(history) < 3:
        return None, None

    # Bloquear se muitos empates recentes
    if verificar_excesso_empates(history):
        logger.info("Muitos empates recentes — sinal bloqueado")
        return None, None

    nome, cor = detectar_padrao(history)
    if cor is None:
        return None, None

    logger.info(f"Padrão detectado: {nome} → {cor}")
    return nome, cor


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
    """Valida o sinal anterior quando chega o resultado da rodada onde o sinal foi aplicado."""
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
        await refresh_analise_message()


async def try_send_signal():
    """Envia sinal somente após o delay inteligente pós-resultado."""
    now = datetime.now().timestamp()

    if state["waiting_for_result"]:
        return

    if now < state["signal_cooldown_until"]:
        return

    if not state["pending_signal_analysis"]:
        return

    if now < state["signal_send_after"]:
        return

    state["pending_signal_analysis"] = False

    if len(state["history"]) < 3:
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
        logger.info(f"Sinal enviado → {cor} ({nome})")


async def api_worker():
    connector = aiohttp.TCPConnector(limit=5, keepalive_timeout=30)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            try:
                updated = await update_history_from_api(session)
                if updated:
                    await resolve_after_result()

                    if not state["waiting_for_result"]:
                        delay = random.uniform(SIGNAL_DELAY_MIN, SIGNAL_DELAY_MAX)
                        state["pending_signal_analysis"] = True
                        state["signal_send_after"] = datetime.now().timestamp() + delay
                        logger.info(f"Novo resultado detectado. Sinal agendado em {delay:.1f}s")

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
