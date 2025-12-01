import time
import threading
import queue
import socket
import json
from pathlib import Path
import multiprocessing
import psutil

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

# -----------------------------
# Scheduler controlado por serviço
# -----------------------------
class TaskScheduler:
    """Orquestrador principal: distribuição, comunicação e métricas.

    Estruturas principais:
    - `task_queues`: filas por servidor consumidas pela ponte sender.
    - `lista_global`: tarefas a alocar (após ordenação por estratégia).
    - `server_load`: contadores de tarefas em execução por servidor.
    - `server_active`: ids das tarefas atualmente ativas por servidor.
    - `dados_concluidos`: resultados para relatório e UI.
    """
    def __init__(self, json_path, strategy="ROUND_ROBIN", quantum=2, logger=None):
        self.json_path = json_path
        self.strategy = strategy.upper()
        self.quantum = 0 if self.strategy == "SJF" else quantum
        self.logger = logger or RealtimeLogger()
        self._load_config()
        self.task_queues = {srv["id"]: queue.Queue() for srv in self.servidores}
        self.lista_global = list(self.requisicoes)
        self.server_load = {srv["id"]: 0 for srv in self.servidores}
        self.load_lock = threading.Lock()
        self.concluidas = set()
        self.dados_concluidos = []
        self.total_reqs = len(self.requisicoes)
        self.quick = QuickFitAllocator(self.servidores)
        self.running = True
        # métricas e controle de ciclo de vida
        self.end_time = None
        self.server_processes = []
        self.ps_procs = []            # psutil.Process dos servidores
        self.proc_mem_series = []     # MB: memória do grupo (Flask + servidores)
        self.proc_mem_peak_mb = 0.0
        self.sys_mem_series = []      # % de memória do sistema
        # acompanhamento das tarefas ativas por servidor
        self.server_active = {srv["id"]: set() for srv in self.servidores}

    def _load_config(self):
        with open(self.json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.servidores = data["servidores"]
        self.requisicoes = data["requisicoes"]
        for r in self.requisicoes:
            r["tempo_restante"] = r["tempo_exec"]

    def _priority_name(self, p):
        return "Alta" if p == 1 else "Média" if p == 2 else "Baixa"

    def socket_bridge(self, server_id, port):
        """Ponte full-duplex: thread sender + loop receiver com contabilidade de carga.

        Robustez:
        - 10 tentativas de conexão com backoff 0.5s.
        - Remoção/adição de IDs em `server_active` protegida por lock.
        """
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        for _ in range(10):
            try:
                client.connect(("localhost", port))
                break
            except Exception:
                time.sleep(0.5)
        else:
            self.logger.log("ERRO", detalhes=f"Falha ao conectar no servidor {server_id}")
            return

        def sender():
            while self.running:
                try:
                    task = self.task_queues[server_id].get(timeout=0.5)
                    if task == "STOP":
                        try:
                            send_json(client, "STOP")
                        except Exception:
                            pass
                        break
                    with self.load_lock:
                        self.server_load[server_id] += 1
                        try:
                            self.server_active[server_id].add(task.get("id"))
                        except Exception:
                            pass
                    send_json(client, task)
                except queue.Empty:
                    continue
                except Exception:
                    break
        t_sender = threading.Thread(target=sender)
        t_sender.start()

        while self.running:
            try:
                resp = recv_json(client)
                if not resp:
                    break
                with self.load_lock:
                    self.server_load[server_id] -= 1
                    try:
                        self.server_active[server_id].discard(resp.get("req_id"))
                    except Exception:
                        pass
                if resp["tipo"] == "CONCLUSAO":
                    self.logger.log("CONCLUSAO", req_id=resp["req_id"], servidor_id=server_id)
                    with self.logger.lock:
                        self.concluidas.add(resp["req_id"])
                        self.dados_concluidos.append(resp)
                        self.logger.metrics["requisicoes_completas"] += 1
                elif resp["tipo"] == "PREEMPCAO":
                    self.logger.log("PREEMPCAO", req_id=resp["req_id"], servidor_id=server_id, detalhes=resp["tempo_restante"])
                    self.logger.add_metric("preempcoes", 1)
                    req_upd = resp["dados_originais"]
                    req_upd["tempo_restante"] = resp["tempo_restante"]
                    self.lista_global.append(req_upd)
            except Exception:
                break
        t_sender.join()
        client.close()

    def orchestrator(self):
        """Loop de escalonamento conforme estratégia e alocação via Quick Fit.

        Estratégias:
        - PRIORIDADE: ordenar por (prioridade, id).
        - SJF: ordenar por `tempo_exec`.
        - RR/FIFO: não reordena (FIFO natural).
        """
        self.logger.log("INFO", detalhes=f"Orquestrador iniciado. Estratégia: {self.strategy}")
        while self.running:
            # Encerramento se nada pendente e nada ativo
            if not self.lista_global and len(self.concluidas) == self.total_reqs:
                with self.load_lock:
                    active = sum(self.server_load.values())
                if active == 0:
                    break
            if not self.lista_global:
                time.sleep(0.05)
                continue
            if self.strategy == "PRIORIDADE":
                self.lista_global.sort(key=lambda x: (x["prioridade"], x["id"]))
            elif self.strategy == "SJF":
                self.lista_global.sort(key=lambda x: x["tempo_exec"])
            with self.load_lock:
                current = self.server_load.copy()
            estado = self.quick.snapshot(current)
            req = self.lista_global[0]
            srv = self.quick.best_server(estado)
            if srv is not None:
                self.lista_global.pop(0)
                try:
                    self.task_queues[srv].put(req)
                    prio = self._priority_name(req.get("prioridade", 0))
                    load = current.get(srv, 0)
                    cap = estado[srv]["capacidade"]
                    self.logger.log("ATRIBUICAO", req_id=req["id"], servidor_id=srv, prioridade=prio, detalhes=f"Carga: {load}/{cap}")
                except Exception:
                    self.lista_global.insert(0, req)
            else:
                time.sleep(0.05)
            time.sleep(0.01)

    def monitor(self):
        """Coleta CPU, memória de processos e memória do sistema periodicamente.

        Métricas:
        - `cpu_valores`: psutil.cpu_percent(interval=1) (média de 1s).
        - `proc_mem_series`: RSS total (Flask + servidores) em MB.
        - `proc_mem_peak_mb`: pico de memória do processo somado.
        - `sys_mem_series`: percentual de uso do sistema.
        """
        while self.running:
            cpu = psutil.cpu_percent(interval=1)
            self.logger.add_metric("cpu_valores", cpu)
            # memória do processo principal + servidores (RSS total)
            try:
                rss_total = psutil.Process().memory_info().rss
            except Exception:
                rss_total = 0
            for pr in list(getattr(self, 'ps_procs', [])):
                try:
                    rss_total += pr.memory_info().rss
                except Exception:
                    pass
            mb = round(rss_total / (1024*1024), 2)
            self.proc_mem_series.append(mb)
            if mb > self.proc_mem_peak_mb:
                self.proc_mem_peak_mb = mb
            # memória do sistema (% usado)
            try:
                self.sys_mem_series.append(psutil.virtual_memory().percent)
            except Exception:
                pass

    def run(self):
        """Inicializa servidores, cria pontes, inicia orquestrador e monitor, e aguarda fim.

        Shutdown:
        - Seta `running=False`, envia "STOP" às filas, junta threads, termina processos.
        - Define `end_time` para congelar métricas de tempo total.
        """
        server_processes = []
        for s in self.servidores:
            sid = s["id"]
            port = get_port_for_server(sid)
            p = multiprocessing.Process(target=run_socket_server, args=(sid, port, self.quantum))
            p.start()
            server_processes.append(p)
        # guardar processos e preparar psutil
        self.server_processes = server_processes
        self.ps_procs = []
        for p in list(server_processes):
            try:
                pr = psutil.Process(p.pid)
                pr.cpu_percent(None)  # inicia a amostragem
                self.ps_procs.append(pr)
            except Exception:
                pass
        time.sleep(2)
        self.logger.start_time = time.time()
        self.logger.log("INICIO")

        bridges = []
        for s in self.servidores:
            sid = s["id"]
            port = get_port_for_server(sid)
            t = threading.Thread(target=self.socket_bridge, args=(sid, port))
            t.start()
            bridges.append(t)

        t_orq = threading.Thread(target=self.orchestrator)
        t_orq.start()
        t_mon = threading.Thread(target=self.monitor, daemon=True)
        t_mon.start()

        try:
            while len(self.concluidas) < self.total_reqs and self.running:
                time.sleep(0.3)
                if not any(p.is_alive() for p in server_processes):
                    self.logger.log("ERRO", detalhes="Todos os servidores caíram")
                    break
        except KeyboardInterrupt:
            pass

        self.running = False
        for qv in self.task_queues.values():
            try:
                qv.put("STOP")
            except Exception:
                pass
        for t in bridges:
            t.join()
        t_orq.join()
        for p in server_processes:
            p.terminate()
        self.logger.log("FIM")
        self.end_time = time.time()

    def stop(self):
        self.running = False

    # snapshots para a API
    def summary(self):
        if self.logger.start_time:
            end = self.end_time if self.end_time else time.time()
            tempo_total = end - self.logger.start_time
        else:
            tempo_total = 0
        total = len([r for r in self.dados_concluidos if isinstance(r, dict)])
        media_resp = 0
        if total:
            tempos_resp = []
            for r in self.dados_concluidos:
                t_final = r.get("tempo_final", time.time())
                tempos_resp.append(t_final - self.logger.start_time)
            media_resp = sum(tempos_resp) / total
        throughput = (total / tempo_total) if tempo_total > 0 else 0
        cpu_vals = self.logger.metrics.get("cpu_valores", [])
        cpu_avg = round(sum(cpu_vals)/len(cpu_vals), 2) if cpu_vals else 0
        mem_sys_avg = round(sum(self.sys_mem_series)/len(self.sys_mem_series), 2) if self.sys_mem_series else 0
        mem_proc_avg = round(sum(self.proc_mem_series)/len(self.proc_mem_series), 2) if self.proc_mem_series else 0
        return {
            "total_concluidas": total,
            "total_requisicoes": self.total_reqs,
            "preempcoes": self.logger.metrics.get("preempcoes", 0),
            "tempo_total": round(tempo_total, 2),
            "media_resposta": round(media_resp, 2),
            "throughput": round(throughput, 2),
            "cpu_avg": cpu_avg,
            "mem_sys_percent_avg": mem_sys_avg,
            "mem_proc_mb_avg": mem_proc_avg,
            "mem_proc_mb_peak": round(self.proc_mem_peak_mb, 2),
            "finished": bool(self.end_time is not None),
        }

    def server_states(self):
        states = []
        with self.load_lock:
            loads = self.server_load.copy()
        for s in self.servidores:
            sid = s["id"]
            cap = s["capacidade"]
            try:
                qobj = self.task_queues[sid]
                qsize = qobj.qsize()
                # snapshot dos próximos itens da fila (ids)
                try:
                    raw = list(qobj.queue)
                except Exception:
                    raw = []
                queue_ids = [r.get("id") if isinstance(r, dict) else None for r in raw if r != "STOP"]
                queue_ids = [i for i in queue_ids if i is not None][:8]
            except Exception:
                qsize = 0
                queue_ids = []
            active_ids = list(self.server_active.get(sid, set()))
            states.append({
                "id": sid,
                "capacidade": cap,
                "carga": loads.get(sid, 0),
                "fila": qsize,
                "queue_ids": queue_ids,
                "active_ids": active_ids,
            })
        return states

    def pendentes(self):
        with self.load_lock:
            active = sum(self.server_load.values())
        return len(self.lista_global) + active

