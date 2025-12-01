import json
from pathlib import Path
import threading
import time

# ============================================================================
# 1. UTILITÁRIOS DE REDE
# ============================================================================

def send_json(sock, data):
    """Envia um dicionário Python como mensagem JSON via socket TCP.

    Protocolo de comunicação:
        Cada mensagem é serializada em JSON e finalizada com uma quebra de
        linha ('\n') para delimitação no stream TCP. Isso permite ao receptor
        identificar o fim de cada mensagem de forma simples e eficiente.
        
    Args:
        sock: Socket TCP conectado (socket.socket).
        data: Dicionário Python a ser enviado. Deve ser serializável em JSON.
        
    Raises:
        Exception: Capturada silenciosamente. Erros de pipe quebrado (EPIPE)
                   são comuns durante o shutdown e não necessitam tratamento.
                   
    Note:
        - Usa UTF-8 para compatibilidade universal.
        - sendall() garante envio completo da mensagem.
        - Failures silenciosos evitam poluir logs em situações normais de desconexão.
    """
    try:
        # Serializa dicionário para string JSON e adiciona delimitador
        msg = json.dumps(data) + "\n"
        # Envia todos os bytes, bloqueando até completar
        sock.sendall(msg.encode('utf-8'))
    except Exception as e:
        # Erros de pipe quebrado são comuns no shutdown coordenado
        # Silenciosamente ignorados para não poluir logs
        pass

def recv_json(sock):
    """Recebe e desserializa mensagem JSON do socket TCP.

    Implementa um receptor baseado em buffer que acumula bytes até encontrar
    o delimitador de mensagem ('\n'). Esse padrão é necessário porque TCP é
    orientado a stream e não garante que recv() retorne mensagens completas.
    
    Args:
        sock: Socket TCP conectado (socket.socket).
        
    Returns:
        dict: Dicionário Python desserializado da mensagem JSON, ou
        None: Se a conexão foi fechada ou ocorreu erro de parsing.
        
    Note:
        - Buffer acumula dados entre chamadas recv() parciais.
        - Tamanho do chunk (1024 bytes) balanceia latência vs overhead de syscalls.
        - Descarta dados após '\n' (rest) pois protocolo assume uma mensagem por linha.
        
    Behavior:
        - Chunk vazio: conexão fechada pelo peer → retorna None.
        - Exceção: erro de rede ou JSON inválido → retorna None.
    """
    buffer = ""  # Acumulador para dados parciais
    while True:
        try:
            # Recebe até 1KB de dados do socket
            chunk = sock.recv(1024).decode('utf-8')
            
            # Chunk vazio indica fechamento gracioso da conexão
            if not chunk:
                return None
            
            # Acumula no buffer para lidar com mensagens fragmentadas
            buffer += chunk
            
            # Procura delimitador de fim de mensagem
            if "\n" in buffer:
                # Extrai primeira mensagem completa
                msg, rest = buffer.split("\n", 1)
                # Desserializa JSON para dicionário Python
                return json.loads(msg)
                
        except Exception:
            # Falha de rede, timeout, ou JSON malformado
            return None

# ============================================================================
# 2. CONFIGURAÇÕES
# ============================================================================

BASE_DIR = Path(__file__).resolve().parent
TASKS_PATH = BASE_DIR / "tasks.json"

def get_port_for_server(server_id):
    """Gera porta TCP dinamicamente para qualquer ID de servidor.
    
    Args:
        server_id: Identificador numérico do servidor.
        
    Returns:
        int: Número da porta TCP (5001, 5002, 5003, ...).
        
    Note:
        Usa offset de 5000 para evitar portas privilegiadas (<1024)
        e conflitos com serviços comuns (ex: 3000, 8080).
    """
    return 5000 + server_id

# ============================================================================
# 3. LOGGER
# ============================================================================

class RealtimeLogger:
    """Logger thread-safe para eventos em tempo real e agregação de métricas.

    Esta classe centraliza o registro de eventos do sistema distribuído,
    fornecendo timestamps formatados, categorização de eventos e coleta
    de métricas estatísticas. Garante segurança em ambientes multithread
    usando locks explícitos.
    
    Responsabilidades:
        - Emitir eventos formatados com timestamps relativos ao início.
        - Acumular métricas: requisições completas, CPU, preempções.
        - Garantir consistência de dados com acesso concorrente.
        - Fornecer visualização humanizada de timestamps (mm:ss.mmm).
        
    Thread Safety:
        Todas as operações de escrita em `events` e `metrics` são protegidas
        por `self.lock` para prevenir race conditions.
        
    Attributes:
        events (list): Histórico de mensagens de log formatadas.
        start_time (float): Timestamp Unix do início da execução.
        metrics (dict): Agregação de métricas numéricas.
        lock (threading.Lock): Mecanismo de sincronização para acesso concorrente.
    """

    def __init__(self):
        """Inicializa logger com estruturas de dados vazias e timestamp de referência."""
        # Histórico sequencial de todas as mensagens de log
        self.events = []
        
        # Marco temporal para cálculo de timestamps relativos
        self.start_time = time.time()
        
        # Contadores e acumuladores de métricas
        self.metrics = {
            "requisicoes_completas": 0,      # Total de tarefas finalizadas
            "tempo_total_espera": 0,          # Acumulador para cálculo de média
            "max_tempo_espera": 0,            # Máximo tempo de resposta observado
            "cpu_valores": [],                # Série temporal de amostras de CPU
            "preempcoes": 0                   # Total de interrupções por quantum
        }
        
        # Lock para garantir atomicidade de operações em ambientes multithread
        self.lock = threading.Lock()

    def log(self, event_type, req_id=None, servidor_id=None, 
            prioridade=None, detalhes=""):
        """Registra evento categorizado com timestamp e contexto opcional.

        Formata mensagens de log de acordo com o tipo de evento, incluindo
        informações contextuais relevantes (IDs, prioridades, detalhes).
        Emite simultaneamente para console (stdout) e histórico interno.
        
        Args:
            event_type (str): Categoria do evento. Valores esperados:
                - "ATRIBUICAO": Tarefa enviada a um servidor.
                - "CONCLUSAO": Tarefa finalizada com sucesso.
                - "PREEMPCAO": Tarefa interrompida por quantum.
                - "INICIO": Início da execução do sistema.
                - "FIM": Término da execução.
                - Outros: Mensagem genérica.
            req_id (int, optional): Identificador da requisição.
            servidor_id (int, optional): ID do servidor processador.
            prioridade (str, optional): Nível de prioridade human-readable.
            detalhes (str, optional): Informações adicionais contextuais.
            
        Thread Safety:
            Método é thread-safe. Aquisição de lock antes de modificar `events`.
            
        Side Effects:
            - Imprime mensagem formatada no stdout.
            - Adiciona mensagem ao histórico interno (`self.events`).
        """
        # Calcula tempo decorrido desde o início
        elapsed = time.time() - self.start_time
        # Converte para formato humanizado (mm:ss.mmm)
        timestamp = self._format_time(elapsed)

        if event_type == "ATRIBUICAO":
            msg = f"[{timestamp}] 📤 Requisição {req_id} ({prioridade}) " \
                  f"-> Enviada via SOCKET p/ Servidor {servidor_id} | {detalhes}"
        elif event_type == "CONCLUSAO":
            msg = f"[{timestamp}] ✅ Servidor {servidor_id} " \
                  f"CONCLUIU Requisição {req_id}"
        elif event_type == "PREEMPCAO":
            msg = f"[{timestamp}] ⏸️  Requisição {req_id} " \
                  f"PREEMPTADA (Recebida via Socket) - Resta: {detalhes}s"
        elif event_type == "INICIO":
            msg = f"[{timestamp}] ========== INÍCIO DO SISTEMA DISTRIBUÍDO =========="
        elif event_type == "FIM":
            msg = f"[{timestamp}] ========== FIM DO PROCESSAMENTO =========="
        else:
            msg = f"[{timestamp}] {event_type}: {detalhes}"

        with self.lock:
            self.events.append(msg)
            print(msg)

    def _format_time(self, seconds):
        """Converte segundos decimais em timestamp humanizado.
        
        Args:
            seconds (float): Tempo em segundos (pode conter frações).
            
        Returns:
            str: Timestamp formatado como "MM:SS.mmm" onde:
                - MM: minutos (2 dígitos, zero-padded)
                - SS: segundos (2 dígitos, zero-padded)
                - mmm: milissegundos (3 dígitos, zero-padded)
                
        Example:
            >>> _format_time(125.456)
            "02:05.456"
        """
        minutes = int(seconds) // 60          # Divisão inteira para minutos
        secs = int(seconds) % 60              # Resto para segundos
        ms = int((seconds % 1) * 1000)        # Fração para milissegundos
        return f"{minutes:02d}:{secs:02d}.{ms:03d}"

    def add_metric(self, metric_name, value):
        """Incrementa ou acrescenta valor a uma métrica existente.
        
        Suporta dois tipos de agregação:
            - Listas: append do novo valor (séries temporais).
            - Numéricos: soma acumulativa (contadores).
            
        Args:
            metric_name (str): Nome da métrica em `self.metrics`.
            value: Valor a ser adicionado. Tipo deve ser compatível com
                   o tipo da métrica existente.
                   
        Thread Safety:
            Operação atômica garantida por aquisição de lock.
            
        Note:
            Ignora silenciosamente métricas inexistentes para evitar
            propagação de erros em paths de coleta.
        """
        with self.lock:
            # Verifica existência da métrica no dicionário
            if metric_name in self.metrics:
                # Séries temporais: acrescenta novo ponto
                if isinstance(self.metrics[metric_name], list):
                    self.metrics[metric_name].append(value)
                # Contadores: incrementa acumulador
                elif isinstance(self.metrics[metric_name], (int, float)):
                    self.metrics[metric_name] += value
