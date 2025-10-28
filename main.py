import asyncio
import aiohttp
import logging
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler
from telegram.error import TelegramError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from collections import Counter
import uuid

# Configurações do Bot (valores fixos para teste)
BOT_TOKEN = "7758723414:AAF-Zq1QPoGy2IS-iK2Wh28PfexP0_mmHHc"
CHAT_ID = "-1002506692600"
API_URL = "https://api.casinoscores.com/svc-evolution-game-events/api/bacbo/latest"

# Inicializar o bot e a aplicação
bot = Bot(token=BOT_TOKEN)
application = Application.builder().token(BOT_TOKEN).build()

# Configuração de logging
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s")

# Histórico e estado
historico = []
empates_historico = []  # Novo: armazena resultados de empates
ultimo_padrao_id = None
ultimo_resultado_id = None
sinais_ativos = []
placar = {
    "ganhos_seguidos": 0,
    "ganhos_gale1": 0,
    "ganhos_gale2": 0,
    "losses": 0,
    "empates": 0
}
rodadas_desde_erro = 0
ultima_mensagem_monitoramento = None
detecao_pausada = False
aguardando_validacao = False

# Mapeamento de outcomes para emojis
OUTCOME_MAP = {
    "PlayerWon": "blue_circle",
    "BankerWon": "red_circle",
    "Tie": "yellow_circle"
}

# Padrões
PADROES = [
    { "id": 1, "sequencia": ["blue_circle", "red_circle", "blue_circle", "red_circle"], "sinal": "blue_circle" },
    { "id": 2, "sequencia": ["red_circle", "red_circle", "blue_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 3, "sequencia": ["blue_circle", "blue_circle", "blue_circle", "red_circle"], "sinal": "blue_circle" },
    { "id": 4, "sequencia": ["red_circle", "blue_circle", "blue_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 5, "sequencia": ["blue_circle", "blue_circle", "red_circle"], "sinal": "blue_circle" },
    { "id": 6, "sequencia": ["red_circle", "blue_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 7, "sequencia": ["red_circle", "red_circle", "blue_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 8, "sequencia": ["blue_circle", "red_circle", "red_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 9, "sequencia": ["red_circle", "blue_circle", "red_circle", "blue_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 10, "sequencia": ["blue_circle", "blue_circle", "red_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 11, "sequencia": ["blue_circle", "red_circle", "blue_circle", "red_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 12, "sequencia": ["red_circle", "red_circle", "blue_circle"], "sinal": "red_circle" },
    { "id": 13, "sequencia": ["blue_circle", "blue_circle", "blue_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 14, "sequencia": ["red_circle", "blue_circle", "blue_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 15, "sequencia": ["blue_circle", "red_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 16, "sequencia": ["red_circle", "blue_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 17, "sequencia": ["blue_circle", "blue_circle", "red_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 18, "sequencia": ["blue_circle", "red_circle", "red_circle", "blue_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 19, "sequencia": ["red_circle", "blue_circle", "blue_circle", "red_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 20, "sequencia": ["blue_circle", "blue_circle", "red_circle", "blue_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 21, "sequencia": ["blue_circle", "red_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 22, "sequencia": ["red_circle", "blue_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 23, "sequencia": ["blue_circle", "blue_circle", "red_circle"], "sinal": "blue_circle" },
    { "id": 24, "sequencia": ["red_circle", "red_circle", "blue_circle"], "sinal": "red_circle" },
    { "id": 25, "sequencia": ["blue_circle", "red_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 26, "sequencia": ["red_circle", "blue_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 27, "sequencia": ["blue_circle", "blue_circle", "red_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 28, "sequencia": ["red_circle", "blue_circle", "blue_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 29, "sequencia": ["blue_circle", "red_circle", "blue_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 30, "sequencia": ["red_circle", "red_circle", "blue_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 31, "sequencia": ["blue_circle", "blue_circle", "blue_circle", "red_circle"], "sinal": "blue_circle" },
    { "id": 32, "sequencia": ["red_circle", "blue_circle", "blue_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 33, "sequencia": ["blue_circle", "blue_circle", "red_circle"], "sinal": "blue_circle" },
    { "id": 34, "sequencia": ["red_circle", "blue_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 35, "sequencia": ["red_circle", "red_circle", "blue_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 36, "sequencia": ["blue_circle", "red_circle", "red_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 37, "sequencia": ["red_circle", "blue_circle", "red_circle", "blue_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 38, "sequencia": ["blue_circle", "blue_circle", "red_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 39, "sequencia": ["blue_circle", "red_circle", "blue_circle", "red_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 40, "sequencia": ["red_circle", "red_circle", "blue_circle"], "sinal": "red_circle" },
    { "id": 41, "sequencia": ["blue_circle", "blue_circle", "blue_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 42, "sequencia": ["red_circle", "blue_circle", "blue_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 43, "sequencia": ["blue_circle", "red_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 44, "sequencia": ["red_circle", "blue_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 45, "sequencia": ["blue_circle", "blue_circle", "red_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 46, "sequencia": ["blue_circle", "red_circle", "red_circle", "blue_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 47, "sequencia": ["red_circle", "blue_circle", "blue_circle", "red_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 48, "sequencia": ["blue_circle", "blue_circle", "red_circle", "blue_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 49, "sequencia": ["blue_circle", "red_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 50, "sequencia": ["red_circle", "blue_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 51, "sequencia": ["blue_circle", "blue_circle", "red_circle"], "sinal": "blue_circle" },
    { "id": 52, "sequencia": ["red_circle", "red_circle", "blue_circle"], "sinal": "red_circle" },
    { "id": 53, "sequencia": ["blue_circle", "red_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 54, "sequencia": ["red_circle", "blue_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 55, "sequencia": ["blue_circle", "blue_circle", "red_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 56, "sequencia": ["red_circle", "blue_circle", "blue_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 57, "sequencia": ["blue_circle", "red_circle", "blue_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 58, "sequencia": ["red_circle", "red_circle", "blue_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 59, "sequencia": ["blue_circle", "blue_circle", "blue_circle", "red_circle"], "sinal": "blue_circle" },
    { "id": 60, "sequencia": ["red_circle", "blue_circle", "blue_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 61, "sequencia": ["blue_circle", "blue_circle", "red_circle"], "sinal": "blue_circle" },
    { "id": 62, "sequencia": ["red_circle", "blue_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 63, "sequencia": ["red_circle", "red_circle", "blue_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 64, "sequencia": ["blue_circle", "red_circle", "red_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 65, "sequencia": ["red_circle", "blue_circle", "red_circle", "blue_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 66, "sequencia": ["blue_circle", "blue_circle", "red_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 67, "sequencia": ["blue_circle", "red_circle", "blue_circle", "red_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 68, "sequencia": ["red_circle", "red_circle", "blue_circle"], "sinal": "red_circle" },
    { "id": 69, "sequencia": ["blue_circle", "blue_circle", "blue_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 70, "sequencia": ["red_circle", "blue_circle", "blue_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 71, "sequencia": ["blue_circle", "red_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 72, "sequencia": ["red_circle", "blue_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 73, "sequencia": ["blue_circle", "blue_circle", "red_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 74, "sequencia": ["blue_circle", "red_circle", "red_circle", "blue_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 75, "sequencia": ["red_circle", "blue_circle", "blue_circle", "red_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 76, "sequencia": ["blue_circle", "blue_circle", "red_circle", "blue_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 77, "sequencia": ["blue_circle", "red_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 78, "sequencia": ["red_circle", "blue_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 79, "sequencia": ["blue_circle", "blue_circle", "red_circle"], "sinal": "blue_circle" },
    { "id": 80, "sequencia": ["red_circle", "red_circle", "blue_circle"], "sinal": "red_circle" },
    { "id": 81, "sequencia": ["blue_circle", "red_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 82, "sequencia": ["red_circle", "blue_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 83, "sequencia": ["blue_circle", "blue_circle", "red_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 84, "sequencia": ["red_circle", "blue_circle", "blue_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 85, "sequencia": ["blue_circle", "red_circle", "blue_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 86, "sequencia": ["red_circle", "red_circle", "blue_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 87, "sequencia": ["blue_circle", "blue_circle", "blue_circle", "red_circle"], "sinal": "blue_circle" },
    { "id": 88, "sequencia": ["red_circle", "blue_circle", "blue_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 89, "sequencia": ["blue_circle", "blue_circle", "red_circle"], "sinal": "blue_circle" },
    { "id": 90, "sequencia": ["red_circle", "blue_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 91, "sequencia": ["red_circle", "red_circle", "blue_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 92, "sequencia": ["blue_circle", "red_circle", "red_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 93, "sequencia": ["red_circle", "blue_circle", "red_circle", "blue_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 94, "sequencia": ["blue_circle", "blue_circle", "red_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 95, "sequencia": ["blue_circle", "red_circle", "blue_circle", "red_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 96, "sequencia": ["red_circle", "red_circle", "blue_circle"], "sinal": "red_circle" },
    { "id": 97, "sequencia": ["blue_circle", "blue_circle", "blue_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 98, "sequencia": ["red_circle", "blue_circle", "blue_circle", "blue_circle"], "sinal": "blue_circle" },
    { "id": 99, "sequencia": ["blue_circle", "red_circle", "red_circle"], "sinal": "red_circle" },
    { "id": 100, "sequencia": ["red_circle", "blue_circle", "blue_circle"], "sinal": "blue_circle" }
]

@retry(stop=stop_after_attempt(7), wait=wait_exponential(multiplier=1, min=4, max=60), retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)))
async def fetch_resultado():
    """Busca o resultado mais recente da API com retry e timeout aumentado."""
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(API_URL, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status != 200:
                    return None, None, None, None
                data = await response.json()
                if 'data' not in data or 'result' not in data['data'] or 'outcome' not in data['data']['result']:
                    return None, None, None, None
                if 'id' not in data:
                    return None, None, None, None
                if data['data'].get('status') != 'Resolved':
                    return None, None, None, None
                resultado_id = data['id']
                outcome = data['data']['result']['outcome']
                player_score = data['data']['result'].get('playerDice', {}).get('score', 0)
                banker_score = data['data']['result'].get('bankerDice', {}).get('score', 0)
                if outcome not in OUTCOME_MAP:
                    return None, None, None, None
                resultado = OUTCOME_MAP[outcome]
                return resultado, resultado_id, player_score, banker_score
        except:
            return None, None, None, None

def verificar_tendencia(historico, sinal, tamanho_janela=8):
    if len(historico) < tamanho_janela:
        return True
    janela = historico[-tamanho_janela:]
    contagem = Counter(janela)
    total = contagem["red_circle"] + contagem["blue_circle"]
    if total == 0:
        return True
    return True

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(TelegramError))
async def enviar_sinal(sinal, padrao_id, resultado_id, sequencia):
    global ultima_mensagem_monitoramento, aguardando_validacao
    try:
        if ultima_mensagem_monitoramento:
            try:
                await bot.delete_message(chat_id=CHAT_ID, message_id=ultima_mensagem_monitoramento)
            except TelegramError:
                pass
            ultima_mensagem_monitoramento = None
        if aguardando_validacao or sinais_ativos:
            logging.info(f"Sinal bloqueado: aguardando validação ou sinal ativo (ID: {padrao_id})")
            return False
        
        sequencia_str = " ".join(sequencia)
        mensagem = f"""ROBOT QUILEBA BOT ROBOT
ENTRA NO: {sinal}
PROTEJA O EMPATE yellow_circle
Sequência: {sequencia_str}"""

        # Adiciona o botão "EMPATES yellow_circle"
        keyboard = [[InlineKeyboardButton("EMPATES yellow_circle", callback_data="mostrar_empates")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message = await bot.send_message(chat_id=CHAT_ID, text=mensagem, reply_markup=reply_markup)
        
        sinais_ativos.append({
            "sinal": sinal,
            "padrao_id": padrao_id,
            "resultado_id": resultado_id,
            "sequencia": sequencia,
            "enviado_em": asyncio.get_event_loop().time(),
            "gale_nivel": 0,
            "gale_message_id": None
        })
        aguardando_validacao = True
        logging.info(f"Sinal enviado para padrão {padrao_id}: {sinal}")
        return message.message_id
    except TelegramError as e:
        logging.error(f"Erro ao enviar sinal: {e}")
        raise

async def mostrar_empates(update, context):
    """Handler para o botão EMPATES yellow_circle"""
    try:
        if not empates_historico:
            await update.callback_query.answer("Nenhum empate registrado ainda.")
            return
        empates_str = "\n".join([f"Empate {i+1}: yellow_circle (blue_circle {e['player_score']} x red_circle {e['banker_score']})" for i, e in enumerate(empates_historico)])
        mensagem = f"Histórico de Empates yellow_circle\n\n{empates_str}"
        await update.callback_query.message.reply_text(mensagem)
        await update.callback_query.answer()
    except TelegramError as e:
        logging.error(f"Erro ao mostrar empates: {e}")
        await update.callback_query.answer("Erro ao exibir empates.")

async def resetar_placar():
    global placar
    placar = {
        "ganhos_seguidos": 0,
        "ganhos_gale1": 0,
        "ganhos_gale2": 0,
        "losses": 0,
        "empates": 0
    }
    try:
        await bot.send_message(chat_id=CHAT_ID, text="Placar resetado após 10 erros! Começando do zero.")
        await enviar_placar()
    except TelegramError:
        pass

async def enviar_placar():
    try:
        total_acertos = placar['ganhos_seguidos'] + placar['ganhos_gale1'] + placar['ganhos_gale2'] + placar['empates']
        total_sinais = total_acertos + placar['losses']
        precisao = (total_acertos / total_sinais * 100) if total_sinais > 0 else 0.0
        precisao = min(precisao, 100.0)
        mensagem_placar = f"""QUILEBA PLACAR
ACERTOS: {total_acertos}
ERROS: {placar['losses']}
PRECISÃO: {precisao:.2f}%"""
        await bot.send_message(chat_id=CHAT_ID, text=mensagem_placar)
    except TelegramError:
        pass

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(TelegramError))
async def enviar_resultado(resultado, player_score, banker_score, resultado_id):
    global rodadas_desde_erro, ultima_mensagem_monitoramento, detecao_pausada, placar, ultimo_padrao_id, aguardando_validacao, empates_historico
    try:
        # Armazena empates no histórico
        if resultado == "yellow_circle":
            empates_historico.append({"player_score": player_score, "banker_score": banker_score})
            if len(empates_historico) > 50:  # Limita o histórico para evitar excesso de memória
                empates_historico.pop(0)
        for sinal_ativo in sinais_ativos[:]:
            if sinal_ativo["resultado_id"] != resultado_id:
                if resultado == sinal_ativo["sinal"] or resultado == "yellow_circle":
                    if resultado == "yellow_circle":
                        placar["empates"] += 1
                    if sinal_ativo["gale_nivel"] == 0:
                        placar["ganhos_seguidos"] += 1
                    elif sinal_ativo["gale_nivel"] == 1:
                        placar["ganhos_gale1"] += 1
                    else:
                        placar["ganhos_gale2"] += 1
                    if sinal_ativo["gale_message_id"]:
                        try:
                            await bot.delete_message(chat_id=CHAT_ID, message_id=sinal_ativo["gale_message_id"])
                        except TelegramError:
                            pass
                    mensagem_validacao = f" ACERTOU check_mark\nResultado: blue_circle {player_score} x red_circle {banker_score}"
                    await bot.send_message(chat_id=CHAT_ID, text=mensagem_validacao)
                    await enviar_placar()
                    ultimo_padrao_id = None
                    aguardando_validacao = False
                    sinais_ativos.remove(sinal_ativo)
                    detecao_pausada = False
                    logging.info(f"Sinal validado com sucesso para padrão {sinal_ativo['padrao_id']}")
                else:
                    if sinal_ativo["gale_nivel"] == 0:
                        detecao_pausada = True
                        mensagem_gale = "FAZER 1º Gale"
                        message = await bot.send_message(chat_id=CHAT_ID, text=mensagem_gale)
                        sinal_ativo["gale_nivel"] = 1
                        sinal_ativo["gale_message_id"] = message.message_id
                        sinal_ativo["resultado_id"] = resultado_id
                    elif sinal_ativo["gale_nivel"] == 1:
                        detecao_pausada = True
                        mensagem_gale = "FAZER 2º Gale"
                        try:
                            await bot.delete_message(chat_id=CHAT_ID, message_id=sinal_ativo["gale_message_id"])
                        except TelegramError:
                            pass
                        message = await bot.send_message(chat_id=CHAT_ID, text=mensagem_gale)
                        sinal_ativo["gale_nivel"] = 2
                        sinal_ativo["gale_message_id"] = message.message_id
                        sinal_ativo["resultado_id"] = resultado_id
                    else:
                        placar["losses"] += 1
                        if sinal_ativo["gale_message_id"]:
                            try:
                                await bot.delete_message(chat_id=CHAT_ID, message_id=sinal_ativo["gale_message_id"])
                            except TelegramError:
                                pass
                        await bot.send_message(chat_id=CHAT_ID, text="ERRAMOS cross_mark")
                        await enviar_placar()
                        if placar["losses"] >= 10:
                            await resetar_placar()
                        ultimo_padrao_id = None
                        aguardando_validacao = False
                        sinais_ativos.remove(sinal_ativo)
                        detecao_pausada = False
                        logging.info(f"Sinal perdido para padrão {sinal_ativo['padrao_id']}, validação liberada")
                ultima_mensagem_monitoramento = None
            elif asyncio.get_event_loop().time() - sinal_ativo["enviado_em"] > 300:
                if sinal_ativo["gale_message_id"]:
                    try:
                        await bot.delete_message(chat_id=CHAT_ID, message_id=sinal_ativo["gale_message_id"])
                    except TelegramError:
                        pass
                ultimo_padrao_id = None
                aguardando_validacao = False
                sinais_ativos.remove(sinal_ativo)
                detecao_pausada = False
                logging.info(f"Sinal expirado para padrão {sinal_ativo['padrao_id']}, validação liberada")
        if not sinais_ativos:
            aguardando_validacao = False
    except TelegramError:
        pass

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(TelegramError))
async def enviar_monitoramento():
    global ultima_mensagem_monitoramento
    while True:
        try:
            if not sinais_ativos:
                if ultima_mensagem_monitoramento:
                    try:
                        await bot.delete_message(chat_id=CHAT_ID, message_id=ultima_mensagem_monitoramento)
                    except TelegramError:
                        pass
                message = await bot.send_message(chat_id=CHAT_ID, text="MONITORANDO A MESA...")
                ultima_mensagem_monitoramento = message.message_id
            await asyncio.sleep(15)
        except TelegramError:
            await asyncio.sleep(15)

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(TelegramError))
async def enviar_relatorio():
    while True:
        try:
            total_acertos = placar['ganhos_seguidos'] + placar['ganhos_gale1'] + placar['ganhos_gale2'] + placar['empates']
            total_sinais = total_acertos + placar['losses']
            precisao = (total_acertos / total_sinais * 100) if total_sinais > 0 else 0.0
            precisao = min(precisao, 100.0)
            msg = f"""QUILEBA PLACAR 
ACERTOS: {total_acertos}
ERROS: {placar['losses']}
PRECISÃO: {precisao:.2f}%"""
            await bot.send_message(chat_id=CHAT_ID, text=msg)
        except TelegramError:
            pass
        await asyncio.sleep(3600)

async def enviar_erro_telegram(erro_msg):
    try:
        await bot.send_message(chat_id=CHAT_ID, text=f"Erro detectado: {erro_msg}")
    except TelegramError:
        pass

async def main():
    global historico, ultimo_padrao_id, ultimo_resultado_id, rodadas_desde_erro, detecao_pausada, aguardando_validacao
    # Registrar o handler para o botão de empates
    application.add_handler(CallbackQueryHandler(mostrar_empates, pattern="mostrar_empates"))
    # Iniciar o polling da aplicação
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    asyncio.create_task(enviar_relatorio())
    asyncio.create_task(enviar_monitoramento())
    try:
        await bot.send_message(chat_id=CHAT_ID, text="Bot iniciado com sucesso!")
    except TelegramError:
        pass
    while True:
        try:
            resultado, resultado_id, player_score, banker_score = await fetch_resultado()
            if not resultado or not resultado_id:
                await asyncio.sleep(2)
                continue
            if resultado_id == ultimo_resultado_id:
                await asyncio.sleep(2)
                continue
            ultimo_resultado_id = resultado_id
            historico.append(resultado)
            if len(historico) > 50:
                historico.pop(0)
            await enviar_resultado(resultado, player_score, banker_score, resultado_id)
            if not detecao_pausada and not aguardando_validacao and not sinais_ativos:
                for padrao in PADROES:
                    seq_len = len(padrao["sequencia"])
                    if len(historico) >= seq_len:
                        if historico[-seq_len:] == padrao["sequencia"] and padrao["id"] != ultimo_padrao_id:
                            if verificar_tendencia(historico, padrao["sinal"]):
                                enviado = await enviar_sinal(padrao["sinal"], padrao["id"], resultado_id, padrao["sequencia"])
                                if enviado:
                                    ultimo_padrao_id = padrao["id"]
                                    break
            await asyncio.sleep(2)
        except Exception as e:
            await enviar_erro_telegram(str(e))
            await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot encerrado pelo usuário")
    except Exception as e:
        logging.error(f"Erro fatal no bot: {e}")
        asyncio.run(enviar_erro_telegram(f"Erro fatal no bot: {e}"))
