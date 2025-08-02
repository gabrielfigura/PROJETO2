import asyncio
import aiohttp
import logging
import os
from telegram import Bot
from telegram.error import TelegramError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from collections import Counter
from datetime import datetime

# Configurações do Bot
BOT_TOKEN = os.getenv("BOT_TOKEN", "7758723414:AAF-Zq1QPoGy2IS-iK2Wh28PfexP0_mmHHc")
CHAT_ID = os.getenv("CHAT_ID", "-1002506692600")
API_URL = "https://api.casinoscores.com/svc-evolution-game-events/api/bacbo/latest"

# Inicializar o bot
bot = Bot(token=BOT_TOKEN)

# Configuração de logging
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s")

# Histórico e estado
historico = []
ultimo_resultado_id = None
ultima_mensagem_alerta = None  # Rastrear ID da mensagem de alerta
previsao_atual = None  # Armazena a previsão ativa
ultima_atualizacao = None  # Para rastrear repetição da API

# Mapeamento de outcomes para emojis
OUTCOME_MAP = {
    "PlayerWon": "🔵",
    "BankerWon": "🔴",
    "Tie": "🟡"
}

# Placar
placar = {"✅": 0, "❌": 0}

async def enviar_mensagem_inicial():
    """Envia uma mensagem de inicialização para confirmar que o bot está ativo."""
    try:
        mensagem = "🤖 Bot iniciado com sucesso às " + datetime.now().strftime('%H:%M:%S') + " WAT!"
        await bot.send_message(chat_id=CHAT_ID, text=mensagem)
        logging.info(f"Mensagem de inicialização enviada: {mensagem}")
    except TelegramError as e:
        logging.error(f"Erro ao enviar mensagem de inicialização: {e}")

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=30), retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)))
async def fetch_resultado():
    """Busca o resultado mais recente da API do cassinoscore."""
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(API_URL, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status != 200:
                    logging.error(f"Erro na API: Status {response.status}, Resposta: {await response.text()}")
                    return None, None, None, None, None
                data = await response.json()
                logging.debug(f"Resposta da API: {data}")
                
                if 'data' not in data or 'result' not in data['data'] or 'outcome' not in data['data']['result']:
                    logging.error(f"Estrutura inválida na resposta: {data}")
                    return None, None, None, None, None
                if 'id' not in data:
                    logging.error(f"Chave 'id' não encontrada na resposta: {data}")
                    return None, None, None, None, None
                
                if data['data'].get('status') != 'Resolved':
                    logging.debug(f"Jogo não resolvido: Status {data['data'].get('status')}")
                    return None, None, None, None, None
                
                resultado_id = data['id']
                outcome = data['data']['result']['outcome']
                player_score = data['data']['result'].get('playerDice', {}).get('score', 0)
                banker_score = data['data']['result'].get('bankerDice', {}).get('score', 0)
                settled_at = datetime.strptime(data['data']['settledAt'], '%Y-%m-%dT%H:%M:%S.%fZ')
                
                if outcome not in OUTCOME_MAP:
                    logging.error(f"Outcome inválido: {outcome}")
                    return None, None, None, None, None
                resultado = OUTCOME_MAP[outcome]
                
                return resultado, resultado_id, player_score, banker_score, settled_at
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logging.error(f"Erro de conexão com a API: {e}")
            return None, None, None, None, None
        except ValueError as e:
            logging.error(f"Erro ao parsear JSON: {e}")
            return None, None, None, None, None
        except Exception as e:
            logging.error(f"Erro inesperado ao buscar resultado: {e}")
            return None, None, None, None, None

def prever_empate(historico, player_score, banker_score, hora_atual):
    """Preve o próximo empate com base no histórico e scores da API do cassinoscore."""
    if len(historico) < 10:
        return None, None, None, None
    
    # Contar frequência de empates e padrões recentes
    contagem = Counter(historico[-10:])
    total = contagem["🔴"] + contagem["🔵"] + contagem["🟡"]
    if total == 0:
        return None, None, None, None
    proporcao_empates = contagem["🟡"] / total
    
    # Verificar se as pontuações estão próximas (indicador de empate)
    diferenca_scores = abs(player_score - banker_score)
    if diferenca_scores <= 2 or proporcao_empates > 0.1:  # Ajustado para maior sensibilidade
        # Estimar tempo baseado na frequência (assumindo 2 segundos por resultado)
        resultados_restantes = max(3 - contagem["🟡"], 1)  # Reduzido para 3 a 6 resultados
        segundos_restantes = resultados_restantes * 2
        
        # Hora prevista a partir da hora atual
        hora_prevista = hora_atual + timedelta(seconds=segundos_restantes)
        
        # Cor anterior (último resultado antes do previsto)
        cor_anterior = historico[-1] if historico[-1] in ["🔴", "🔵"] else (historico[-2] if len(historico) > 1 and historico[-2] in ["🔴", "🔵"] else None)
        
        logging.debug(f"Previsão: Empate em {hora_prevista.strftime('%H:%M:%S')} após {cor_anterior}, Diferença scores: {diferenca_scores}, Proporção empates: {proporcao_empates:.2%}")
        return hora_prevista.strftime('%H:%M:%S'), cor_anterior, segundos_restantes, ultimo_resultado_id
    return None, None, None, None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(TelegramError))
async def enviar_previsao(hora_prevista, cor_anterior, segundos_restantes, resultado_id_base):
    """Envia a previsão de empate ao Telegram e armazena a previsão ativa."""
    global ultima_mensagem_alerta, previsao_atual
    try:
        # Apagar a última mensagem de previsão, se existir
        if ultima_mensagem_alerta:
            try:
                await bot.delete_message(chat_id=CHAT_ID, message_id=ultima_mensagem_alerta)
                logging.debug("Mensagem de previsão anterior apagada")
            except TelegramError as e:
                logging.debug(f"Erro ao apagar mensagem de previsão: {e}")
            ultima_mensagem_alerta = None

        mensagem = f"🎯 PREVISÃO DE EMPATE\nHorário previsto: {hora_prevista}\nApós: {cor_anterior if cor_anterior else 'desconhecido'}\nTempo restante: {segundos_restantes} segundos"
        message = await bot.send_message(chat_id=CHAT_ID, text=mensagem)
        ultima_mensagem_alerta = message.message_id
        logging.info(f"Previsão enviada: {mensagem}")
        
        # Armazenar previsão ativa
        previsao_atual = {
            "hora_prevista": datetime.strptime(hora_prevista, '%H:%M:%S'),
            "cor_anterior": cor_anterior,
            "segundos_restantes": segundos_restantes,
            "resultado_id_base": resultado_id_base
        }
    except TelegramError as e:
        logging.error(f"Erro ao enviar previsão: {e}")

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(TelegramError))
async def enviar_alerta():
    """Envia um alerta contínuo a cada 15 segundos."""
    global ultima_mensagem_alerta
    try:
        # Apagar a última mensagem de alerta, se existir
        if ultima_mensagem_alerta:
            try:
                await bot.delete_message(chat_id=CHAT_ID, message_id=ultima_mensagem_alerta)
                logging.debug("Mensagem de alerta anterior apagada")
            except TelegramError as e:
                logging.debug(f"Erro ao apagar mensagem de alerta: {e}")
            ultima_mensagem_alerta = None

        mensagem = "PREVENDO UM EMPATE🤌"
        message = await bot.send_message(chat_id=CHAT_ID, text=mensagem)
        ultima_mensagem_alerta = message.message_id
        logging.info(f"Alerta enviado: {mensagem}")
    except TelegramError as e:
        logging.error(f"Erro ao enviar alerta: {e}")

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(TelegramError))
async def enviar_validacao(resultado, player_score, banker_score, resultado_id, hora_atual):
    """Envia a validação da previsão ao Telegram."""
    global ultima_mensagem_alerta, previsao_atual, placar
    try:
        if previsao_atual and resultado_id > previsao_atual["resultado_id_base"]:
            hora_real = hora_atual + timedelta(seconds=(resultado_id - previsao_atual["resultado_id_base"]) * 2)
            diferenca_tempo = abs((hora_real - previsao_atual["hora_prevista"]).total_seconds())
            
            if resultado == "🟡" and diferenca_tempo <= 5:  # Tolerância de 5 segundos
                cor_anterior_real = historico[-2] if historico[-2] in ["🔴", "🔵"] else None
                if cor_anterior_real == previsao_atual["cor_anterior"]:
                    placar["✅"] += 1
                    mensagem = f"✅ ACERTO DE EMPATE\nHorário real: {hora_real.strftime('%H:%M:%S')}\nPontuação: {player_score}:{banker_score}\nPlacar: {placar['✅']}✅ {placar['❌']}❌"
                else:
                    placar["❌"] += 1
                    mensagem = f"❌ ERRO DE EMPATE\nHorário real: {hora_real.strftime('%H:%M:%S')}\nPontuação: {player_score}:{banker_score}\nCor prevista: {previsao_atual['cor_anterior']}, Cor real: {cor_anterior_real}\nPlacar: {placar['✅']}✅ {placar['❌']}❌"
            else:
                placar["❌"] += 1
                mensagem = f"❌ ERRO DE EMPATE\nHorário real: {hora_real.strftime('%H:%M:%S')}\nResultado: {resultado}\nPontuação: {player_score}:{banker_score}\nPlacar: {placar['✅']}✅ {placar['❌']}❌"
            
            await bot.send_message(chat_id=CHAT_ID, text=mensagem)
            logging.info(f"Validação enviada: {mensagem}")
            previsao_atual = None  # Limpar previsão após validação
    except TelegramError as e:
        logging.error(f"Erro ao enviar validação: {e}")

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(TelegramError))
async def enviar_monitoramento():
    """Envia alerta contínuo a cada 15 segundos, apagando o anterior."""
    while True:
        try:
            await enviar_alerta()  # Chama o alerta continuamente
        except TelegramError as e:
            logging.error(f"Erro ao enviar monitoramento: {e}")
        await asyncio.sleep(15)

async def main():
    """Loop principal do bot com reconexão."""
    global historico, ultimo_resultado_id, ultima_atualizacao
    logging.info("Iniciando o bot...")
    await enviar_mensagem_inicial()  # Envia mensagem de inicialização
    asyncio.create_task(enviar_monitoramento())

    while True:
        try:
            resultado, resultado_id, player_score, banker_score, settled_at = await fetch_resultado()
            if not resultado or not resultado_id:
                await asyncio.sleep(2)
                continue

            # Verificar se os dados são novos ou estagnados
            if ultimo_resultado_id is None or resultado_id != ultimo_resultado_id:
                ultimo_resultado_id = resultado_id
                historico.append(resultado)
                historico = historico[-25:]  # Mantém os últimos 25 resultados
                ultima_atualizacao = settled_at
                logging.info(f"Histórico atualizado: {historico} (ID: {resultado_id}, Settled at: {settled_at})")
            elif ultima_atualizacao and (datetime.utcnow() - ultima_atualizacao).total_seconds() > 30:
                logging.warning("Dados da API estagnados por mais de 30 segundos, forçando nova verificação")
                ultima_atualizacao = settled_at

            # Validar previsão com o resultado atual
            await enviar_validacao(resultado, player_score, banker_score, resultado_id, settled_at)

            # Prever o próximo empate usando a API do cassinoscore
            if not previsao_atual:  # Só prever se não houver previsão ativa
                hora_prevista, cor_anterior, segundos_restantes, resultado_id_base = prever_empate(historico, player_score, banker_score, settled_at)
                if hora_prevista and cor_anterior is not None:
                    await enviar_previsao(hora_prevista, cor_anterior, segundos_restantes, resultado_id_base)

            await asyncio.sleep(2)
        except Exception as e:
            logging.error(f"Erro no loop principal: {e}")
            await asyncio.sleep(10)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot encerrado pelo usuário")
    except Exception as e:
        logging.error(f"Erro fatal no bot: {e}")
