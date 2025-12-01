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

# ============================================================================
# 4. ALGORITMO QUICK FIT (ADAPTADO PARA CARGA ATIVA)
# ============================================================================

class QuickFitAllocator:
    """Alocador Quick Fit adaptado para balanceamento dinâmico de carga.

    Implementa uma variação do algoritmo Quick Fit clássico de alocação de
    memória, adaptado para distribuição de tarefas entre servidores com
    capacidades heterogêneas e cargas variáveis em tempo real.
    
    Algoritmo de Seleção (Least Connections com Desempates):
        1. Filtra servidores disponíveis (carga < capacidade).
        2. Ordena por critérios:
            a) Menor carga ativa (minimiza congestionamento).
            b) Maior capacidade (desempate, favorece servidores potentes).
            c) Menor ID (desempate determinístico).
        3. Retorna o servidor ideal ou None se todos saturados.
        
    Diferenças do Quick Fit Tradicional:
        - Original: alocação de blocos de memória de tamanhos fixos.
        - Adaptado: distribuição de tarefas considerando capacidade dinâmica.
        
    Attributes:
        servidores (list): Lista de dicionários com metadados dos servidores.
                          Cada item deve conter 'id' e 'capacidade'.
    """

    def __init__(self, servidores):
        """Inicializa alocador com configuração de servidores.
        
        Args:
            servidores (list): Lista de servidores com schema:
                [{"id": int, "capacidade": int}, ...]
        """
        # Armazena referência imutável à configuração de servidores
        self.servidores = servidores

    def calcular_estado_servidores(self, task_queues, current_loads):
        """Gera snapshot consistente do estado de todos os servidores.

        Combina dados de configuração estática (capacidade) com métricas
        dinâmicas (carga ativa) para determinar disponibilidade em tempo real.
        
        Args:
            task_queues (dict): Mapa {server_id: Queue} com filas de tarefas pendentes.
            current_loads (dict): Mapa {server_id: int} com contadores de tarefas
                                 em execução no momento (snapshot thread-safe).
                                 
        Returns:
            dict: Mapa {server_id: estado} onde cada estado contém:
                - 'id' (int): Identificador do servidor.
                - 'capacidade' (int): Máximo de tarefas paralelas suportadas.
                - 'carga_atual' (int): Tarefas atualmente em execução.
                - 'pode_aceitar' (bool): True se carga < capacidade.
                
        Note:
            - Usa `current_loads` (contador controlado) ao invés de `qsize()`
              (apenas tarefas enfileiradas) para refletir carga real.
            - Snapshot é instantâneo mas pode desatualizar; lock externo
              garante consistência se necessário.
        """
        estado = {}
        for server_id, queue_obj in task_queues.items():
            # Obtém capacidade teórica da configuração
            capacidade = self._get_capacidade(server_id)
            
            # Carga real: tarefas em execução (não apenas na fila)
            # Fonte confiável: contador mantido pelo Scheduler com locks
            carga_ativa = current_loads.get(server_id, 0)
            
            estado[server_id] = {
                'id': server_id,
                'capacidade': capacidade,
                'carga_atual': carga_ativa,
                # Só aceita se carga ativa for menor que capacidade
                'pode_aceitar': carga_ativa < capacidade
            }
        return estado

    def _get_capacidade(self, server_id):
        """Consulta capacidade configurada de um servidor pelo ID.
        
        Args:
            server_id (int): Identificador do servidor.
            
        Returns:
            int: Capacidade (número máximo de tarefas paralelas) ou 1 se
                 servidor não encontrado (fallback seguro).
                 
        Complexity:
            O(n) onde n = número de servidores. Aceitável para clusters pequenos.
            Para escala maior, considerar dicionário indexado por ID.
        """
        for srv in self.servidores:
            if srv["id"] == server_id:
                return srv["capacidade"]
        # Fallback: assume capacidade unitária se servidor não encontrado
        return 1

    def encontrar_melhor_servidor(self, requisicao, estado_servidores):
        """Seleciona servidor ideal usando heurística Least Connections.

        Algoritmo de seleção otimizado para minimizar congestionamento e
        aproveitar servidores de maior capacidade. Critérios aplicados
        sequencialmente para desempate determinístico.
        
        Args:
            requisicao (dict): Metadados da tarefa a ser alocada (não usado
                              na versão atual, reservado para extensões).
            estado_servidores (dict): Snapshot de estados retornado por
                                     calcular_estado_servidores().
                                     
        Returns:
            int: ID do servidor selecionado, ou
            None: Se todos os servidores estão saturados (carga ≥ capacidade).
            
        Critérios de Seleção (em ordem de prioridade):
            1. **Menor carga atual**: Distribui uniformemente, evita hot spots.
            2. **Maior capacidade**: Em caso de empate, favorece servidores
               mais potentes (mais espaço para crescimento).
            3. **Menor ID**: Desempate final determinístico para reprodutibilidade.
            
        Complexity:
            O(n) onde n = número de servidores disponíveis.
            
        Note:
            Parâmetro `requisicao` permite extensões futuras com seleção
            baseada em características da tarefa (ex: tipo, prioridade).
        """
        # Fase 1: Filtra servidores que têm capacidade disponível
        candidatos = [e for e in estado_servidores.values() if e['pode_aceitar']]
        
        # Se todos saturados, sinaliza impossibilidade de alocar
        if not candidatos:
            return None

        # Fase 2: Aplica critérios de seleção com tupla ordenada
        # Python compara tuplas lexicograficamente (elemento por elemento)
        melhor = min(
            candidatos,
            key=lambda est: (
                est['carga_atual'],      # 1º: menor carga (menos congestionado)
                -est['capacidade'],      # 2º: maior capacidade (nega para ordem decrescente)
                est['id']                # 3º: menor ID (desempate determinístico)
            )
        )
        return melhor['id']
