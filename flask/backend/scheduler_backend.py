import time
import threading
import queue
import socket
import json
from pathlib import Path


# -----------------------------
# Logger com suporte a SSE
# -----------------------------
class RealtimeLogger:
    """Logger com suporte a SSE e métricas agregadas.

    Responsabilidades:
    - Bufferizar eventos humanizados para UI e streaming SSE.
    - Manter métricas agregadas (CPU, preempções, concluídas).
    - Gerenciar lista de inscritos SSE (filas por conexão).

    Design & Concorrência:
    - `events` guarda histórico textual para debugging.
    - `metrics` agrega valores numéricos ao longo da execução.
    - `_subscribers` mantém filas por conexão SSE para broadcast.
    - `lock` protege todas as estruturas internas contra corrida.
    """
    def __init__(self):
        self.events = []
        self.start_time = time.time()
        self.metrics = {
            "requisicoes_completas": 0,
            "cpu_valores": [],
            "preempcoes": 0
        }
        self.lock = threading.Lock()
        self._subscribers = []  # filas (Queue) para SSE

    def subscribe(self):
        """Cria e registra uma fila para SSE e retorna ao assinante.

        Returns:
            queue.Queue: Fila dedicada para o assinante atual.
        """
        q = queue.Queue(maxsize=1000)
        with self.lock:
            self._subscribers.append(q)
        return q

    def _broadcast(self, payload):
        """Envia evento para todas as filas SSE sem bloquear.

        Estratégia de robustez:
        - `put_nowait` evita bloquear o thread do logger.
        - `queue.Full` ocasiona remoção do assinante (provável desconexão).
        """
        drop = []
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except queue.Full:
                drop.append(q)
        if drop:
            with self.lock:
                for q in drop:
                    if q in self._subscribers:
                        self._subscribers.remove(q)

    def log(self, event_type, req_id=None, servidor_id=None, prioridade=None, detalhes=""):
        """Formata e publica um evento de log (stdout + SSE).

        Args:
            event_type (str): Tipo do evento (ATRIBUICAO, CONCLUSAO, PREEMPCAO, INICIO, FIM, INFO/ERRO).
            req_id (int|None): Identificador da requisição (opcional).
            servidor_id (int|None): ID do servidor (opcional).
            prioridade (str|None): Rótulo da prioridade (opcional).
            detalhes (str): Texto adicional para contexto.

        Side Effects:
            - Acrescenta linha em `events` (histórico).
            - Publica payload para assinantes SSE.
        """
        elapsed = time.time() - self.start_time
        ts = self._format_time(elapsed)
        if event_type == "ATRIBUICAO":
            text = f"[{ts}] Req {req_id} ({prioridade}) -> Servidor {servidor_id} | {detalhes}"
        elif event_type == "CONCLUSAO":
            text = f"[{ts}] Concluída no Servidor {servidor_id} (Req {req_id})"
        elif event_type == "PREEMPCAO":
            text = f"[{ts}] Preempção (Req {req_id}) - resta {detalhes}s"
        elif event_type == "INICIO":
            text = f"[{ts}] ===== INÍCIO ====="
        elif event_type == "FIM":
            text = f"[{ts}] ===== FIM ====="
        else:
            text = f"[{ts}] {event_type}: {detalhes}"

        payload = {"type": event_type, "text": text, "ts": ts, "req": req_id, "srv": servidor_id}
        with self.lock:
            self.events.append(text)
        self._broadcast(payload)

    def _format_time(self, seconds):
        """Converte segundos em timestamp MM:SS.mmm.

        Example:
            >>> _format_time(125.456)
            '02:05.456'
        """
        minutes = int(seconds) // 60
        secs = int(seconds) % 60
        ms = int((seconds % 1) * 1000)
        return f"{minutes:02d}:{secs:02d}.{ms:03d}"

    def add_metric(self, metric, value):
        """Atualiza métrica agregada de forma thread-safe.

        Args:
            metric (str): Nome da métrica ('cpu_valores', 'preempcoes', ...).
            value (Any): Valor a agregar (append ou soma).
        """
        with self.lock:
            if metric == "cpu_valores":
                self.metrics[metric].append(value)
            elif metric in self.metrics:
                self.metrics[metric] += value

# -----------------------------
# Config e utilitários de rede
# -----------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
TASKS_PATH = BASE_DIR / "tasks.json"

def get_port_for_server(server_id):
    """Gera porta TCP dinamicamente para qualquer ID de servidor.

    Returns:
        int: 5000 + server_id (evita portas privilegiadas e colisões comuns).
    """
    return 5000 + server_id


def send_json(sock, data):
    try:
        msg = json.dumps(data) + "\n"
        sock.sendall(msg.encode("utf-8"))
    except Exception:
        pass


def recv_json(sock):
    buffer = ""
    while True:
        try:
            chunk = sock.recv(1024).decode("utf-8")
            if not chunk:
                return None
            buffer += chunk
            if "\n" in buffer:
                msg, _ = buffer.split("\n", 1)
                return json.loads(msg)
        except Exception:
            return None

# -----------------------------
# Quick Fit e servidor worker
# -----------------------------
class QuickFitAllocator:
    """Alocador de carga baseado em Least Connections (Quick Fit adaptado).

    Seleção:
    - 1º: menor carga atual (evita hotspots).
    - 2º: maior capacidade (desempate favorece servidores potentes).
    - 3º: menor ID (determinismo/reprodutibilidade).
    """
    def __init__(self, servidores):
        self.servidores = servidores

    def _cap(self, sid):
        for s in self.servidores:
            if s["id"] == sid:
                return s["capacidade"]
        return 1

    def snapshot(self, current_loads):
        """Gera estado atual dos servidores combinando capacidade e carga.

        Args:
            current_loads (dict[int,int]): Contadores de carga por servidor.

        Returns:
            dict[int,dict]: Estado com chaves ('id','capacidade','carga_atual','pode_aceitar').
        """
        estado = {}
        for sid in [s["id"] for s in self.servidores]:
            cap = self._cap(sid)
            load = current_loads.get(sid, 0)
            estado[sid] = {
                "id": sid,
                "capacidade": cap,
                "carga_atual": load,
                "pode_aceitar": load < cap,
            }
        return estado

    def best_server(self, estado):
        """Seleciona melhor servidor conforme heurística Least Connections.

        Returns:
            int|None: ID do servidor selecionado ou None se todos cheios.
        """
        candidatos = [e for e in estado.values() if e["pode_aceitar"]]
        if not candidatos:
            return None
        m = min(candidatos, key=lambda e: (e["carga_atual"], -e["capacidade"], e["id"]))
        return m["id"]


def fake_inference_core(req, quantum):
    """Simula processamento de tarefa com mix de I/O e CPU, respeitando quantum.

    Detalhes:
    - Usa chunks de 0.1s para responsividade e precisão do tempo.
    - Cálculo sintético (soma de quadrados) gera carga CPU mensurável.
    """
    restante = req.get("tempo_restante", req.get("tempo_exec"))
    exec_time = restante if quantum == 0 else min(quantum, restante)
    start = time.time()
    elapsed = 0
    while elapsed < exec_time:
        chunk = min(0.1, exec_time - elapsed)
        time.sleep(chunk)
        _ = sum(i * i for i in range(1000))
        elapsed = time.time() - start
    novo = max(0, restante - exec_time)
    return round(novo, 2), exec_time


def process_task_thread(conn, lock, req, quantum, server_id):
    """Processa tarefa em thread e envia resposta ao orquestrador.

    Protocolos:
    - CONCLUSAO: `tempo_restante` ~ 0 (tarefa finalizada).
    - PREEMPCAO: `tempo_restante` > 0 (retorna à fila global).
    """
    req_id = req.get("id")
    novo_restante, _ = fake_inference_core(req, quantum)
    req["tempo_restante"] = novo_restante
    if novo_restante <= 0.05:
        resp = {"tipo": "CONCLUSAO", "req_id": req_id, "servidor_id": server_id, "tempo_final": time.time(), "dados_originais": req}
    else:
        resp = {"tipo": "PREEMPCAO", "req_id": req_id, "servidor_id": server_id, "tempo_restante": novo_restante, "dados_originais": req}
    with lock:
        try:
            send_json(conn, resp)
        except Exception:
            pass


def run_socket_server(server_id, port, quantum):
    """Servidor TCP multi-thread por processo: aceita conexões e processa tarefas.

    Ciclo:
    - bind/listen na porta.
    - accept conexões do orquestrador.
    - para cada requisição: cria thread e processa.
    - encerra ao receber "STOP".
    """
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server_socket.bind(("localhost", port))
        server_socket.listen(5)
    except Exception as e:
        print(f"[Server {server_id}] bind error: {e}")
        return

    while True:
        try:
            conn, _ = server_socket.accept()
            sock_lock = threading.Lock()
            while True:
                req = recv_json(conn)
                if not req:
                    break
                if req == "STOP":
                    break
                t = threading.Thread(target=process_task_thread, args=(conn, sock_lock, req, quantum, server_id))
                t.start()
            conn.close()
            if req == "STOP":
                break
        except Exception:
            pass
    server_socket.close()

