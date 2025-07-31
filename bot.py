import asyncio
import aiohttp
import logging
import os
from telegram import Bot
from telegram.error import TelegramError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from collections import Counter
from datetime import datetime, timedelta

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
sinais_por_hora = []
ultima_hora = datetime.now()

# Mapeamento de outcomes para emojis
OUTCOME_MAP = {
    "PlayerWon": "🔵",
    "BankerWon": "🔴",
    "Tie": "🟡"
}

# Padrões fortes (expandidos para aumentar oportunidades de sinais)
PADROES = [
    {"id": 1, "sequencia": ["🔴", "🔴", "🔴", "🔴", "🔴"], "sinal": "🔴", "prob_base": 0.85},
    {"id": 2, "sequencia": ["🔵", "🔵", "🔵", "🔵", "🔵"], "sinal": "🔵", "prob_base": 0.85},
    {"id": 3, "sequencia": ["🔴", "🔴", "🔴", "🔴", "🔵"], "sinal": "🔵", "prob_base": 0.80},
    {"id": 4, "sequencia": ["🔵", "🔵", "🔵", "🔵", "🔴"], "sinal": "🔴", "prob_base": 0.80},
    {"id": 5, "sequencia": ["🔴", "🔵", "🔴", "🔵", "🔴"], "sinal": "🔵", "prob_base": 0.75},
    {"id": 6, "sequencia": ["🔵", "🔴", "🔵", "🔴", "🔵"], "sinal": "🔴", "prob_base": 0.75},
    {"id": 7, "sequencia": ["🔵", "🔵", "🔴", "🔴", "🔵"], "sinal": "🔵", "prob_base": 0.78},
    {"id": 8, "sequencia": ["🔴", "🔴", "🔵", "🔵", "🔴"], "sinal": "🔴", "prob_base": 0.78},
    {"id": 9, "sequencia": ["🔵", "🔵", "🔵", "🔴", "🔵", "🔵"], "sinal": "🔵", "prob_base": 0.82},
    {"id": 10, "sequencia": ["🔴", "🔴", "🔴", "🔵", "🔴", "🔴"], "sinal": "🔴", "prob_base": 0.82},
    {"id": 11, "sequencia": ["🔵", "🔵", "🔴", "🔵", "🔵"], "sinal": "🔴", "prob_base": 0.77},
    {"id": 12, "sequencia": ["🔴", "🔴", "🔵", "🔴", "🔴"], "sinal": "🔵", "prob_base": 0.77},
    {"id": 13, "sequencia": ["🔵", "🔵", "🔵", "🔴", "🔴", "🔵", "🔵"], "sinal": "🔴", "prob_base": 0.80},
    {"id": 14, "sequencia": ["🔴", "🔴", "🔴", "🔵", "🔵", "🔴", "🔴"], "sinal": "🔵", "prob_base": 0.80},
    {"id": 15, "sequencia": ["🔵", "🔴", "🔵", "🔵", "🔴"], "sinal": "🔵", "prob_base": 0.76},
    {"id": 16, "sequencia": ["🔴", "🔵", "🔴", "🔴", "🔵"], "sinal": "🔴", "prob_base": 0.76},
    {"id": 17, "sequencia": ["🔵", "🔴", "🔴", "🔵", "🔵"], "sinal": "🔴", "prob_base": 0.77},
    {"id": 18, "sequencia": ["🔴", "🔵", "🔵", "🔴", "🔴"], "sinal": "🔵", "prob_base": 0.77},
    {"id": 19, "sequencia": ["🔵", "🔵", "🔴", "🔴", "🔴", "🔵"], "sinal": "🔵", "prob_base": 0.79},
    {"id": 20, "sequencia": ["🔴", "🔴", "🔵", "🔵", "🔵", "🔴"], "sinal": "🔴", "prob_base": 0.79},
]

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=30), retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)))
async def fetch_resultado():
    """Busca o resultado mais recente da API."""
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(API_URL, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status != 200:
                    logging.error(f"Erro na API: Status {response.status}")
                    return None, None, None, None
                data = await response.json()
                if 'data' not in data or 'result' not in data['data'] or 'outcome' not in data['data']['result'] or 'id' not in data:
                    logging.error(f"Estrutura inválida na resposta: {data}")
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
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            logging.error(f"Erro ao buscar resultado: {e}")
            return None, None, None, None

def calcular_probabilidade_sinal(historico, sinal, sequencia, prob_base, tamanho_janela=10):
    """Calcula a probabilidade de acerto do sinal com base no histórico (sem numpy)."""
    if len(historico) < tamanho_janela:
        return prob_base
    janela = historico[-tamanho_janela:]
    contagem = Counter(janela)
    total = contagem["🔴"] + contagem["🔵"]
    if total == 0:
        return prob_base
    
    # Proporção do sinal na janela
    proporcao = contagem[sinal] / total
    logging.debug(f"Proporção de {sinal}: {proporcao:.2%} em janela de {tamanho_janela}")
    
    # Aproximação de entropia usando variância
    proporcoes = [contagem["🔴"]/total, contagem["🔵"]/total] if total > 0 else [0.5, 0.5]
    variancia = sum((p - 0.5) ** 2 for p in proporcoes) / 2
    fator_confianca = 1 - variancia
    
    # Ajustar probabilidade
    prob_ajustada = prob_base * (0.5 + 0.5 * proporcao) * fator_confianca
    logging.debug(f"Probabilidade ajustada: {prob_ajustada:.2%} (Variância: {variancia:.2f})")
    
    return min(prob_ajustada, 0.95)

def verificar_tendencia(historico, sinal, tamanho_janela=10):
    """Verifica se o sinal está alinhado com a tendência dos últimos resultados."""
    if len(historico) < tamanho_janela:
        return True
    janela = historico[-tamanho_janela:]
    contagem = Counter(janela)
    total = contagem["🔴"] + contagem["🔵"]
    if total == 0:
        return True
    proporcao = contagem[sinal] / total
    logging.debug(f"Tendência: {sinal} aparece {contagem[sinal]}/{total} ({proporcao:.2%})")
    return proporcao >= 0.75

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(TelegramError))
async def enviar_sinal(sinal, padrao_id, resultado_id, sequencia, probabilidade):
    """Envia uma mensagem de sinal ao Telegram."""
    global ultima_mensagem_monitoramento, sinais_por_hora
    try:
        # Verificar limite de sinais por hora
        agora = datetime.now()
        sinais_por_hora = [t for t in sinais_por_hora if agora - t < timedelta(hours=1)]
        if len(sinais_por_hora) >= 20:
            logging.debug("Limite de 20 sinais por hora atingido, pausando envio.")
            return
        
        # Apagar mensagem de monitoramento
        if ultima_mensagem_monitoramento:
            try:
                await bot.delete_message(chat_id=CHAT_ID, message_id=ultima_mensagem_monitoramento)
            except TelegramError as e:
                logging.debug(f"Erro ao apagar mensagem de monitoramento: {e}")
            ultima_mensagem_monitoramento = None

        # Evitar sinais duplicados
        if any(sinal_ativo["padrao_id"] == padrao_id for sinal_ativo in sinais_ativos):
            logging.debug(f"Sinal com Padrão ID {padrao_id} já ativo, ignorando.")
            return

        sequencia_str = " ".join(sequencia)
        mensagem = f"""🎯 SINAL ENCONTRADO
Padrão ID: {padrao_id}
Sequência: {sequencia_str}
Entrar: {sinal}
Confiança: {probabilidade:.1%} 🔒
Proteger o empate🟡
⏳ Aposte agora!"""
        message = await bot.send_message(chat_id=CHAT_ID, text=mensagem)
        sinais_ativos.append({
            "sinal": sinal,
            "padrao_id": padrao_id,
            "resultado_id": resultado_id,
            "sequencia": sequencia,
            "enviado_em": asyncio.get_event_loop().time(),
            "gale_nivel": 0,
            "gale_message_id": None
        })
        sinais_por_hora.append(agora)
        logging.info(f"Sinal enviado: Padrão {padrao_id}, Sinal: {sinal}, Prob: {probabilidade:.1%}")
        return message.message_id
    except TelegramError as e:
        logging.error(f"Erro ao enviar sinal: {e}")
        raise

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(TelegramError))
async def enviar_resultado(resultado, player_score, banker_score, resultado_id):
    """Valida sinais ativos e envia resultados."""
    global rodadas_desde_erro, detecao_pausada, placar, sinais_por_hora
    try:
        for sinal_ativo in sinais_ativos[:]:
            if sinal_ativo["resultado_id"] != resultado_id:
                resultado_texto = f"🎲 Resultado: {'EMPATE' if resultado == '🟡' else 'AZUL' if resultado == '🔵' else 'VERMELHO'}: {player_score}:{banker_score}"
                sequencia_str = " ".join(sinal_ativo["sequencia"])
                
                if resultado == sinal_ativo["sinal"] or resultado == "🟡":
                    placar["✅"] += 1
                    if sinal_ativo["gale_message_id"]:
                        try:
                            await bot.delete_message(chat_id=CHAT_ID, message_id=sinal_ativo["gale_message_id"])
                        except TelegramError as e:
                            logging.debug(f"Erro ao apagar mensagem de gale: {e}")
                    mensagem_validacao = f"🤑 ENTROU DINHEIRO 🤑\n{resultado_texto}\n📊 Sinal (Padrão {sinal_ativo['padrao_id']}): {sequencia_str}\nPlacar: {placar['✅']}✅ {placar['❌']}❌"
                    await bot.send_message(chat_id=CHAT_ID, text=mensagem_validacao)
                    sinais_ativos.remove(sinal_ativo)
                    detecao_pausada = False
                    rodadas_desde_erro = 0
                else:
                    placar["❌"] += 1
                    if sinal_ativo["gale_nivel"] == 0:
                        detecao_pausada = True
                        mensagem_gale = "BORA GANHAR NO 1º GALE 🎯"
                        message = await bot.send_message(chat_id=CHAT_ID, text=mensagem_gale)
                        sinal_ativo["gale_nivel"] = 1
                        sinal_ativo["gale_message_id"] = message.message_id
                        sinal_ativo["resultado_id"] = resultado_id
                        rodadas_desde_erro = 0
                    else:
                        if sinal_ativo["gale_message_id"]:
                            try:
                                await bot.delete_message(chat_id=CHAT_ID, message_id=sinal_ativo["gale_message_id"])
                            except TelegramError as e:
                                logging.debug(f"Erro ao apagar mensagem de gale: {e}")
                        mensagem_erro = f"NÃO FOI DESSA 🤧\n{resultado_texto}\n📊 Sinal (Padrão {sinal_ativo['padrao_id']}): {sequencia_str}\nPlacar: {placar['✅']}✅ {placar['❌']}❌"
                        await bot.send_message(chat_id=CHAT_ID, text=mensagem_erro)
                        sinais_ativos.remove(sinal_ativo)
                        detecao_pausada = True
                        rodadas_desde_erro = 0
                        if placar["❌"] >= 2:
                            placar["✅"] = 0
                            placar["❌"] = 0
                            await bot.send_message(chat_id=CHAT_ID, text="🔄 Placar zerado após 2 perdas.")
                
                ultima_mensagem_monitoramento = None
            elif asyncio.get_event_loop().time() - sinal_ativo["enviado_em"] > 300:
                if sinal_ativo["gale_message_id"]:
                    try:
                        await bot.delete_message(chat_id=CHAT_ID, message_id=sinal_ativo["gale_message_id"])
                    except TelegramError as e:
                        logging.debug(f"Erro ao apagar mensagem de gale obsoleta: {e}")
                sinais_ativos.remove(sinal_ativo)
                detecao_pausada = False
    except TelegramError as e:
        logging.error(f"Erro ao enviar resultado: {e}")

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(TelegramError))
async def enviar_monitoramento():
    """Envia mensagem de monitoramento a cada 15 segundos."""
    global ultima_mensagem_monitoramento
    while True:
        try:
            if not sinais_ativos and not detecao_pausada:
                if ultima_mensagem_monitoramento:
                    try:
                        await bot.delete_message(chat_id=CHAT_ID, message_id=ultima_mensagem_monitoramento)
                    except TelegramError as e:
                        logging.debug(f"Erro ao apagar mensagem de monitoramento: {e}")
                message = await bot.send_message(chat_id=CHAT_ID, text="MONITORANDO A MESA…🤌")
                ultima_mensagem_monitoramento = message.message_id
                logging.debug(f"Monitoramento enviado: ID {ultima_mensagem_monitoramento}")
        except TelegramError as e:
            logging.error(f"Erro ao enviar monitoramento: {e}")
        await asyncio.sleep(15)

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(TelegramError))
async def enviar_relatorio():
    """Envia relatório periódico com placar."""
    global sinais_por_hora
    while True:
        try:
            agora = datetime.now()
            sinais_por_hora = [t for t in sinais_por_hora if agora - t < timedelta(hours=1)]
            msg = f"📈 Relatório: Bot em operação\nPlacar: {placar['✅']}✅ {placar['❌']}❌\nSinais na última hora: {len(sinais_por_hora)}"
            await bot.send_message(chat_id=CHAT_ID, text=msg)
            logging.info(f"Relatório enviado: {msg}")
        except TelegramError as e:
            logging.error(f"Erro ao enviar relatório: {e}")
        await asyncio.sleep(3600)

async def main():
    """Loop principal do bot."""
    global historico, ultimo_padrao_id, ultimo_resultado_id, rodadas_desde_erro, detecao_pausada
    asyncio.create_task(enviar_relatorio())
    asyncio.create_task(enviar_monitoramento())

    while True:
        try:
            resultado, resultado_id, player_score, banker_score = await fetch_resultado()
            if not resultado or not resultado_id:
                await asyncio.sleep(2)
                continue

            if ultimo_resultado_id is None or resultado_id != ultimo_resultado_id:
                ultimo_resultado_id = resultado_id
                historico.append(resultado)
                historico = historico[-30:]  # Aumentado para 30 resultados
                logging.info(f"Histórico atualizado: {historico} (ID: {resultado_id})")
                rodadas_desde_erro += 1

                # Validar sinais ativos
                await enviar_resultado(resultado, player_score, banker_score, resultado_id)

                # Detectar padrões e enviar sinais
                if not detecao_pausada or rodadas_desde_erro >= 2:
                    detecao_pausada = False
                    padroes_ordenados = sorted(PADROES, key=lambda x: x["prob_base"], reverse=True)
                    for padrao in padroes_ordenados:
                        seq = padrao["sequencia"]
                        probabilidade = calcular_probabilidade_sinal(historico, padrao["sinal"], seq, padrao["prob_base"])
                        if (len(historico) >= len(seq) and 
                            historico[-len(seq):] == seq and 
                            padrao["id"] != ultimo_padrao_id and 
                            probabilidade >= 0.80 and 
                            verificar_tendencia(historico, padrao["sinal"]) and
                            not any(sinal["padrao_id"] == padrao["id"] for sinal in sinais_ativos)):
                            await enviar_sinal(
                                sinal=padrao["sinal"],
                                padrao_id=padrao["id"],
                                resultado_id=resultado_id,
                                sequencia=seq,
                                probabilidade=probabilidade
                            )
                            ultimo_padrao_id = padrao["id"]
                            break
                    else:
                        logging.debug("Nenhum padrão correspondente encontrado.")

                if len(historico) >= 5:
                    ultimo_padrao_id = None

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
