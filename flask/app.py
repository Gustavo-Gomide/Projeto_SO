"""
=================================================================
BSB COMPUTE — Camada Flask (UI + API) com SSE
=================================================================

Visão geral
-----------
Esta camada fornece interface web e endpoints REST para operar o
orquestrador distribuído. A UI consome status/métricas e recebe
eventos em tempo real via Server-Sent Events (SSE), enquanto as
rotas delegam a lógica de execução ao `SchedulerService`.

Destaques de arquitetura
------------------------
- Rotas finas: validação de entrada e delegação ao serviço backend.
- SSE com filas dedicadas por assinante: evita bloqueios e backpressure.
- JSON consistente: respostas estruturadas para fácil integração.
- Concorrência segura: serviço interno usa locks; Flask roda em modo
    `threaded=True` para múltiplas conexões (REST + SSE).

EndPoints
---------
- GET `/`          : Renderiza UI principal.
- POST `/start`    : Inicia execução com `strategy` e `quantum`.
- POST `/stop`     : Solicita parada graciosa do backend em execução.
- GET `/status`    : Snapshot de servidores (carga, capacidade, fila, ativos).
- GET `/metrics`   : Métricas agregadas (CPU, memória, tempos, throughput).
- GET `/results`   : Dados de tarefas concluídas para relatório.
- GET `/events`    : Stream SSE com eventos em tempo real e keepalive.

Protocolos e formatos
---------------------
- Requests: JSON ou form-data. Parâmetros principais:
    - `strategy`: "SJF", "ROUND_ROBIN", "PRIORIDADE", "FIFO".
    - `quantum` : inteiro (segundos); ignorado para SJF (0).
    - `tasksPath`: caminho para arquivo `tasks.json` (opcional).
- Responses: `application/json` com campos `ok`, `message`, payloads
    específicos por rota.
- SSE: linhas no formato `data: {json}\n\n`, com `keepalive` após 30s
    sem eventos.

Concorrência & robustez
-----------------------
- `SchedulerService` mantém thread-safety e lifecycle do backend.
- Assinantes SSE recebem filas independentes; desconexões são tratadas.
- `debug=True` apenas em desenvolvimento; em produção usar WSGI.
"""

import os
import json
import queue
import threading
from datetime import datetime
from flask import Flask, render_template, request, jsonify, Response, stream_with_context

from backend.scheduler_backend import SchedulerService

app = Flask(__name__)
service = SchedulerService()

@app.route("/")
def index():
    """Página inicial da UI.

    Renderiza `templates/index.html` com JS que consome `/status`,
    `/metrics` e `/events` para atualização dinâmica.
    """
    return render_template("index.html")

@app.post("/start")
def start():
    """Inicia o backend com parâmetros fornecidos pela UI.

    Request:
        Body JSON/form com `strategy`, `quantum`, `tasksPath` (opcional).
    Behavior:
        - `strategy` normalizado para uppercase.
        - `quantum` validado como inteiro; default=2.
        - `tasksPath` default para `Projeto_SO_flask/tasks.json`.

    Returns:
        JSON `{ok: bool, message: str}` indicando resultado da operação.
    """
    body = request.get_json(silent=True) or request.form
    strategy = (body.get("strategy") or "ROUND_ROBIN").upper()
    quantum = body.get("quantum")
    try:
        quantum = int(quantum) if quantum not in (None, "") else 2
    except ValueError:
        return jsonify({"ok": False, "error": "quantum deve ser inteiro"}), 400

    tasks_path = body.get("tasksPath") or os.path.join(os.path.dirname(__file__), "tasks.json")
    ok, msg = service.start(tasks_path, strategy=strategy, quantum=quantum)
    return jsonify({"ok": ok, "message": msg})

@app.post("/stop")
def stop():
    """Solicita parada graciosa do backend atual, se existir.

    Returns:
        JSON com `ok` e `message` descrevendo o resultado.
    """
    ok, msg = service.stop()
    return jsonify({"ok": ok, "message": msg})

@app.get("/status")
def status():
    """Snapshot de estado dos servidores e pendências.

    Inclui `running`, estratégia/quantum atuais, pendentes e lista
    detalhada de servidores (carga, capacidade, fila, active_ids).

    Returns:
        JSON com o estado atual consolidado.
    """
    return jsonify(service.status())

@app.get("/metrics")
def metrics():
    """Métricas agregadas de execução (CPU, memória, tempos).

    Campos: total_concluidas, total_requisicoes, preempcoes, tempo_total,
    media_resposta, throughput, cpu_avg, mem_sys_percent_avg,
    mem_proc_mb_avg, mem_proc_mb_peak, finished.

    Returns:
        JSON com métricas sumarizadas para UI/relatórios.
    """
    return jsonify(service.metrics())

@app.get("/results")
def results():
    """Resultados finais de execução (tarefas concluídas).

    Útil para relatórios pós-execução.

    Returns:
        JSON com lista de conclusões e metadados.
    """
    return jsonify(service.results())

@app.get("/events")
def events():
    """Endpoint SSE para stream contínuo de eventos.

    Comportamento:
    - Sem execução ativa: stream curto com evento `idle`.
    - Execução ativa: consome fila do serviço e envia `data: <json>\n\n`.
    - Keepalive: a cada 30s sem eventos, envia `keepalive`.

    Returns:
        `text/event-stream` com linhas SSE. Encerra após evento `FIM`.
    """
    q = service.subscribe()
    if q is None:
        # Sem execução ativa: stream curto avisando "idle"
        def idle_stream():
            yield "data: {\"event\":\"idle\"}\n\n"
        return Response(stream_with_context(idle_stream()), mimetype="text/event-stream")

    def gen():
        while True:
            try:
                # Bloca até 30s aguardando próximo evento
                item = q.get(timeout=30)
                payload = json.dumps(item, ensure_ascii=False)
                yield f"data: {payload}\n\n"
                # Encerra stream quando evento de FIM for recebido
                if item.get("type") == "FIM":
                    break
            except queue.Empty:
                # keep-alive: evita timeouts/interrupções do browser
                yield "data: {\"event\":\"keepalive\"}\n\n"
    return Response(stream_with_context(gen()), mimetype="text/event-stream")

if __name__ == "__main__":
    # Execução para desenvolvimento local
    # `threaded=True` suporta múltiplas conexões (SSE + chamadas REST)
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
