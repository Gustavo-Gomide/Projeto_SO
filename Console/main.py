"""
=================================================================
BSB COMPUTE — Sistema Distribuído (Sockets TCP/IP)
=================================================================

Visão geral
-----------
Este módulo implementa um sistema distribuído simplificado com três
componentes principais:

- Servidores de execução: processos independentes (via `multiprocessing`)
    que aceitam tarefas por TCP, processam em threads para paralelismo real
    e retornam resultados ao orquestrador.
- Orquestrador (cliente): coordena o envio das tarefas, recebe as
    respostas, aplica políticas de escalonamento (SJF, Round Robin, Prioridade)
    e faz o balanceamento com um alocador Quick Fit adaptado.
- Logger: gera logs em tempo real e coleta métricas básicas de execução.

Destaques de arquitetura
------------------------
- Comunicação full-duplex por sockets com protocolo simples baseado em
    mensagens JSON terminadas por `\n`.
- Paralelismo real nos servidores: cada tarefa recebida é processada em
    uma thread independente, permitindo múltiplas tarefas simultâneas.
- Controle de carga ativa: o orquestrador acompanha tarefas em execução
    por servidor e usa essa informação para decidir a alocação.
- Quick Fit (adaptado): seleciona o melhor servidor considerando menor
    carga ativa, maior capacidade como desempate e ID como critério final.

Estratégias suportadas
----------------------
- SJF: menor tempo de execução primeiro (sem quantum).
- Round Robin: com quantum configurável.
- Prioridade: ordena por prioridade (1=Alta, 2=Média, 3=Baixa).
- FIFO: Round Robin com quantum zero (execução contínua).
"""

import json
import time
import threading
import multiprocessing
import psutil
import socket
from pathlib import Path
import queue

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

# ============================================================================
# 5. WORKER (SERVIDOR SOCKET MULTITHREAD)
# ============================================================================

def fake_inference_core(requisicao, quantum):
    """Simula processamento de tarefa com consumo sintético de CPU.

    Implementa loop de trabalho que respeita semântica de quantum para
    escalonamento preemptivo. Combina sleep (I/O-bound) com cálculos
    (CPU-bound) para simular carga realista.
    
    Args:
        requisicao (dict): Metadados da tarefa contendo:
            - 'tempo_restante' (float): Tempo ainda não processado.
            - 'tempo_exec' (float): Tempo total original (fallback).
        quantum (float): Limite de tempo para esta fatia de execução.
                        Se 0, executa até completar (modo FIFO/SJF).
                        
    Returns:
        tuple: (tempo_restante_novo, tempo_executado) onde:
            - tempo_restante_novo (float): Saldo após esta execução.
            - tempo_executado (float): Duração efetiva desta fatia.
            
    Behavior:
        - Quantum 0: Executa tarefa completa (modo não-preemptivo).
        - Quantum > 0: Limita execução a min(quantum, tempo_restante).
        - Trabalho sintético: sleep + cálculos aritméticos (simula mix I/O+CPU).
        
    Note:
        - Chunks de 0.1s balanceiam responsividade vs overhead de iteração.
        - Cálculo de quadrados gera carga CPU mensurável para testes de desempenho.
    """
    # Obtém tempo pendente (prioriza 'tempo_restante' se disponível)
    tempo_restante = requisicao.get("tempo_restante", requisicao.get("tempo_exec"))
    
    # Determina duração desta fatia de execução
    if quantum == 0:
        # Modo não-preemptivo: executa até o fim
        tempo_execucao = tempo_restante
    else:
        # Modo preemptivo: respeita quantum ou termina se restar menos
        tempo_execucao = min(quantum, tempo_restante)

    # Marca início para medição precisa de tempo decorrido
    start = time.time()
    elapsed = 0
    
    # Loop de trabalho: simula processamento em chunks
    while elapsed < tempo_execucao:
        # Calcula duração do próximo chunk (máximo 0.1s)
        chunk = min(0.1, tempo_execucao - elapsed)
        
        # Simula I/O-bound: espera passiva (libera CPU)
        time.sleep(chunk)
        
        # Simula CPU-bound: cálculo sintético (consome ciclos)
        # Sum de quadrados gera carga mensurável sem otimização do interpretador
        _ = sum(i * i for i in range(1000))
        
        # Atualiza tempo decorrido (medição real, não sleep nominal)
        elapsed = time.time() - start

    # Calcula saldo restante (garante não-negativo por arredondamento)
    novo_tempo_restante = max(0, tempo_restante - tempo_execucao)
    return novo_tempo_restante, tempo_execucao

def process_task_thread(conn, lock, requisicao, quantum, server_id):
    """Worker thread que processa uma tarefa e envia resposta ao orquestrador.

    Cada tarefa recebida pelo servidor é executada em uma thread independente,
    permitindo paralelismo real (múltiplas tarefas simultâneas por servidor).
    Após processamento, envia resposta via socket com resultado (CONCLUSAO
    ou PREEMPCAO).
    
    Args:
        conn (socket.socket): Conexão TCP estabelecida com o orquestrador.
        lock (threading.Lock): Lock para serializar envios no socket compartilhado.
        requisicao (dict): Metadados da tarefa a processar.
        quantum (float): Limite de tempo por fatia (0 = sem limite).
        server_id (int): ID deste servidor (para logging/depuração).
        
    Side Effects:
        - Modifica `requisicao['tempo_restante']` in-place.
        - Envia JSON via socket (CONCLUSAO ou PREEMPCAO).
        
    Thread Safety:
        - Lock protege send_json() contra intercalação de mensagens.
        - `requisicao` é cópia independente (não compartilhada entre threads).
        
    Protocol:
        Envia um dos dois tipos de resposta:
        - CONCLUSAO: Tarefa finalizada (tempo_restante ≤ 0.05s).
        - PREEMPCAO: Quantum esgotado, tarefa deve retornar à fila.
    """
    # Extrai identificador para rastreabilidade
    req_id = requisicao.get("id")
    
    # Executa fatia de trabalho (respeita quantum)
    novo_restante, executado = fake_inference_core(requisicao, quantum)
    
    # Atualiza estado da requisição in-place (arredonda para evitar drift)
    requisicao['tempo_restante'] = round(novo_restante, 2)

    # Decide tipo de resposta baseado em critério de conclusão
    # Threshold de 0.05s tolera imprecisões de ponto flutuante
    if novo_restante <= 0.05:
        # Tarefa completa: envia resultado final
        resposta = {
            "tipo": "CONCLUSAO",
            "req_id": req_id,
            "servidor_id": server_id,
            "tempo_final": time.time(),       # Timestamp para métricas
            "dados_originais": requisicao      # Estado final para auditoria
        }
    else:
        # Quantum esgotado: sinaliza preempção
        resposta = {
            "tipo": "PREEMPCAO",
            "req_id": req_id,
            "servidor_id": server_id,
            "tempo_restante": novo_restante,  # Saldo para próxima fatia
            "dados_originais": requisicao      # Estado parcial preservado
        }

    # Envia resposta com proteção contra intercalação
    # Lock necessário pois múltiplas threads compartilham mesmo socket
    with lock:
        try:
            send_json(conn, resposta)
        except:
            # Falhas de envio (ex: conexão fechada) são ignoradas
            # Servidor não tem como reprocessar, responsabilidade do orquestrador
            pass

def run_socket_server(server_id, port, quantum):
    """Processo servidor independente que gerencia execução paralela de tarefas.

    Implementa servidor TCP multi-threaded que:
    1. Aceita conexões do orquestrador (permanentes durante sessão)
    2. Recebe tarefas via mensagens JSON delimitadas por '\n'
    3. Processa tarefas em threads paralelas (até capacidade do servidor)
    4. Envia respostas (CONCLUSAO/PREEMPCAO) de volta ao orquestrador
    5. Suporta shutdown gracioso via mensagem "STOP"
    
    Args:
        server_id (int): Identificador único deste servidor.
        port (int): Porta TCP para escutar (tipicamente 5000 + server_id).
        quantum (float): Limite de tempo por fatia de execução (0 = sem limite).
                        
    Lifecycle:
        1. Cria socket TCP em modo SO_REUSEADDR (permite restart rápido)
        2. Entra em loop accept() para aguardar conexões
        3. Para cada conexão: spawn thread por tarefa recebida
        4. Ao receber "STOP", encerra loops e fecha socket
        
    Concurrency:
        - Uma thread por tarefa ativa (paralelismo real limitado por GIL+I/O)
        - Lock por conexão protege envios no socket compartilhado
        - Threads são daemon=True (encerram com processo pai)
        
    Protocol:
        - Input: JSON por linha (ex: {"id": 101, "tempo_exec": 5, ...})
        - Output: JSON por linha (ex: {"tipo": "CONCLUSAO", "req_id": 101, ...})
        - Control: String "STOP" encerra servidor
        
    Note:
        - Roda como processo separado (multiprocessing) para isolamento real.
        - SO_REUSEADDR evita erro "Address already in use" em restarts rápidos.
        - Backlog de 5 conexões pendentes suficiente para 1 cliente.
    """
    # Cria socket TCP IPv4
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    
    # Permite reusar porta imediatamente após encerramento (evita TIME_WAIT)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    
    # Tenta ligar socket à porta especificada
    try:
        server_socket.bind(('0.0.0.0', port))
        # Habilita modo listening com backlog=5 (até 5 conexões pendentes)
        server_socket.listen(5)
    except Exception as e:
        # Falha crítica: porta ocupada ou sem permissões
        print(f"[Server {server_id}] Erro ao ligar na porta {port}: {e}")
        return

    # Loop principal: aguarda conexões indefinidamente
    while True:
        try:
            # Aceita próxima conexão (bloqueia até cliente conectar)
            conn, addr = server_socket.accept()
            
            # Lock exclusivo para esta conexão (protege send_json concorrente)
            socket_lock = threading.Lock()
            
            # Loop de recepção: processa mensagens até STOP ou desconexão
            while True:
                # Lê próxima mensagem JSON (bloqueia até receber linha completa)
                requisicao = recv_json(conn)
                
                # Verifica condições de término
                if not requisicao:           # Conexão fechada ou erro de leitura
                    break
                if requisicao == "STOP":     # Comando de shutdown
                    break

                # Mensagem normal: trata como tarefa a processar
                # ARQUITETURA PARALELA: Uma thread por tarefa
                # Permite múltiplas tarefas simultâneas no mesmo servidor
                t = threading.Thread(
                    target=process_task_thread,
                    args=(conn, socket_lock, requisicao, quantum, server_id),
                    daemon=True  # Thread morre com processo pai
                )
                t.start()
            
            # Cleanup: fecha conexão após loop interno
            conn.close()
            
            # Se recebeu STOP, encerra servidor completamente
            if requisicao == "STOP":
                break

        except Exception as e:
            # Erro de comunicação: loga silenciosamente e aguarda próxima conexão
            # Comentado para evitar poluir stdout com erros esperados (ex: RST)
            # print(f"Erro na conexão do server {server_id}: {e}")
            pass
            
    # Cleanup final: fecha socket de escuta ao sair do loop
    server_socket.close()

# ============================================================================
# 6. ESCALONADOR (CLIENTE)
# ============================================================================

class TaskScheduler:
    """Orquestrador central de tarefas com políticas de escalonamento distribuído.

    Coordena execução de tarefas em cluster de servidores TCP independentes,
    implementando estratégias clássicas de escalonamento de processos adaptadas
    para ambiente distribuído. Gerencia ciclo completo: carregamento, distribuição,
    monitoramento e coleta de resultados.
    
    Responsabilidades:
        - **Carregamento**: Lê configuração JSON com servidores e tarefas.
        - **Distribuição**: Aloca tarefas usando Quick Fit (Least Connections).
        - **Escalonamento**: Aplica SJF, Round Robin, Prioridade ou FIFO.
        - **Comunicação**: Mantém pontes full-duplex via sockets TCP.
        - **Monitoramento**: Rastreia carga ativa, tarefas concluídas, métricas.
        - **Relatório**: Calcula estatísticas de utilização e desempenho.
        
    Estratégias Suportadas:
        - **SJF**: Shortest Job First (quantum=0, não-preemptivo).
        - **ROUND_ROBIN**: Quantum fixo com preempção cíclica.
        - **PRIORIDADE**: Ordena por prioridade numérica (1=alta, 3=baixa).
        - **FIFO**: First In First Out, ordem de chegada.
        
    Arquitetura:
        - 1 processo orquestrador (este) + N processos servidores.
        - 1 thread de ponte (socket_bridge) por servidor.
        - 1 thread de monitoramento (metrics) compartilhada.
        - Comunicação via JSON sobre TCP com protocolo de mensagens.
    """

    def __init__(self, json_path, strategy="ROUND_ROBIN", quantum=2):
        """Inicializa orquestrador com configuração e estruturas de dados.
        
        Args:
            json_path (str|Path): Caminho para JSON com 'servidores' e 'requisicoes'.
            strategy (str): Estratégia de escalonamento. Valores aceitos:
                           "SJF", "ROUND_ROBIN", "PRIORIDADE", "FIFO".
            quantum (float): Fatia de tempo para Round Robin (em segundos).
                            Ignorado para SJF (sempre 0).
                            
        Attributes:
            json_path (str): Caminho do arquivo de configuração.
            strategy (str): Estratégia normalizada (uppercase).
            quantum (float): Quantum efetivo (0 para SJF).
            logger (RealtimeLogger): Sistema de logging thread-safe.
            servidores (list[dict]): Configuração dos servidores carregada.
            requisicoes (list[dict]): Lista de tarefas a processar.
            task_queues (dict): Mapa {server_id: Queue} com filas locais.
            lista_global_pendentes (list): Tarefas ainda não alocadas.
            server_load (dict): Mapa {server_id: int} com contadores de carga.
            load_lock (Lock): Proteção para server_load (modificações concorrentes).
            concluidas (set): IDs de tarefas já finalizadas.
            dados_concluidos (list): Metadados completos de tarefas concluídas.
            total_reqs (int): Número total de tarefas (para calcular progresso).
            quick_fit (QuickFitAllocator): Alocador de carga Least Connections.
            running (bool): Flag de controle para loops de orquestração.
            
        Note:
            - Quantum é forçado a 0 para SJF (semântica não-preemptiva).
            - task_queues criadas dinamicamente para N servidores.
            - lista_global_pendentes é cópia (modificações não afetam original).
        """
        # Configuração básica
        self.json_path = json_path
        self.strategy = strategy.upper()  # Normaliza para comparações
        
        # Quantum: 0 para SJF (não-preemptivo), valor fornecido para outros
        self.quantum = quantum if strategy != "SJF" else 0
        
        # Sistema de logging thread-safe
        self.logger = RealtimeLogger()
        
        # Carrega servidores e requisições do JSON
        self._load_config()

        # Estruturas de distribuição: uma fila por servidor
        # Filas são thread-safe nativamente (queue.Queue)
        self.task_queues = {srv["id"]: queue.Queue() for srv in self.servidores}
        
        # Lista global: tarefas ainda não alocadas a nenhum servidor
        # Cópia independente permite manipulação destrutiva
        self.lista_global_pendentes = list(self.requisicoes)
        
        # RASTREAMENTO DE CARGA ATIVA
        # server_load: contador de tarefas em execução (não apenas na fila)
        self.server_load = {srv["id"]: 0 for srv in self.servidores}
        # load_lock: protege server_load contra condições de corrida
        self.load_lock = threading.Lock()

        # Rastreamento de conclusão
        self.concluidas = set()           # IDs para lookup rápido (O(1))
        self.dados_concluidos = []        # Dados completos para relatório
        self.total_reqs = len(self.requisicoes)  # Total para calcular progresso
        
        # Alocador de servidores (Least Connections)
        self.quick_fit = QuickFitAllocator(self.servidores)
        
        # Flag de controle do loop de orquestração
        self.running = True

    def _load_config(self):
        """Carrega e pré-processa configuração do arquivo JSON.
        
        Lê estrutura JSON esperada com chaves 'servidores' e 'requisicoes',
        validando implicitamente formato e inicializando campos derivados.
        
        Side Effects:
            - Define self.servidores: Lista de dicts com {id, capacidade, ...}.
            - Define self.requisicoes: Lista de dicts com {id, tipo, tempo_exec, ...}.
            - Adiciona campo 'tempo_restante' a cada requisição (inicialmente = tempo_exec).
            
        Raises:
            FileNotFoundError: Se json_path não existir.
            json.JSONDecodeError: Se arquivo não for JSON válido.
            KeyError: Se estrutura JSON faltar chaves 'servidores' ou 'requisicoes'.
            
        Note:
            - 'tempo_restante' permite rastrear progressão de tarefas preemptivas.
            - Modificação in-place das requisições é segura (lista será copiada).
        """
        with open(self.json_path, "r", encoding="utf-8") as f:
            # Parse JSON completo
            d = json.load(f)
            
            # Extrai seções de configuração (lança KeyError se ausentes)
            self.servidores = d["servidores"]
            self.requisicoes = d["requisicoes"]
            
            # Pré-processamento: inicializa tempo restante
            # Cada requisição começa com tempo_restante = tempo_exec
            for r in self.requisicoes:
                r['tempo_restante'] = r['tempo_exec']

    def _priority_name(self, p):
        """Converte nível numérico de prioridade em label humanizado.
        
        Args:
            p (int): Prioridade numérica (1, 2 ou 3).
            
        Returns:
            str: "Alta" (p=1), "Média" (p=2) ou "Baixa" (outros).
            
        Note:
            - Usado apenas para logging/UI, não afeta lógica de escalonamento.
            - Padrão "Baixa" para valores inesperados (defensive programming).
        """
        return "Alta" if p == 1 else "Média" if p == 2 else "Baixa"

    # --- PONTE FULL-DUPLEX ---
    def socket_bridge(self, server_id, port):
        """Estabelece canal de comunicação bidirecional com servidor via TCP.

        Implementa padrão full-duplex: thread separada para envio (sender) +
        loop principal para recepção (receiver). Gerencia contabilidade de
        carga ativa e sincronização de estado entre orquestrador e servidor.
        
        Args:
            server_id (int): Identificador do servidor alvo.
            port (int): Porta TCP onde servidor está escutando.
            
        Lifecycle:
            1. Tenta conectar ao servidor (até 10 tentativas com retry).
            2. Spawna thread sender para enviar tarefas da fila.
            3. Loop principal receiver processa respostas (CONCLUSAO/PREEMPCAO).
            4. Ao encerrar, envia comando STOP e fecha socket.
            
        Concurrency:
            - Sender: Thread dedicada lê task_queues[server_id] e envia JSONs.
            - Receiver: Loop principal (esta thread) lê respostas e atualiza estado.
            - load_lock: Protege server_load contra condições de corrida.
            
        State Management:
            - Incrementa server_load ANTES de enviar tarefa (antecipa carga).
            - Decrementa server_load AO RECEBER resposta (libera slot).
            - CONCLUSAO: Move tarefa para concluidas, registra metrics.
            - PREEMPCAO: Retorna tarefa para lista_global_pendentes com tempo atualizado.
            
        Error Handling:
            - Retry com backoff exponencial (10 tentativas x 0.5s).
            - Falha de conexão: loga erro e retorna (servidor indisponível).
            - Erros de comunicação: encerra ponte graciosamente.
            
        Note:
            - Roda em thread separada (uma por servidor).
            - Conexão permanente durante toda a sessão (não reconecta).
        """
        # Cria socket TCP cliente
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        
        # Retry loop: aguarda servidor inicializar (pode demorar alguns ms)
        connected = False
        for tentativa in range(10):
            try:
                client_socket.connect(('localhost', port))
                connected = True
                break
            except:
                # Servidor ainda não pronto, aguarda antes de retry
                time.sleep(0.5)
        
        # Se todas as tentativas falharam, aborta ponte
        if not connected:
            print(f"❌ Falha ao conectar com Servidor {server_id} na porta {port}")
            return

        # ====================================================================
        # SENDER THREAD: Envia tarefas da fila para o servidor
        # ====================================================================
        def sender_loop():
            """Thread emissora: drena fila do servidor e envia tarefas via socket.
            
            Responsabilidades:
                - Bloqueia aguardando tarefas na task_queues[server_id].
                - Envia JSON via socket para servidor processar.
                - Incrementa server_load ANTES de enviar (reserva slot).
                - Trata comando especial "STOP" para encerramento gracioso.
                
            Termination:
                - self.running=False: Encerra após timeout.
                - Comando "STOP": Envia para servidor e encerra.
                - Erro de socket: Encerra silenciosamente.
            """
            while self.running:
                try:
                    # Aguarda próxima tarefa com timeout (permite checagem de self.running)
                    task = self.task_queues[server_id].get(timeout=0.5)
                    
                    # Comando especial de encerramento
                    if task == "STOP":
                        try:
                            send_json(client_socket, "STOP")
                        except:
                            pass  # Servidor já pode ter fechado
                        break
                    
                    # CONTABILIDADE DE CARGA: Incrementa ANTES de enviar
                    # Garante que Quick Fit veja carga atualizada imediatamente
                    with self.load_lock:
                        self.server_load[server_id] += 1
                    
                    # Envia tarefa via socket (JSON serializado + '\n')
                    send_json(client_socket, task)
                    
                except queue.Empty:
                    # Timeout normal: verifica self.running e continua
                    continue
                except:
                    # Erro crítico (ex: socket fechado): encerra thread
                    break

        # Spawna thread sender (daemon=False para aguardar no join)
        t_sender = threading.Thread(target=sender_loop, daemon=False)
        t_sender.start()

        # ====================================================================
        # RECEIVER LOOP: Processa respostas do servidor (loop principal)
        # ====================================================================
        while self.running:
            try:
                # Lê próxima resposta do servidor (bloqueia até receber)
                resposta = recv_json(client_socket)
                
                # Conexão fechada ou erro de leitura: encerra ponte
                if not resposta:
                    break

                # CONTABILIDADE DE CARGA: Decrementa AO RECEBER resposta
                # Libera slot para próxima tarefa (servidor ficou disponível)
                with self.load_lock:
                    self.server_load[server_id] -= 1

                # ============================================================
                # Tipo: CONCLUSAO - Tarefa finalizada com sucesso
                # ============================================================
                if resposta['tipo'] == 'CONCLUSAO':
                    # Loga evento de conclusão
                    self.logger.log("CONCLUSAO", 
                                   req_id=resposta['req_id'], 
                                   servidor_id=server_id)
                    
                    # Atualiza estruturas de controle de forma atômica
                    with self.logger.lock:
                        self.concluidas.add(resposta['req_id'])          # Marca como concluída
                        self.dados_concluidos.append(resposta)           # Armazena metadados
                        self.logger.metrics["requisicoes_completas"] += 1  # Incrementa métrica
                
                # ============================================================
                # Tipo: PREEMPCAO - Quantum esgotado, tarefa retorna à fila
                # ============================================================
                elif resposta['tipo'] == 'PREEMPCAO':
                    # Loga evento de preempção com tempo restante
                    self.logger.log("PREEMPCAO", 
                                   req_id=resposta['req_id'], 
                                   servidor_id=server_id, 
                                   detalhes=resposta['tempo_restante'])
                    
                    # Incrementa contador de preempções
                    self.logger.add_metric("preempcoes", 1)
                    
                    # Recupera estado atualizado da tarefa (com tempo_restante reduzido)
                    req_atualizada = resposta['dados_originais']
                    req_atualizada['tempo_restante'] = resposta['tempo_restante']
                    
                    # Retorna tarefa para fila global (será reescalonada)
                    self.lista_global_pendentes.append(req_atualizada)

            except:
                # Erro de comunicação: encerra ponte
                break
        
        # Cleanup: aguarda thread sender terminar e fecha socket
        t_sender.join()
        client_socket.close()

    # --- ORQUESTRADOR ---
    def orchestrator_loop(self):
        """Loop central de escalonamento e distribuição de tarefas.

        Implementa ciclo contínuo de:
        1. Ordenação: Aplica estratégia de escalonamento à fila global.
        2. Seleção: Consulta Quick Fit para escolher servidor ideal.
        3. Alocação: Enfileira tarefa na fila do servidor escolhido.
        4. Verificação: Checa condições de término.
        
        Estratégias de Ordenação:
            - PRIORIDADE: Sort por (prioridade, id) crescente.
            - SJF: Sort por tempo_exec crescente (shortest first).
            - ROUND_ROBIN/FIFO: Ordem de inserção preservada (FIFO implícito).
            
        Termination Conditions:
            - Lista global vazia E todas as tarefas concluídas E carga zerada.
            - self.running=False (sinalizado externamente).
            
        Concurrency:
            - load_lock: Protege leitura de server_load (snapshot consistente).
            - task_queues são thread-safe nativamente (queue.Queue).
            
        Note:
            - Polling com sleep(0.01) para balancear responsividade vs CPU.
            - Rollback automático (insert(0)) se alocação falhar.
        """
        # Loga início da orquestração
        self.logger.log("INFO", detalhes=f"Orquestrador (TCP) iniciado. Estratégia: {self.strategy}")
        
        # Loop principal: executa até self.running=False
        while self.running:
            # ================================================================
            # VERIFICAÇÃO DE TÉRMINO
            # ================================================================
            # Condição 1: Lista global vazia (nada mais a escalonar)
            # Condição 2: Todas as tarefas marcadas como concluídas
            if not self.lista_global_pendentes and len(self.concluidas) == self.total_reqs:
                # Condição 3: Carga ativa zerada (ninguém trabalhando)
                with self.load_lock:
                    active = sum(self.server_load.values())
                if active == 0:
                    # Todas as condições satisfeitas: sistema ocioso
                    time.sleep(0.5)
                    continue

            # Se lista global vazia mas ainda há tarefas em execução, aguarda
            if not self.lista_global_pendentes:
                time.sleep(0.1)
                continue

            # ================================================================
            # FASE 1: ORDENAÇÃO (Aplicar estratégia de escalonamento)
            # ================================================================
            if self.strategy == "PRIORIDADE":
                # Ordena por prioridade (1=alta, 3=baixa), desempate por ID
                self.lista_global_pendentes.sort(key=lambda x: (x['prioridade'], x['id']))
            elif self.strategy == "SJF":
                # Shortest Job First: ordena por tempo de execução
                self.lista_global_pendentes.sort(key=lambda x: x['tempo_exec'])
            # ROUND_ROBIN/FIFO: não reordena (mantém ordem FIFO natural)
            
            
            # ================================================================
            # FASE 2: ALOCAÇÃO (Quick Fit + Distribuição)
            # ================================================================
            # Obtém snapshot thread-safe da carga atual
            with self.load_lock:
                current_load = self.server_load.copy()
            
            # Calcula estado atualizado de todos os servidores
            estado = self.quick_fit.calcular_estado_servidores(self.task_queues, current_load)
            
            # Candidata: primeira tarefa após ordenação (maior prioridade)
            req_candidata = self.lista_global_pendentes[0]
            
            # Aplica heurística Least Connections para escolher servidor
            melhor_srv = self.quick_fit.encontrar_melhor_servidor(req_candidata, estado)

            # ============================================================
            # Caso 1: Servidor disponível encontrado
            # ============================================================
            if melhor_srv is not None:
                # Remove tarefa da lista global (commit da alocação)
                req_a_enviar = self.lista_global_pendentes.pop(0)
                
                try:
                    # Enfileira tarefa na fila do servidor escolhido
                    # Thread sender da ponte consumirá e enviará via socket
                    self.task_queues[melhor_srv].put(req_a_enviar)
                    
                    # Prepara informações para logging
                    prio_lbl = self._priority_name(req_a_enviar.get('prioridade', 0))
                    load_real = current_load.get(melhor_srv, 0)
                    cap_real = estado[melhor_srv]['capacidade']
                    
                    # Loga evento de atribuição com métricas de carga
                    self.logger.log(
                        "ATRIBUICAO",
                        req_id=req_a_enviar['id'],
                        servidor_id=melhor_srv,
                        prioridade=prio_lbl,
                        detalhes=f"Carga: {load_real}/{cap_real}"
                    )
                    
                except:
                    # Falha ao enfileirar: rollback (retorna tarefa ao início)
                    # Condição rara (fila não deve falhar), mas garante consistência
                    self.lista_global_pendentes.insert(0, req_a_enviar)
            
            # ============================================================
            # Caso 2: Nenhum servidor disponível (todos saturados)
            # ============================================================
            else:
                # Aguarda antes de tentar novamente (backpressure)
                time.sleep(0.1)
            
            # Pequeno delay para evitar busy-waiting (economiza CPU)
            time.sleep(0.01)

    def monitor_loop(self):
        """Thread de monitoramento contínuo de recursos do sistema.
        
        Coleta métricas de CPU em intervalos regulares para análise de
        desempenho. Roda como daemon thread (encerra com processo principal).
        
        Behavior:
            - Amostra CPU a cada 1 segundo (psutil.cpu_percent).
            - Armazena valores em logger.metrics["cpu_valores"] (lista).
            - Loop termina quando self.running=False.
            
        Note:
            - interval=1 no cpu_percent causa sleep de 1s entre amostras.
            - Métricas agregadas (média, pico) calculadas em exibir_relatorio.
        """
        while self.running:
            # Obtém percentual de uso de CPU (média de 1s)
            cpu = psutil.cpu_percent(interval=1)
            # Adiciona à lista de valores (thread-safe via lock interno)
            self.logger.add_metric("cpu_valores", cpu)

    def exibir_relatorio(self, tempo_total):
        """Gera e imprime relatório estatístico de desempenho do sistema.
        
        Calcula métricas agregadas sobre execução completa e apresenta
        resumo formatado com estatísticas principais.
        
        Args:
            tempo_total (float): Duração total da execução em segundos.
            
        Métricas Calculadas:
            - Tarefas concluídas vs total.
            - Tempo total de execução.
            - Tempo médio de resposta (from submit to complete).
            - Total de preempções (Round Robin).
            - Throughput (tarefas/segundo).
            
        Note:
            - Filtra dados_concluidos para garantir apenas dicts válidos.
            - Tempo de resposta: delta entre tempo_final e start_time.
            - Tratamento especial para caso de nenhuma tarefa concluída.
        """
        # Cabeçalho do relatório
        print("\n" + "="*55)
        print("📊 RESUMO FINAL (MODO SOCKETS TCP/IP)")
        print("="*55)
        
        # Sanitização: filtra apenas dicts válidos (ignora possíveis None/erros)
        dados_validos = [r for r in self.dados_concluidos if isinstance(r, dict)]
        total = len(dados_validos)
        
        # Caso especial: nenhuma tarefa foi concluída
        if total == 0:
            print("Nenhuma tarefa concluída.")
            return

        # Calcula tempos de resposta individuais
        tempos_resp = []
        for r in dados_validos:
            # Tempo de resposta = timestamp de conclusão - início do sistema
            t_final = r.get('tempo_final', time.time())
            tempos_resp.append(t_final - self.logger.start_time)

        # Métricas agregadas
        media_resp = sum(tempos_resp)/total if total > 0 else 0
        preempcoes = self.logger.metrics["preempcoes"]
        
        # Impressão formatada
        print(f"✅ Requisições: {total}/{self.total_reqs}")
        print(f"⏱️  Tempo Total: {tempo_total:.2f}s")
        print(f"📈 Tempo Médio de Resposta: {media_resp:.2f}s")
        print(f"⏸️  Total de Preempções: {preempcoes}")
        if tempo_total > 0:
            # Throughput: tarefas concluídas por segundo
            print(f"🚀 Throughput: {total/tempo_total:.2f} req/s")
        print("\n" + "="*55 + "\n")

    def run(self):
        """Orquestra ciclo de vida completo do sistema distribuído.
        
        Sequencia de inicialização:
        1. Spawna processos servidores TCP independentes.
        2. Aguarda inicialização (bind/listen de sockets).
        3. Cria threads de ponte (uma por servidor).
        4. Inicia threads de orquestração e monitoramento.
        5. Aguarda conclusão de todas as tarefas.
        6. Shutdown gracioso: sinaliza threads, aguarda joins, termina processos.
        7. Exibe relatório final de desempenho.
        
        Arquitetura de Concorrência:
            - N processos: Servidores TCP (multiprocessing).
            - N threads: Pontes full-duplex (uma por servidor).
            - 1 thread: Orquestrador (escalonamento + alocação).
            - 1 thread: Monitor de recursos (daemon).
            - Main thread: Aguarda conclusão e coordena shutdown.
            
        Shutdown Gracioso:
            - Seta self.running=False (threads checam periodicamente).
            - Envia "STOP" para todas as filas (threads sender encerram).
            - Join em bridge_threads e t_orq (aguarda conclusão limpa).
            - Terminate em server_processes (força encerramento).
            
        Error Recovery:
            - KeyboardInterrupt: Captura Ctrl+C e encerra graciosamente.
            - Verificação de health: Alerta se todos os servidores caírem.
            
        Note:
            - Sleep inicial de 2s garante servidores prontos antes de conexões.
            - Daemon thread (monitor) encerra automaticamente com main.
        """
        # Banner de inicialização
        print(f"\n🚀 INICIANDO SISTEMA DISTRIBUÍDO: {self.strategy}")
        
        # ====================================================================
        # FASE 1: SPAWNAR PROCESSOS SERVIDORES
        # ====================================================================
        server_processes = []
        for srv in self.servidores:
            sid = srv['id']
            # Calcula porta dinamicamente (5000 + ID)
            port = get_port_for_server(sid)
            
            # Cria processo servidor independente
            p = multiprocessing.Process(
                target=run_socket_server,
                args=(sid, port, self.quantum),
                daemon=False  # Permite join explícito
            )
            p.start()
            server_processes.append(p)
        
        # Aguarda servidores iniciarem (bind + listen)
        print("⏳ Aguardando servidores iniciarem...")
        time.sleep(2)

        # Marca início da contagem de tempo
        self.logger.start_time = time.time()
        self.logger.log("INICIO")

        # ====================================================================
        # FASE 2: CRIAR THREADS DE PONTE (Full-Duplex)
        # ====================================================================
        bridge_threads = []
        for srv in self.servidores:
            sid = srv['id']
            port = get_port_for_server(sid)
            
            # Cria thread de ponte bidirecional (sender + receiver)
            t = threading.Thread(
                target=self.socket_bridge,
                args=(sid, port),
                daemon=False  # Aguardamos join explícito
            )
            t.start()
            bridge_threads.append(t)

        # ====================================================================
        # FASE 3: INICIAR ORQUESTRADOR E MONITOR
        # ====================================================================
        # Thread de orquestração (escalonamento + distribuição)
        t_orq = threading.Thread(target=self.orchestrator_loop, daemon=False)
        t_orq.start()
        
        # Thread de monitoramento (métricas de CPU) - daemon encerra com main
        t_mon = threading.Thread(target=self.monitor_loop, daemon=True)
        t_mon.start()

        # ====================================================================
        # FASE 4: AGUARDAR CONCLUSÃO DE TODAS AS TAREFAS
        # ====================================================================
        try:
            # Loop de espera: verifica conclusão a cada 0.5s
            while len(self.concluidas) < self.total_reqs:
                time.sleep(0.5)
                
                # Health check: verifica se servidores ainda estão ativos
                if not any(p.is_alive() for p in server_processes):
                    print("⚠️  CRÍTICO: Todos os servidores caíram!")
                    break
                    
        except KeyboardInterrupt:
            # Usuário interrompeu com Ctrl+C
            print("\nInterrupção do usuário. Encerrando graciosamente...")

        # ====================================================================
        # FASE 5: SHUTDOWN GRACIOSO
        # ====================================================================
        # Sinaliza threads para encerrar
        self.running = False
        
        # Calcula tempo total de execução
        tempo_total = time.time() - self.logger.start_time

        # Envia comando STOP para todas as filas (threads sender consumirão)
        for q in self.task_queues.values():
            q.put("STOP")
        
        # Aguarda threads de ponte encerrarem (enviam STOP aos servidores)
        for t in bridge_threads:
            t.join(timeout=5)  # Timeout 5s para evitar deadlock
        
        # Aguarda thread de orquestração encerrar
        t_orq.join(timeout=5)
        
        # Força encerramento de processos servidores (SIGTERM)
        for p in server_processes:
            p.terminate()
            p.join(timeout=2)  # Aguarda até 2s por processo

        # ====================================================================
        # FASE 6: RELATÓRIO FINAL
        # ====================================================================
        self.logger.log("FIM")
        self.exibir_relatorio(tempo_total)

# ============================================================================
# MAIN
# ============================================================================

def main():
    """Entry point principal: Interface CLI para seleção de estratégia.
    
    Apresenta menu interativo permitindo escolha entre 4 estratégias de
    escalonamento. Instancia TaskScheduler com configuração escolhida e
    inicia execução do sistema distribuído.
    
    Opções de Estratégia:
        1. SJF (Shortest Job First):
           - Ordena por tempo_exec crescente.
           - Quantum=0 (não-preemptivo, executa até o fim).
           - Minimiza tempo médio de resposta para workloads heterogêneos.
           
        2. ROUND_ROBIN:
           - Ordem FIFO com quantum=2s.
           - Preemptivo: tarefas rotacionam a cada 2s.
           - Garante fairness, evita starvação.
           
        3. PRIORIDADE:
           - Ordena por prioridade (1=alta, 3=baixa) + ID.
           - Quantum=2s (preemptivo dentro de mesma prioridade).
           - Favorece tarefas críticas.
           
        4. FIFO (Round Robin sem Quantum):
           - Ordem de chegada (First In First Out).
           - Quantum=0 (não-preemptivo).
           - Simplicidade máxima, sem overhead de preempção.
           
    Behavior:
        - Input inválido: Defaults para ROUND_ROBIN (quantum=2).
        - Leitura de tasks.json via constante TASKS_PATH.
        - Execução bloqueante: aguarda conclusão de todas as tarefas.
        
    Note:
        - multiprocessing.freeze_support() necessário para Windows (PyInstaller).
        - Execução interativa, não retorna até conclusão ou Ctrl+C.
    """
    # Apresenta menu de opções
    print("\n" + "="*50)
    print("  SISTEMA DE ORQUESTRAÇÃO DISTRIBUÍDA VIA SOCKETS")
    print("="*50)
    print("Escolha a estratégia de escalonamento:")
    print()
    print("1. SJF (Shortest Job First) - Não-preemptivo")
    print("2. ROUND_ROBIN - Quantum = 2s")
    print("3. PRIORIDADE - Com quantum 2s")
    print("4. FIFO - Ordem de chegada, não-preemptivo")
    print("="*50)
    
    # Lê escolha do usuário
    opt = input("Escolha (1-4): ").strip()
    
    # Mapa de configurações: opção -> (estratégia, quantum)
    cfg = {
        "1": ("SJF", 0),              # SJF: quantum 0 (não-preemptivo)
        "2": ("ROUND_ROBIN", 2),      # RR: quantum 2s
        "3": ("PRIORIDADE", 2),       # Prioridade: quantum 2s
        "4": ("ROUND_ROBIN", 0)       # FIFO: RR com quantum 0 (equivalente)
    }
    
    # Extrai configuração (default: RR com quantum 2s)
    strat, quant = cfg.get(opt, ("ROUND_ROBIN", 2))

    # Instancia orquestrador com configuração escolhida
    app = TaskScheduler(TASKS_PATH, strategy=strat, quantum=quant)
    
    # Inicia execução (bloqueia até conclusão)
    app.run()

if __name__ == "__main__":
    # Suporte para Windows (freeze para exe standalone via PyInstaller)
    multiprocessing.freeze_support()
    
    # Executa interface CLI
    main()