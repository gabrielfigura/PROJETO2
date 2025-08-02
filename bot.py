import asyncio
import aiohttp
import logging
import os
from telegram import Bot
from telegram.error import TelegramError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from collections import Counter
import math
import time

# Configurações do Bot
BOT_TOKEN = os.getenv("BOT_TOKEN", "8344261996:AAEgDWaIb7hzknPpTQMdiYKSE3hjzP0mqFc")
CHAT_ID = os.getenv("CHAT_ID", "-1002783091818")
API_URL = "https://api.casinoscores.com/svc-evolution-game-events/api/bacbo/latest"

# Inicializar o bot
bot = Bot(token=BOT_TOKEN)

# Configuração de logging
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s")

# Histórico e estado
historico = []
ultimo_padrao_id = None
ultimo_resultado_id = None
sinais_ativos = []
placar = {"✅": 0, "❌": 0}
rodadas_desde_erro = 0
ultima_mensagem_monitoramento = None
detecao_pausada = False
sinais_por_hora = 0
ultima_hora = int(time.time() / 3600)

# Mapeamento de outcomes para emojis
OUTCOME_MAP = {
    "PlayerWon": "🔵",
    "BankerWon": "🔴",
    "Tie": "🟡"
}

# Padrões otimizados com novos padrões de 3 a 4 sequências
PADROES = [
    {"id": 13, "sequencia": ["🔵", "🔵", "🔵", "🔴", "🔴", "🔵", "🔵"], "sinal": "🔴"},
    {"id": 14, "sequencia": ["🔴", "🔴", "🔴", "🔵", "🔵", "🔴", "🔴"], "sinal": "🔵"},
    {"id": 17, "sequencia": ["🔴", "🔴", "🔵", "🔵", "🔴"], "sinal": "🔴"},
    {"id": 18, "sequencia": ["🔵", "🔵", "🔴", "🔴", "🔵"], "sinal": "🔵"},
    {"id": 21, "sequencia": ["🔵", "🔵", "🔵", "🔴", "🔵", "🔵"], "sinal": "🔵"},
    {"id": 22, "sequencia": ["🔴", "🔴", "🔴", "🔵", "🔴", "🔴"], "sinal": "🔴"},
    {"id": 23, "sequencia": ["🔵", "🔵", "🔴", "🔵", "🔵"], "sinal": "🔴"},
    {"id": 24, "sequencia": ["🔴", "🔴", "🔵", "🔴", "🔴"], "sinal": "🔵"},
    {"id": 2, "sequencia": ["🔴", "🔴", "🔴", "🔴", "🔴"], "sinal": "🔴"},
    {"id": 3, "sequencia": ["🔵", "🔵", "🔵", "🔵", "🔵"], "sinal": "🔵"},
    {"id": 6, "sequencia": ["🔴", "🔴", "🔴", "🔴", "🔵"], "sinal": "🔵"},
    {"id": 7, "sequencia": ["🔵", "🔵", "🔵", "🔵", "🔴"], "sinal": "🔴"},
    {"id": 8, "sequencia": ["🔴", "🔵", "🔴", "🔵", "🔴"], "sinal": "🔵"},
    {"id": 9, "sequencia": ["🔵", "🔴", "🔵", "🔴", "🔵"], "sinal": "🔴"},
    {"id": 25, "sequencia": ["🔴", "🔴", "🔵"], "sinal": "🔴"},
    {"id": 26, "sequencia": ["🔵", "🔵", "🔴"], "sinal": "🔵"},
    {"id": 27, "sequencia": ["🔴", "🔵", "🔴"], "sinal": "🔵"},
    {"id": 28, "sequencia": ["🔵", "🔴", "🔵"], "sinal": "🔴"},
    {"id": 29, "sequencia": ["🔴", "🔴", "🔴", "🔵"], "sinal": "🔵"},
    {"id": 30, "sequencia": ["🔵", "🔵", "🔵", "🔴"], "sinal": "🔴"}
]

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=30), retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)))
async def fetch_resultado():
    """Busca o resultado mais recente da API com retry e timeout aumentado."""
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(API_URL, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status != 200:
                    logging.error(f"Erro na API: Status {response.status}, Resposta: {await response.text()}")
                    return None, None, None, None
                data = await response.json()
                logging.debug(f"Resposta da API: {data}")
                
                if 'data' not in data or 'result' not in data['data'] or 'outcome' not in data['data']['result']:
                    logging.error(f"Estrutura inválida na resposta: {data}")
                    return None, None, None, None
                if 'id' not in data:
                    logging.error(f"Chave 'id' não encontrada na resposta: {data}")
                    return None, None, None, None
                
                if data['data'].get('status') != 'Resolved':
                    logging.debug(f"Jogo não resolvido: Status {data['data'].get('status')}")
                    return None, None, None, None
                
                resultado_id = data['id']
                outcome = data['data']['result']['outcome']
                player_score = data['data']['result'].get('playerDice', {}).get('score', 0)
                banker_score = data['data']['result'].get('bankerDice', {}).get('score', 0)
                
                if outcome not in OUTCOME_MAP:
                    logging.error(f"Outcome inválido: {outcome}")
                    return None, None, None, None
                resultado = OUTCOME_MAP[outcome]
                
                return resultado, resultado_id, player_score, banker_score
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logging.error(f"Erro de conexão com a API: {e}")
            return None, None, None, None
        except ValueError as e:
            logging.error(f"Erro ao parsear JSON: {e}")
            return None, None, None, None
        except Exception as e:
            logging.error(f"Erro inesperado ao buscar resultado: {e}")
            return None, None, None, None

def calcular_entropia(janela):
    """Calcula a entropia da janela para evitar padrões ambíguos."""
    contagem = Counter(janela)
    total = sum(contagem.values())
    if total == 0:
        return 0
    entropia = 0
    for count in contagem.values():
        prob = count / total
        entropia -= prob * math.log2(prob) if prob > 0 else 0
    return entropia

def verificar_tendencia(historico, sinal, tamanho_janela=10):
    """Verifica se o sinal está alinhado com a tendência dos últimos resultados."""
    if len(historico) < tamanho_janela:
        return False  # Exige histórico suficiente
    janela = historico[-tamanho_janela:]
    contagem = Counter(janela)
    total = contagem["🔴"] + contagem["🔵"]
    if total == 0:
        return False
    proporcao = contagem[sinal] / total
    entropia = calcular_entropia(janela)
    logging.debug(f"Tendência: {sinal} aparece {contagem[sinal]}/{total} ({proporcao:.2%}), Entropia: {entropia:.2f}")
    return proporcao >= 0.8 and entropia < 1.5  # Alta dominância e baixa entropia

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(TelegramError))
async def enviar_sinal(sinal, padrao_id, resultado_id, sequencia):
    """Envia uma mensagem de sinal ao Telegram com retry."""
    global ultima_mensagem_monitoramento, sinais_por_hora, ultima_hora
    try:
        if ultima_mensagem_monitoramento:
            try:
                await bot.delete_message(chat_id=CHAT_ID, message_id=ultima_mensagem_monitoramento)
                logging.debug("Mensagem de monitoramento apagada")
            except TelegramError as e:
                logging.debug(f"Erro ao apagar mensagem de monitoramento: {e}")
            ultima_mensagem_monitoramento = None

        if any(sinal["padrao_id"] == padrao_id for sinal in sinais_ativos):
            logging.debug(f"Sinal com Padrão ID {padrao_id} já ativo, ignorando.")
            return

        sequencia_str = " ".join(sequencia)
        mensagem = f"""🎯 SINAL MILIONÁRIO
Padrão ID: {padrao_id}
Sequência: {sequencia_str}
Entrar: {sinal}
Proteger o empate🟡
⏳ Clever apostou, não fica de fora!"""
        message = await bot.send_message(chat_id=CHAT_ID, text=mensagem)
        logging.info(f"Sinal enviado: Padrão {padrao_id}, Sequência: {sequencia_str}, Sinal: {sinal}, Resultado ID: {resultado_id}")
        
        # Incrementar contador de sinais
        current_hour = int(time.time() / 3600)
        if current_hour != ultima_hora:
            sinais_por_hora = 0
            ultima_hora = current_hour
        sinais_por_hora += 1
        
        sinais_ativos.append({
            "sinal": sinal,
            "padrao_id": padrao_id,
            "resultado_id": resultado_id,
            "sequencia": sequencia,
            "enviado_em": asyncio.get_event_loop().time(),
            "gale_nivel": 0,
            "gale_message_id": None
        })
        return message.message_id
    except TelegramError as e:
        logging.error(f"Erro ao enviar sinal: {e}")
        raise

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(TelegramError))
async def enviar_resultado(resultado, player_score, banker_score, resultado_id):
    """Envia a validação de cada sinal ao Telegram."""
    global rodadas_desde_erro, ultima_mensagem_monitoramento, detecao_pausada, placar
    try:
        for sinal_ativo in sinais_ativos[:]:
            if sinal_ativo["resultado_id"] != resultado_id:
                resultado_texto = f"🎲 Resultado: "
                if resultado == "🟡":
                    resultado_texto += f"EMPATE: {player_score}:{banker_score}"
                else:
                    resultado_texto += f"AZUL: {player_score} VS VERMELHO: {banker_score}"

                sequencia_str = " ".join(sinal_ativo["sequencia"])
                if resultado == sinal_ativo["sinal"] or resultado == "🟡":
                    placar["✅"] += 1
                    if sinal_ativo["gale_message_id"]:
                        try:
                            await bot.delete_message(chat_id=CHAT_ID, message_id=sinal_ativo["gale_message_id"])
                            logging.debug(f"Mensagem de gale apagada: ID {sinal_ativo['gale_message_id']}")
                        except TelegramError as e:
                            logging.debug(f"Erro ao apagar mensagem de gale: {e}")
                    mensagem_validacao = f"🤑ENTROU DINHEIRO🤑\n{resultado_texto}\n📊 Resultado do sinal (Padrão {sinal_ativo['padrao_id']} Sequência: {sequencia_str})\nPlacar: {placar['✅']}✅ {placar['❌']}❌"
                    await bot.send_message(chat_id=CHAT_ID, text=mensagem_validacao)
                    logging.info(f"Validação enviada: Sinal {sinal_ativo['sinal']}, Resultado {resultado}, Resultado ID: {resultado_id}")
                    sinais_ativos.remove(sinal_ativo)
                    detecao_pausada = False
                else:
                    if sinal_ativo["gale_nivel"] == 0:
                        detecao_pausada = True
                        mensagem_gale = "BORA GANHAR NO 1º GALE🎯"
                        message = await bot.send_message(chat_id=CHAT_ID, text=mensagem_gale)
                        sinal_ativo["gale_nivel"] = 1
                        sinal_ativo["gale_message_id"] = message.message_id
                        sinal_ativo["resultado_id"] = resultado_id
                        logging.info(f"Mensagem de 1º gale enviada: {mensagem_gale}, ID: {message.message_id}")
                    else:
                        if sinal_ativo["gale_message_id"]:
                            try:
                                await bot.delete_message(chat_id=CHAT_ID, message_id=sinal_ativo["gale_message_id"])
                                logging.debug(f"Mensagem de 1º gale apagada: ID {sinal_ativo['gale_message_id']}")
                            except TelegramError as e:
                                logging.debug(f"Erro ao apagar mensagem de 1º gale: {e}")
                        placar["❌"] += 1
                        placar["✅"] = 0
                        await bot.send_message(chat_id=CHAT_ID, text="NÃO FOI DESSA🤧")
                        logging.info(f"Validação enviada (Erro 1º Gale): Sinal {sinal_ativo['sinal']}, Resultado {resultado}, Resultado ID: {resultado_id}")
                        sinais_ativos.remove(sinal_ativo)
                        detecao_pausada = False

                ultima_mensagem_monitoramento = None
            elif asyncio.get_event_loop().time() - sinal_ativo["enviado_em"] > 300:
                logging.warning(f"Sinal obsoleto removido: Padrão {sinal_ativo['padrao_id']}, Resultado ID: {sinal_ativo['resultado_id']}")
                if sinal_ativo["gale_message_id"]:
                    try:
                        await bot.delete_message(chat_id=CHAT_ID, message_id=sinal_ativo["gale_message_id"])
                        logging.debug(f"Mensagem de gale obsoleta apagada: ID {sinal_ativo['gale_message_id']}")
                    except TelegramError as e:
                        logging.debug(f"Erro ao apagar mensagem de gale obsoleta: {e}")
                sinais_ativos.remove(sinal_ativo)
                detecao_pausada = False
    except TelegramError as e:
        logging.error(f"Erro ao enviar resultado: {e}")

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(TelegramError))
async def enviar_monitoramento():
    """Envia mensagem de monitoramento a cada 10 segundos."""
    global ultima_mensagem_monitoramento
    while True:
        try:
            if not sinais_ativos:
                if ultima_mensagem_monitoramento:
                    try:
                        await bot.delete_message(chat_id=CHAT_ID, message_id=ultima_mensagem_monitoramento)
                        logging.debug("Mensagem de monitoramento anterior apagada")
                    except TelegramError as e:
                        logging.debug(f"Erro ao apagar mensagem de monitoramento: {e}")
                
                message = await bot.send_message(chat_id=CHAT_ID, text="MONITORANDO A MESA…🤌")
                ultima_mensagem_monitoramento = message.message_id
                logging.debug(f"Mensagem de monitoramento enviada: ID {ultima_mensagem_monitoramento}")
            else:
                logging.debug("Monitoramento pausado: Sinal ativo pendente")
        except TelegramError as e:
            logging.error(f"Erro ao enviar monitoramento: {e}")
        await asyncio.sleep(10)

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(TelegramError))
async def enviar_relatorio():
    """Envia um relatório periódico com o placar."""
    global sinais_por_hora, ultima_hora
    while True:
        try:
            current_hour = int(time.time() / 3600)
            if current_hour != ultima_hora:
                sinais_por_hora = 0
                ultima_hora = current_hour
            msg = f"📈 Relatório: Bot em operação\nPlacar: {placar['✅']}✅ {placar['❌']}❌\nSinais na última hora: {sinais_por_hora}"
            await bot.send_message(chat_id=CHAT_ID, text=msg)
            logging.info(f"Relatório enviado: {msg}")
        except TelegramError as e:
            logging.error(f"Erro ao enviar relatório: {e}")
        await asyncio.sleep(3600)

async def main():
    """Loop principal do bot com reconexão."""
    global historico, ultimo_padrao_id, ultimo_resultado_id, rodadas_desde_erro, detecao_pausada, sinais_por_hora, ultima_hora
    asyncio.create_task(enviar_relatorio())
    asyncio.create_task(enviar_monitoramento())

    while True:
        try:
            resultado, resultado_id, player_score, banker_score = await fetch_resultado()
            if not resultado or not resultado_id:
                await asyncio.sleep(1)
                continue

            if ultimo_resultado_id is None or resultado_id != ultimo_resultado_id:
                ultimo_resultado_id = resultado_id
                historico.append(resultado)
                historico = historico[-30:]  # Mantém os últimos 30 resultados
                logging.info(f"Histórico atualizado: {historico} (ID: {resultado_id})")

                rodadas_desde_erro += 1
                await enviar_resultado(resultado, player_score, banker_score, resultado_id)

                if not detecao_pausada:
                    logging.debug(f"Detecção de padrões ativa. Histórico: {historico}")
                    padroes_ordenados = sorted(PADROES, key=lambda x: len(x["sequencia"]), reverse=True)
                    for padrao in padroes_ordenados:
                        seq = padrao["sequencia"]
                        logging.debug(f"Verificando padrão ID {padrao['id']}: Sequência {seq}")
                        if (len(historico) >= len(seq) and 
                            historico[-len(seq):] == seq and 
                            padrao["id"] != ultimo_padrao_id and 
                            verificar_tendencia(historico, padrao["sinal"]) and
                            not any(sinal["padrao_id"] == padrao["id"] for sinal in sinais_ativos) and
                            sinais_por_hora < 25):
                            logging.debug(f"Padrão ID {padrao['id']} detectado! Enviando sinal.")
                            await enviar_sinal(sinal=padrao["sinal"], padrao_id=padrao["id"], resultado_id=resultado_id, sequencia=seq)
                            ultimo_padrao_id = padrao["id"]
                            break
                        else:
                            logging.debug(f"Padrão ID {padrao['id']} não corresponde ou está bloqueado.")
                    else:
                        logging.debug("Nenhum padrão correspondente encontrado.")

                if len(historico) >= 5:
                    ultimo_padrao_id = None

            else:
                logging.debug(f"Resultado repetido ignorado: ID {resultado_id}")

            await asyncio.sleep(1)
        except Exception as e:
            logging.error(f"Erro no loop principal: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot encerrado pelo usuário")
    except Exception as e:
        logging.error(f"Erro fatal no bot: {e}")
