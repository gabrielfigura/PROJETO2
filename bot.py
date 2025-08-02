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
        
        # Cor anterior (
