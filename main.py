import os
import json
import asyncio
import logging
import math
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime
import pytz
from collections import Counter

import aiohttp
from telegram import Bot
from telegram.error import TelegramError
from dotenv import load_dotenv

load_dotenv()

# Configurações
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7891678107:AAEmt_oN2Safe_2gEPS7x7XNeP8AA4hQXCI")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "-1003779669370")

# URL base SEM lastResult para carga inicial (pega os últimos resultados)
BASE_API_URL = "https://api.signals-house.com/validate/results?tableId=27"

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

# ─── TIMING ───
API_POLL_INTERVAL = 0.5
SIGNAL_COOLDOWN_DURATION = 4.5
POST_RESULT_DELAY = 1.2

# ─── PARÂMETROS DA ESTRATÉGIA ───
JANELA_PRINCIPAL = 36
JANELA_EMPATE = 20
JANELA_ENTROPIA = 12
MIN_DESVIO_PORCENTAGEM = 3.5
MIN_CONFANCA = 55.0
MAX_TAXA_EMPATE_RECENTE = 18.0
P_CASA = 44.5
P_VISITANTE = 44.5
P_TIE = 11.0

STATE_FILE = "bot_state.json"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-5s | %(message)s'
)
logger = logging.getLogger("FootballStudioBot")

bot = Bot(token=TELEGRAM_BOT_TOKEN)

# ─── STICKERS ───
GREEN_STICKER_ID = "CAACAgEAAxkBAAMGablwQw7_e6LQPpkPLMGUT_7XHlsAApECAAJAHLhE5HwaQw6L5SA6BA"
LOSS_STICKER_ID = "CAACAgEAAxkBAAMHablwRJ3yTERtooEJKzCbGMCfvv8AAucCAALGBLlEm0eHrOWqoe06BA"

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
    "total_empates": 0,
    "total_losses": 0,
    "last_signal_pattern": None,
    "last_signal_sequence": None,
    "last_signal_round_id": None,
    "signal_cooldown_until": 0.0,
    "analise_message_id": None,
    "last_reset_date": None,
    "last_analise_refresh": 0.0,
    "last_result_round_id": None,
    "player_score_last": None,
    "banker_score_last": None,
    "next_signal_possible_after": 0.0,
    "initial_history_loaded": False,
}


# ─── PERSISTÊNCIA DO PLACAR ───
def save_state():
    try:
        data = {
            "total_greens": state["total_greens"],
            "greens_sem_gale": state["greens_sem_gale"],
            "greens_gale_1": state["greens_gale_1"],
            "total_empates": state["total_empates"],
            "total_losses": state["total_losses"],
            "greens_seguidos": state["greens_seguidos"],
        }
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.debug(f"Erro ao salvar estado: {e}")


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        for k in ["total_greens", "greens_sem_gale", "greens_gale_1",
                   "total_empates", "total_losses", "greens_seguidos"]:
            if k in data:
                state[k] = data[k]
        logger.info(f"Estado carregado: Greens={state['total_greens']} Losses={state['total_losses']}")
    except FileNotFoundError:
        logger.info("Nenhum estado anterior encontrado, começando do zero.")
    except Exception as e:
        logger.debug(f"Erro ao carregar estado: {e}")


# ─── TELEGRAM HELPERS ───
async def send_to_channel(text: str, parse_mode="HTML", disable_preview=True) -> Optional[int]:
    try:
        msg = await bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=text,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_preview
        )
        return msg.message_id
    except Exception as e:
        logger.error(f"Erro ao enviar texto: {e}")
        return None


async def send_sticker_to_channel(sticker_id: str) -> Optional[int]:
    try:
        msg = await bot.send_sticker(
            chat_id=TELEGRAM_CHANNEL_ID,
            sticker=sticker_id
        )
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
    return "0.00%" if total == 0 else f"{(state['total_greens'] / total * 100):.2f}%"


def format_placar() -> str:
    acert = calcular_acertividade()
    return (
        f"📊 Placar atual 🟢 {state['total_greens']} 🔴 {state['total_losses']}\n"
        f"✅ Assertividade {acert}\n"
        f"🏆 {state['greens_seguidos']} Greens seguidos"
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


# ─── HELPER: converter resultado da API para emoji ───
def parse_outcome(outcome_raw) -> Optional[str]:
    outcome = OUTCOME_MAP.get(outcome_raw)
    if outcome:
        return outcome
    s = str(outcome_raw or "").lower()
    if "casa" in s:
        return "🔴"
    elif "visitante" in s:
        return "🔵"
    elif any(x in s for x in ["tie", "empate", "draw"]):
        return "🟡"
    return None


# ─── API ───
async def fetch_api(session: aiohttp.ClientSession, url: str) -> Optional[Dict]:
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                logger.warning(f"API retornou status {resp.status}")
            return None
    except Exception as e:
        logger.debug(f"Erro fetch API: {e}")
        return None


async def load_initial_history(session: aiohttp.ClientSession):
    """Carrega histórico inicial da API (sem lastResult) para ter dados suficientes."""
    logger.info("Carregando histórico inicial da API...")
    url = BASE_API_URL
    data = await fetch_api(session, url)
    if not data:
        logger.warning("Falha ao carregar histórico inicial")
        return

    items = data.get("data", [])
    if not isinstance(items, list) or len(items) == 0:
        logger.warning("API retornou lista vazia no histórico inicial")
        return

    # A API retorna em ordem decrescente (mais recente primeiro)
    # Precisamos inverter para que o histórico fique cronológico
    items_sorted = sorted(items, key=lambda x: x.get("id", 0))

    count = 0
    for item in items_sorted:
        outcome_raw = item.get("result")
        outcome = parse_outcome(outcome_raw)
        if outcome:
            state["history"].append(outcome)
            count += 1

    # Guardar o ID mais recente como último processado
    if items:
        # O mais recente é o primeiro item (antes de inverter)
        newest = max(items, key=lambda x: x.get("id", 0))
        state["last_round_id"] = newest.get("id")

    # Limitar histórico
    if len(state["history"]) > 200:
        state["history"] = state["history"][-200:]

    state["initial_history_loaded"] = True
    logger.info(f"Histórico inicial carregado: {count} rodadas | Último ID: {state['last_round_id']}")
    logger.info(f"Últimas 10 rodadas: {''.join(state['history'][-10:])}")


def build_api_url() -> str:
    """Constrói URL com lastResult dinâmico para pegar apenas novos resultados."""
    if state["last_round_id"] is not None:
        return f"{BASE_API_URL}&lastResult={state['last_round_id']}"
    return BASE_API_URL


async def update_history_from_api(session) -> bool:
    """Busca novos resultados da API e atualiza o histórico."""
    url = build_api_url()
    data = await fetch_api(session, url)
    if not data:
        return False

    try:
        items = data.get("data", [])
        if not isinstance(items, list) or len(items) == 0:
            return False

        # A API retorna em ordem decrescente, pegar apenas os novos
        # (IDs maiores que o last_round_id)
        new_items = []
        for item in items:
            item_id = item.get("id")
            if item_id and (state["last_round_id"] is None or item_id > state["last_round_id"]):
                new_items.append(item)

        if not new_items:
            return False

        # Ordenar cronologicamente (do mais antigo ao mais recente)
        new_items.sort(key=lambda x: x.get("id", 0))

        found_new = False
        for item in new_items:
            round_id = item.get("id")
            outcome_raw = item.get("result")
            if not round_id or not outcome_raw:
                continue

            outcome = parse_outcome(outcome_raw)
            if not outcome:
                continue

            state["last_round_id"] = round_id
            state["history"].append(outcome)
            found_new = True
            logger.info(f"🔔 NOVA RODADA: {outcome} (ID: {round_id}) | Histórico: {len(state['history'])} rodadas")

        if found_new:
            # Limitar histórico
            if len(state["history"]) > 200:
                state["history"] = state["history"][-200:]

            now = datetime.now().timestamp()
            state["next_signal_possible_after"] = now + POST_RESULT_DELAY

        return found_new

    except Exception as e:
        logger.debug(f"Erro processando API: {e}")
        return False


# ─── MOTOR DE DECISÃO (ANÁLISE ESTATÍSTICA) ───
def calcular_entropia_binaria(p: float) -> float:
    if p <= 0 or p >= 1:
        return 0.0
    return -(p * math.log2(p) + (1 - p) * math.log2(1 - p))


def proporcao_na_janela(hist: List[str], janela: int) -> Tuple[float, float, float]:
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


def gerar_sinal_inteligente(history: List[str]) -> Tuple[Optional[str], Optional[str], float]:
    if len(history) < 12:
        logger.info(f"📊 Histórico insuficiente: {len(history)}/12 rodadas")
        return None, None, 0.0

    p_c, p_v, p_t = proporcao_na_janela(history, JANELA_PRINCIPAL)
    p_c_short, p_v_short, p_t_short = proporcao_na_janela(history, JANELA_EMPATE)

    logger.info(f"📊 Proporções (janela {JANELA_PRINCIPAL}): Casa={p_c:.1f}% Visit={p_v:.1f}% Emp={p_t:.1f}%")
    logger.info(f"📊 Proporções (janela {JANELA_EMPATE}): Casa={p_c_short:.1f}% Visit={p_v_short:.1f}% Emp={p_t_short:.1f}%")

    if p_t_short > MAX_TAXA_EMPATE_RECENTE:
        logger.info(f"❌ Muitos empates recentes: {p_t_short:.1f}% > {MAX_TAXA_EMPATE_RECENTE}%")
        return "Muitos empates recentes", None, 0.0

    desv_c = desvio_da_esperada(p_c, P_CASA)
    desv_v = desvio_da_esperada(p_v, P_VISITANTE)

    logger.info(f"📊 Desvios: Casa={desv_c:.1f}% Visit={desv_v:.1f}% (mín={MIN_DESVIO_PORCENTAGEM}%)")

    ent = 1.0
    if len(history) >= JANELA_ENTROPIA:
        recorte = history[-JANELA_ENTROPIA:]
        c = Counter(x for x in recorte if x in ("🔴", "🔵"))
        n_bin = sum(c.values())
        if n_bin >= 6:
            p_bin = c["🔴"] / n_bin
            ent = calcular_entropia_binaria(p_bin)
            logger.info(f"📊 Entropia binária: {ent:.3f} (p_casa_bin={p_bin:.2f})")

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
        logger.info(f"📊 Penalidade: proporções curtas muito próximas ({abs(p_c_short - p_v_short):.1f}%)")

    logger.info(f"📊 Score final: {score:.2f} (mín=1.6) | Cor: {cor_favor}")

    if score < 1.6 or cor_favor is None:
        return "Sem força estatística suficiente", None, 0.0

    confianca = min(78.0, 52.0 + score * 4.2)

    if confianca < MIN_CONFANCA:
        logger.info(f"📊 Confiança baixa: {confianca:.1f}% < {MIN_CONFANCA}%")
        return "Confiança abaixo do mínimo", None, confianca

    nome = "Desequilíbrio estatístico"
    if ent < 0.75:
        nome += " + baixa entropia"

    logger.info(f"✅ SINAL APROVADO: {cor_favor} | Confiança: {confianca:.1f}% | {nome}")
    return nome, cor_favor, round(confianca, 1)


def gerar_sinal_estrategia(history: List[str]) -> Tuple[Optional[str], Optional[str]]:
    nome, cor, confianca = gerar_sinal_inteligente(history)
    if cor is None:
        return None, None
    return f"{nome} ({confianca}%)", cor


# ─── MENSAGEM DE SINAL ───
def main_entry_text(color: str) -> str:
    return (
        f"🧠 | Sinal confirmado\n"
        f"⚽️ | Mesa Football Studio\n"
        f"⚔️ | Aposte no {color} + 🟠\n"
        f"♻️ | Fazer máximo G1\n"
        f"💻 | Abra o jogo pelo link abaixo ⤵️\n"
        f"\n"
        f'<a href="https://btt-pt.hopghpfa.com/pt/casino?partner=p8506p33116p4649#registration-bonus">👉 Regista-te aqui: BETILT</a>'
    )


async def send_gale_warning(level: int):
    if level != 1:
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

    if state["last_signal_round_id"] and state["last_signal_round_id"] >= state["last_round_id"]:
        return

    last_outcome = state["history"][-1]
    state["last_result_round_id"] = state["last_round_id"]

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
            "waiting_for_result": False,
            "last_signal_color": None,
            "martingale_count": 0,
            "entrada_message_id": None,
            "last_signal_pattern": None,
            "last_signal_sequence": None,
            "last_signal_round_id": None,
            "signal_cooldown_until": datetime.now().timestamp() + 2
        })
        save_state()
        return

    state["martingale_count"] += 1

    if state["martingale_count"] == 1:
        await send_gale_warning(1)

    if state["martingale_count"] >= 2:
        state["greens_seguidos"] = 0
        state["total_losses"] += 1

        await send_sticker_to_channel(LOSS_STICKER_ID)
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
            "signal_cooldown_until": datetime.now().timestamp() + 2
        })
        save_state()

    await refresh_analise_message()


async def try_send_signal():
    now = datetime.now().timestamp()

    if state["waiting_for_result"]:
        await delete_analise_message()
        return

    if now < state["signal_cooldown_until"]:
        return

    if now < state.get("next_signal_possible_after", 0):
        return

    if len(state["history"]) < 12:
        logger.info(f"⏳ Aguardando histórico: {len(state['history'])}/12 rodadas")
        return

    padrao, cor = gerar_sinal_estrategia(state["history"])

    if not cor:
        await refresh_analise_message()
        return

    seq = "".join(state["history"][-6:])
    if state["last_signal_pattern"] == padrao and state["last_signal_sequence"] == seq:
        await refresh_analise_message()
        return

    await delete_analise_message()

    state["martingale_message_ids"] = []
    msg_id = await send_to_channel(main_entry_text(cor), disable_preview=False)

    if msg_id:
        state["entrada_message_id"] = msg_id
        state["waiting_for_result"] = True
        state["last_signal_color"] = cor
        state["martingale_count"] = 0
        state["last_signal_pattern"] = padrao
        state["last_signal_sequence"] = seq
        state["last_signal_round_id"] = state["last_round_id"]
        state["signal_cooldown_until"] = now + SIGNAL_COOLDOWN_DURATION
        logger.info(f"⚡ SINAL ENVIADO → {cor} ({padrao})")


async def api_worker():
    connector = aiohttp.TCPConnector(limit=5, keepalive_timeout=30)
    async with aiohttp.ClientSession(connector=connector) as session:
        # PASSO 1: Carregar histórico inicial completo
        await load_initial_history(session)
        logger.info(f"📊 Pronto para análise com {len(state['history'])} rodadas no histórico")

        # Tentar gerar sinal logo após carregar histórico
        await try_send_signal()

        poll_count = 0
        while True:
            try:
                nova_rodada = await update_history_from_api(session)
                if nova_rodada:
                    await resolve_after_result()
                    await asyncio.sleep(0.3)
                    await try_send_signal()

                poll_count += 1
                if poll_count % 120 == 0:  # A cada ~60 segundos
                    logger.info(f"💓 Heartbeat: {len(state['history'])} rodadas | Último ID: {state['last_round_id']} | Aguardando resultado: {state['waiting_for_result']}")

                await asyncio.sleep(API_POLL_INTERVAL)
            except Exception as e:
                logger.error(f"Erro loop principal: {e}")
                await asyncio.sleep(API_POLL_INTERVAL)


async def main():
    load_state()
    logger.info("Bot iniciado...")
    await send_to_channel("🤖 BOT INICIADO FOOTBALL STUDIO 🤖")
    await refresh_analise_message()
    await api_worker()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot parado pelo usuário")
    except Exception as e:
        logger.critical("Erro fatal", exc_info=True)
