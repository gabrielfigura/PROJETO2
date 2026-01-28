import os
import asyncio
import logging
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime
import pytz
from collections import Counter

import aiohttp
import numpy as np
from scipy import stats
from telegram import Bot
from telegram.error import TelegramError
from dotenv import load_dotenv

load_dotenv()

# Configurações
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7592335545:AAGjbyAZYG33LC42xvCDOaxBgrM-jXW5XXQ")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "-1002629616421")  # MUDE SE NECESSÁRIO

API_URL = "https://api-cs.casino.org/svc-evolution-game-events/api/bacbo/latest"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

ANGOLA_TZ = pytz.timezone('Africa/Luanda')

OUTCOME_MAP = {
    "PlayerWon": "🔵",
    "BankerWon": "🔴",
    "Tie": "🟡",
    "🔵": "🔵",
    "🔴": "🔴",
    "🟡": "🟡",
}

API_POLL_INTERVAL = 3
SIGNAL_CYCLE_INTERVAL = 5
ANALISE_REFRESH_INTERVAL = 15

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-5s | %(message)s'
)
logger = logging.getLogger("BacBoBot")

bot = Bot(token=TELEGRAM_BOT_TOKEN)

state: Dict[str, Any] = {
    "history": [],
    "last_round_id": None,
    "waiting_for_result": False,
    "last_signal_color": None,
    "martingale_count": 0,
    "entrada_message_id": None,
    "martingale_message_ids": [],
    "greens_seguidos": 0,
    "total_greens": 0,
    "greens_sem_gale": 0,
    "greens_gale_1": 0,
    "greens_gale_2": 0,
    "total_empates": 0,
    "total_losses": 0,
    "last_signal_pattern": None,
    "last_signal_sequence": None,
    "last_signal_round_id": None,
    "signal_cooldown": False,
    "analise_message_id": None,
    "last_reset_date": None,
    "last_analise_refresh": 0.0,
    "last_result_round_id": None,
    "player_score_last": None,
    "banker_score_last": None,
    
    # ──────────────── NOVO ────────────────
    "market_regime": "NEUTRAL",
    "volatility_index": 0.0,
    "trend_strength": 0.0,
    "adaptive_threshold": 0.62,
    "pattern_success_rate": {},           # ex: "Rep 4x_🔵": 0.72
    "last_signals": [],                   # lista de dicts para feedback adaptativo
}

async def send_to_channel(text: str, parse_mode="HTML") -> Optional[int]:
    try:
        msg = await bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=text,
            parse_mode=parse_mode,
            disable_web_page_preview=True
        )
        return msg.message_id
    except TelegramError as te:
        logger.error(f"Telegram Error: {te}")
        return None
    except Exception as e:
        logger.exception("Erro ao enviar mensagem")
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
    current_date = now.date()
    if state["last_reset_date"] is None or state["last_reset_date"] != current_date:
        state["last_reset_date"] = current_date
        return True
    if state["total_losses"] >= 10:
        return True
    return False

def reset_placar_if_needed():
    if should_reset_placar():
        state["total_greens"] = 0
        state["greens_sem_gale"] = 0
        state["greens_gale_1"] = 0
        state["greens_gale_2"] = 0
        state["total_empates"] = 0
        state["total_losses"] = 0
        state["greens_seguidos"] = 0
        logger.info("🔄 Placar resetado (diário ou por 10 losses)")

def calcular_acertividade() -> str:
    total_decisoes = state["total_greens"] + state["total_losses"]
    if total_decisoes == 0:
        return "—"
    perc = (state["total_greens"] / total_decisoes) * 100
    return f"{perc:.1f}%"

def format_placar() -> str:
    acert = calcular_acertividade()
    return (
        "🏆 <b>RESUMO</b> 🏆\n"
        f"✅ Ganhos sem gale:    <b>{state['greens_sem_gale']}</b>\n"
        f"🔄 Ganhos gale 1:       <b>{state['greens_gale_1']}</b>\n"
        f"🔄 Ganhos gale 2:       <b>{state['greens_gale_2']}</b>\n"
        f"🤝 Total empates:       <b>{state['total_empates']}</b>\n"
        f"⛔ Losses reais:        <b>{state['total_losses']}</b>\n"
        f"─────────────────────────────\n"
        f"🎯 Total greens:        <b>{state['total_greens']}</b>\n"
        f"🎯 Acertividade:        <b>{acert}</b>"
    )

def format_analise_text() -> str:
    regime = state.get("market_regime", "—")
    vol = state.get("volatility_index", 0) * 100
    thresh = state.get("adaptive_threshold", 0.62) * 100
    return (
        f"🎲 <b>ANALISANDO...</b> 🎲\n\n"
        f"Regime: <b>{regime}</b>\n"
        f"Volatilidade: <b>{vol:.0f}%</b>\n"
        f"Limiar atual: <b>{thresh:.0f}%</b>\n\n"
        "<i>Aguarde o próximo sinal de alta confiança</i>"
    )

async def refresh_analise_message():
    now = datetime.now().timestamp()
    if (now - state["last_analise_refresh"]) < ANALISE_REFRESH_INTERVAL:
        return

    await delete_analise_message()
    msg_id = await send_to_channel(format_analise_text())
    if msg_id:
        state["analise_message_id"] = msg_id
        state["last_analise_refresh"] = now

async def delete_analise_message():
    if state["analise_message_id"] is not None:
        await delete_messages([state["analise_message_id"]])
        state["analise_message_id"] = None

async def fetch_api(session: aiohttp.ClientSession) -> Optional[Dict]:
    try:
        async with session.get(API_URL, headers=HEADERS, timeout=12) as resp:
            if resp.status != 200:
                await send_error_to_channel(f"API retornou status {resp.status}")
                return None
            return await resp.json()
    except Exception as e:
        await send_error_to_channel(f"Erro na API: {str(e)}")
        return None

async def update_history_from_api(session):
    reset_placar_if_needed()
    data = await fetch_api(session)
    if not data:
        return
    try:
        if "data" in data:
            data = data["data"]
        round_id = data.get("id")
        outcome_raw = (data.get("result") or {}).get("outcome")
        player_dice = None
        banker_dice = None

        result = data.get("result") or {}
        if isinstance(result, dict):
            pl = result.get("player") or result.get("playerDice") or {}
            bk = result.get("banker") or result.get("bankerDice") or {}
            for k in ("score", "sum", "total", "points"):
                if k in pl: player_dice = pl[k]
                if k in bk: banker_dice = bk[k]

        if not round_id or not outcome_raw:
            return

        outcome = OUTCOME_MAP.get(outcome_raw)
        if not outcome:
            s = str(outcome_raw).lower()
            if "player" in s: outcome = "🔵"
            elif "banker" in s: outcome = "🔴"
            elif any(x in s for x in ["tie", "empate", "draw"]): outcome = "🟡"

        if outcome and state["last_round_id"] != round_id:
            state["last_round_id"] = round_id
            state["history"].append(outcome)
            if player_dice is not None and banker_dice is not None:
                state["player_score_last"] = player_dice
                state["banker_score_last"] = banker_dice
            if len(state["history"]) > 200:
                state["history"].pop(0)
            logger.info(f"Novo resultado → {outcome} | round {round_id}")
            state["signal_cooldown"] = False
    except Exception as e:
        await send_error_to_channel(f"Erro processando API: {str(e)}")

def oposto(cor: str) -> str:
    return "🔵" if cor == "🔴" else "🔴"

# ───────────────────────────────────────────────
#     NOVA LÓGICA AVANÇADA DE SINAIS (2025)
# ───────────────────────────────────────────────

def limpar_history_para_analise(history: List[str]) -> List[str]:
    return [x for x in history if x in ("🔵", "🔴")]

def calcular_volatilidade(history: List[str]) -> float:
    if len(history) < 4:
        return 0.0
    clean = limpar_history_para_analise(history[-20:])
    if len(clean) < 3:
        return 0.0
    changes = sum(1 for a, b in zip(clean, clean[1:]) if a != b)
    return changes / (len(clean) - 1) if len(clean) > 1 else 0.0

def ajustar_limiar_adaptativo(confianca_atual: float):
    if not state["last_signals"]:
        return
    recentes = state["last_signals"][-10:]
    if not recentes:
        return
    taxa_real = sum(1 for x in recentes if x["acertou"]) / len(recentes)
    if taxa_real > 0.70 and state["adaptive_threshold"] > 0.58:
        state["adaptive_threshold"] = max(0.58, state["adaptive_threshold"] - 0.012)
    elif taxa_real < 0.48 and state["adaptive_threshold"] < 0.80:
        state["adaptive_threshold"] += 0.015
    state["adaptive_threshold"] = round(np.clip(state["adaptive_threshold"], 0.56, 0.84), 3)

def detectar_regime_mercado(history: List[str]) -> Dict[str, Any]:
    clean = limpar_history_para_analise(history[-14:])
    if len(clean) < 8:
        return {"tipo": "NEUTRAL", "forca": 0.0}
    cnt = Counter(clean)
    total = cnt["🔵"] + cnt["🔴"]
    if total == 0:
        return {"tipo": "NEUTRAL", "forca": 0.0}
    blue_ratio = cnt["🔵"] / total
    red_ratio = cnt["🔴"] / total
    max_ratio = max(blue_ratio, red_ratio)
    if max_ratio > 0.72:
        regime = "BULL" if blue_ratio > red_ratio else "BEAR"
    elif max_ratio > 0.58:
        regime = "TENDÊNCIA FRACA"
    else:
        regime = "NEUTRAL / CHOPPY"
    forca = (max_ratio - 0.5) * 2
    return {"tipo": regime, "forca": round(forca, 3)}

def analisar_tendencia_principal(history: List[str]) -> Dict[str, Any]:
    clean = limpar_history_para_analise(history)
    if len(clean) < 7:
        return {"direcao": None, "forca": 0.0}
    ratios = []
    for w in [5, 8, 12, 16]:
        if len(clean) >= w:
            window = clean[-w:]
            blue_r = window.count("🔵") / len(window)
            ratios.append(blue_r)
    if len(ratios) < 3:
        return {"direcao": None, "forca": 0.0}
    x = np.arange(len(ratios))
    y = np.array(ratios)
    try:
        slope, _, r_value, _, _ = stats.linregress(x, y)
        if abs(slope) < 0.008:
            direcao = None
        else:
            direcao = "🔵" if slope > 0 else "🔴"
        forca = abs(r_value) ** 1.4
        forca = min(0.99, max(0.0, forca))
    except:
        direcao, forca = None, 0.0
    return {"direcao": direcao, "forca": round(forca, 3)}

def detectar_padroes_complexos(history: List[str]) -> List[Dict[str, Any]]:
    padroes = []
    n = len(history)
    # Repetições
    for length in [3,4,5,6]:
        if n >= length and all(x == history[-1] for x in history[-length:]):
            if history[-1] == "🟡": continue
            padroes.append({
                "tipo": f"Rep {length}x",
                "cor": history[-1],
                "conf": min(0.45 + length*0.09, 0.82),
                "complexidade": length
            })
    # Alternância
    if n >= 6:
        last6 = history[-6:]
        if all(x in ("🔵","🔴") for x in last6) and \
           last6[0] == last6[2] == last6[4] and \
           last6[1] == last6[3] == last6[5] and \
           last6[0] != last6[1]:
            prox = oposto(last6[-1])
            padroes.append({
                "tipo": "Alt ABABAB",
                "cor": prox,
                "conf": 0.68,
                "complexidade": 3
            })
    # Pós-empate
    if n >= 2 and history[-2] == "🟡" and history[-1] in ("🔵","🔴"):
        key = f"pos-tie-{history[-1]}"
        sucesso = state["pattern_success_rate"].get(key, 0.52)
        padroes.append({
            "tipo": "Pós-Tie",
            "cor": history[-1],
            "conf": 0.38 + sucesso*0.34,
            "complexidade": 2
        })
    # Maioria forte
    if n >= 10:
        recent10 = limpar_history_para_analise(history[-10:])
        if len(recent10) >= 7:
            cnt = Counter(recent10)
            most, qtd = cnt.most_common(1)[0]
            ratio = qtd / len(recent10)
            if ratio >= 0.70:
                padroes.append({
                    "tipo": "Maioria forte 10",
                    "cor": most,
                    "conf": 0.55 + (ratio-0.7)*1.2,
                    "complexidade": 2
                })
    return padroes

def gerar_sinal_avancado(history: List[str], player_score=None, banker_score=None) -> Tuple[Optional[str], Optional[str], float]:
    if len(history) < 8:
        return None, None, 0.0

    regime = detectar_regime_mercado(history)
    state["market_regime"] = regime["tipo"]
    
    vol = calcular_volatilidade(history)
    state["volatility_index"] = round(vol, 3)
    
    tendencia = analisar_tendencia_principal(history)
    state["trend_strength"] = tendencia["forca"]
    
    padroes = detectar_padroes_complexos(history)
    if not padroes:
        return None, None, 0.0
    
    padroes_ordenados = sorted(
        padroes,
        key=lambda p: p["conf"] * (1 + p["complexidade"]*0.18),
        reverse=True
    )
    
    melhor = padroes_ordenados[0]
    confianca = melhor["conf"]
    
    if regime["forca"] > 0.4:
        if (regime["tipo"] == "BULL" and melhor["cor"] == "🔵") or \
           (regime["tipo"] == "BEAR" and melhor["cor"] == "🔴"):
            confianca *= 1.12
        else:
            confianca *= 0.88
    
    if vol > 0.75:
        confianca *= 0.84
    
    confianca = min(0.92, max(0.40, confianca))
    
    ajustar_limiar_adaptativo(confianca)
    
    if confianca >= state["adaptive_threshold"]:
        return melhor["tipo"], melhor["cor"], round(confianca, 3)
    
    return None, None, 0.0

def main_entry_text(color: str, estrategia: str, confianca: float) -> str:
    cor_nome = "AZUL" if color == "🔵" else "VERMELHO"
    emoji = color
    conf_str = f" ({confianca:.1%})" if confianca > 0 else ""
    return (
        f"🎲 <b>CLEVER_M</b> 🎲\n"
        f"🧠 APOSTA EM: <b>{emoji} {cor_nome}{conf_str}</b>\n"
        f"📊 <i>{estrategia}</i>\n"
        f"🛡️ Proteja o TIE <b>🟡</b>\n"
        f"<b>FAZER ATÉ 2 GALE</b>\n"
        f"🤑 <b>VAI ENTRAR DINHEIRO</b> 🤑"
    )

def green_text(greens: int) -> str:
    return (
        f"🤡 <b>ENTROU DINHEIRO</b> 🤡\n"
        f"🎲 <b>MAIS FOCO E MENOS GANÂNCIA</b> 🎲\n"
        f"🔥 <b>CLEVER É O LÍDER</b> 🔥"
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
    if not state.get("waiting_for_result", False) or not state.get("last_signal_color"):
        return

    if state["last_result_round_id"] == state["last_round_id"]:
        return

    if not state["history"]:
        return

    last_outcome = state["history"][-1]

    if state["last_signal_round_id"] == state["last_round_id"]:
        return

    state["last_result_round_id"] = state["last_round_id"]
    target = state["last_signal_color"]

    acertou_cor = (last_outcome == target)
    is_tie = (last_outcome == "🟡")
    acertou = acertou_cor or is_tie

    # Registrar para feedback adaptativo
    state["last_signals"].append({"acertou": acertou, "cor": target})
    if len(state["last_signals"]) > 40:
        state["last_signals"].pop(0)

    if acertou:
        state["total_greens"] += 1
        state["greens_seguidos"] += 1

        if is_tie:
            state["total_empates"] += 1
        else:
            if state["martingale_count"] == 0:
                state["greens_sem_gale"] += 1
            elif state["martingale_count"] == 1:
                state["greens_gale_1"] += 1
            elif state["martingale_count"] == 2:
                state["greens_gale_2"] += 1

        await send_to_channel(green_text(state["greens_seguidos"]))
        await send_to_channel(format_placar())

        seq_text = f"ESTAMOS NUMA SEQUÊNCIA DE {state['greens_seguidos']} GANHOS SEGUIDOS 🔥"
        await send_to_channel(seq_text)

        await clear_gale_messages()

        state.update({
            "waiting_for_result": False,
            "last_signal_color": None,
            "martingale_count": 0,
            "entrada_message_id": None,
            "last_signal_pattern": None,
            "last_signal_sequence": None,
            "last_signal_round_id": None,
            "signal_cooldown": True
        })
        return

    # Perda real
    state["martingale_count"] += 1

    if state["martingale_count"] == 1:
        await send_gale_warning(1)
    elif state["martingale_count"] == 2:
        await send_gale_warning(2)

    if state["martingale_count"] >= 3:
        state["greens_seguidos"] = 0
        state["total_losses"] += 1

        await send_to_channel("🟥 <b>LOSS 🟥</b>")
        await send_to_channel(format_placar())

        await clear_gale_messages()

        state.update({
            "waiting_for_result": False,
            "last_signal_color": None,
            "martingale_count": 0,
            "entrada_message_id": None,
            "last_signal_pattern": None,
            "last_signal_sequence": None,
            "last_signal_round_id": None,
            "signal_cooldown": True
        })

    reset_placar_if_needed()
    await refresh_analise_message()

async def try_send_signal():
    if state["waiting_for_result"]:
        await delete_analise_message()
        return

    if state["signal_cooldown"]:
        await refresh_analise_message()
        return

    if len(state["history"]) < 8:  # exigência mínima aumentada
        await refresh_analise_message()
        return

    estrategia, cor, confianca = gerar_sinal_avancado(
        state["history"],
        state.get("player_score_last"),
        state.get("banker_score_last")
    )

    if not cor or confianca < state["adaptive_threshold"]:
        await refresh_analise_message()
        return

    # Evitar repetir o mesmo sinal consecutivo
    seq_str = "".join(state["history"][-8:])
    if state["last_signal_pattern"] == estrategia and state["last_signal_sequence"] == seq_str:
        await refresh_analise_message()
        return

    await delete_analise_message()
    state["martingale_message_ids"] = []

    msg_id = await send_to_channel(main_entry_text(cor, estrategia, confianca))
    if msg_id:
        state["entrada_message_id"] = msg_id
        state["waiting_for_result"] = True
        state["last_signal_color"] = cor
        state["martingale_count"] = 0
        state["last_signal_pattern"] = estrategia
        state["last_signal_sequence"] = seq_str
        state["last_signal_round_id"] = state["last_round_id"]
        logger.info(f"Sinal enviado: {cor} | {estrategia} | conf {confianca:.3f}")

async def api_worker():
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await update_history_from_api(session)
                await resolve_after_result()
            except Exception as e:
                logger.exception("Erro no api_worker")
                await send_error_to_channel(f"Erro grave no loop da API:\n<code>{str(e)}</code>")
                await asyncio.sleep(10)
            await asyncio.sleep(API_POLL_INTERVAL)

async def scheduler_worker():
    await asyncio.sleep(3)
    while True:
        try:
            await refresh_analise_message()
            await try_send_signal()
        except Exception as e:
            logger.exception("Erro no scheduler")
            await send_error_to_channel(f"Erro no envio de sinais:\n<code>{str(e)}</code>")
        await asyncio.sleep(SIGNAL_CYCLE_INTERVAL)

async def main():
    logger.info("🤖 Bot iniciado...")
    await send_to_channel("🤖 Bot iniciado - procurando sinais avançados...")
    await asyncio.gather(api_worker(), scheduler_worker())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot parado pelo usuário")
    except Exception as e:
        logger.critical("Erro fatal", exc_info=True)
        try:
            asyncio.run(send_error_to_channel(f"ERRO FATAL: {str(e)}"))
        except:
            pass
