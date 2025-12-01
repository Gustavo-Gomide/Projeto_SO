import time
import threading
import queue


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
