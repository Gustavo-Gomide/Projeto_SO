# Projeto sistemas operacionais

# BSB COMPUTE — Sistema Distribuído (Console + Flask)

Projeto completo de orquestração distribuída com duas interfaces sobre o mesmo núcleo de scheduler: CLI em console e UI Web em Flask. Implementa comunicação via sockets TCP/IP, paralelismo real nos servidores, balanceamento por Least Connections (Quick Fit adaptado) e métricas em tempo real (CPU/memória/SSE). Este README documenta a arquitetura, cada componente, protocolos, execução em Windows, acesso em LAN/VLAN, e troubleshooting profissional.

---
## Imagem da Arquitetura
![paraalelismo arquitetura](diagrama_arquitetura_paralelismo.png)

Perfeito! Vou te ajudar a organizar o trecho do README com instruções claras para preparar o ambiente, diferenciando os comandos para **Windows** e **Linux/Mac**. Aqui está um exemplo bem estruturado:  

---

## Guia de Preparação do Ambiente

### 1. Baixar o código
Clone o repositório:
```bash
git clone https://github.com/Gustavo-Gomide/Projeto_SO.git
cd Projeto_SO
```

### 2. Criar e ativar a Virtual Environment (venv)

#### 🔹 Windows (PowerShell ou CMD)
```bash
python -m venv venv
venv\Scripts\activate
```

#### 🔹 Linux / MacOS (bash/zsh)
```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Instalar dependências
```bash
pip install -r requirements.txt
```

### 4. Executar o projeto

- **Modo Console (linha de comando):**
```bash
python main.py
```

- **Modo Web (Flask):**
```bash
python app.py
```

---

## Sumário
- Visão Geral
- Arquitetura Técnica
- Estrutura do Repositório
- Núcleo (Scheduler): responsabilidades e ciclo de vida
- Servidores TCP: processo/threads e protocolo
- Ponte (Socket Bridge): envio/recebimento e contabilidade de carga
- Balanceador Quick Fit (Least Connections): critérios
- Estratégias de Escalonamento
- Logger + Métricas + SSE
- `tasks.json`: esquema e exemplo
- Execução (Console)
- Execução (Flask UI + API)
- Acesso em LAN/VLAN
- Troubleshooting

## Visão Geral
- Orquestrador lê `tasks.json`, aplica estratégia, distribui tarefas a N servidores TCP e coleta resultados.
- Cada servidor roda como processo independente; para cada tarefa recebida, cria uma thread de execução (paralelismo real).
- Balanceamento por Least Connections com desempate por capacidade e ID, garantindo determinismo e distribuição uniforme.
- Interface Console: execução orientada a menu e relatório final no terminal.
- Interface Flask: UI com SSE para eventos em tempo real, endpoints REST para controle e métricas.

## Arquitetura Técnica
- Processos: 1 orquestrador (main) + N servidores (multiprocessing).
- Threads: por servidor, 1 thread de ponte (sender) + loop receiver; dentro do servidor, 1 thread por tarefa.
- Comunicação: JSON por linha (terminado em `\n`) sobre TCP; full-duplex.
- Portas: por convenção `5000 + id` do servidor (ex.: id=1 → 5001).
- Shutdown gracioso: sinalização `running=False`, envio de `STOP` nas filas, join das pontes e terminate dos processos.

## Estrutura do Repositório
```
Projeto_SO_console/
  README.md                 # Este documento (global do repositório)
  Projeto_SO_console/
    main.py                 # Orquestrador CLI + servidores TCP + Quick Fit + logger
    tasks.json              # Configuração (servidores + requisições)
  Projeto_SO_flask/
    app.py                  # Flask (rotas REST + SSE; UI)
    README.md               # Guia específico da UI Web
    requirements.txt        # Dependências da UI
    tasks.json              # Config (pode ser distinta do console)
    backend/
      scheduler_backend.py  # Núcleo do orquestrador (threads/processos/sockets)
      __init__.py
    templates/
      base.html             # Layout base Jinja
      index.html            # Página principal da UI
    static/
      style.css             # Tema escuro, grid responsivo, tooltips
```

## Núcleo (Scheduler): responsabilidades e ciclo de vida
- Carregar configuração de servidores e requisições a partir de `tasks.json`.
- Manter estruturas:
  - `task_queues`: fila por servidor (consumida pela ponte sender).
  - `lista_global`: tarefas pendentes (ordenadas conforme estratégia).
  - `server_load`: contagem de tarefas ativas por servidor (protegida por `Lock`).
  - `concluidas` e `dados_concluidos`: controle e metadados de resultados.
- Ciclo de execução:
  1. Spawn de processos servidores (bind/listen nas portas dinâmicas).
  2. Criação de pontes socket (conectar, sender/receiver por servidor).
  3. Loop de orquestração (ordenar, escolher servidor, enfileirar tarefa).
  4. Loop de monitoramento (CPU/memória, SSE, agregação de métricas).
  5. Espera pela conclusão; shutdown gracioso; relatório final.

## Servidores TCP: processo/threads e protocolo
- Processo por servidor: aceita conexões do orquestrador.
- Para cada mensagem de tarefa recebida, cria uma thread que executa um “core” de inferência sintético:
  - Respeita `quantum` quando > 0 (preemptivo; devolve `PREEMPCAO`).
  - Quando `quantum == 0`, executa até terminar (devolve `CONCLUSAO`).
- Protocolo:
  - Request: `{id, tempo_exec, tempo_restante?, prioridade, ...}`
  - Response:
    - `{"tipo":"CONCLUSAO", "req_id", "servidor_id", "tempo_final", "dados_originais"}`
    - `{"tipo":"PREEMPCAO", "req_id", "servidor_id", "tempo_restante", "dados_originais"}`
- Comando de controle: string `"STOP"` encerra o servidor.

## Ponte (Socket Bridge): envio/recebimento e contabilidade de carga
- Sender (thread): bloqueia na fila por servidor, incrementa `server_load` antes de enviar, envia JSON ao socket.
- Receiver (loop): ao receber resposta, decrementa `server_load`, atualiza estruturas:
  - `CONCLUSAO`: marca concluída, agrega métrica de throughput/tempo de resposta, log.
  - `PREEMPCAO`: reintroduz na `lista_global` com `tempo_restante` atualizado, conta preempções, log.
- Robustez: tentativas de conexão com backoff; encerramento limpo; exceções tratadas.

## Balanceador Quick Fit (Least Connections): critérios
- Seleção determinística:
  1. Menor carga atual (`server_load`).
  2. Maior capacidade (desempate favorece servidores mais potentes).
  3. Menor ID (determinismo/reprodutibilidade).
- Aceitação somente quando `carga_atual < capacidade`.

## Estratégias de Escalonamento
- SJF: ordena por `tempo_exec` e usa `quantum=0` (não-preemptivo).
- ROUND_ROBIN: usa `quantum` fixo (ex.: 2s), gera preempções controladas.
- PRIORIDADE: ordena por `prioridade` (1 alta, 3 baixa), desempata por `id`.
- FIFO: ordem de chegada (equivalente a RR com `quantum=0`).

## Logger + Métricas + SSE
- Logger humaniza eventos (atribuição, preempção, conclusão, início/fim) com timestamps relativos.
- Métricas agregadas: CPU média (psutil), memória de processos/sistema (na UI), preempções, throughput, tempos de resposta.
- SSE (na UI Flask): cada assinante recebe eventos via fila própria; broadcast não bloqueia o backend; keepalive a cada 30s sem eventos.

## `tasks.json`: esquema e exemplo
- Servidores: `[{"id": int, "capacidade": int}]` — porta = `5000 + id`.
- Requisições: `[{"id": int, "tipo": str, "prioridade": 1|2|3, "tempo_exec": float}]`.
```json
{
  "servidores": [
    {"id": 1, "capacidade": 2},
    {"id": 2, "capacidade": 2},
    {"id": 3, "capacidade": 1}
  ],
  "requisicoes": [
    {"id": 101, "tipo": "visao_computacional", "prioridade": 1, "tempo_exec": 8},
    {"id": 102, "tipo": "nlp", "prioridade": 3, "tempo_exec": 3}
  ]
}
```

## Execução (Console)
```cmd
cd Projeto_SO_console
python -m venv .venv
.venv\Scripts\activate
python -m pip install psutil
python Projeto_SO_console\main.py
```
- Escolha estratégia no menu: 1=SJF, 2=ROUND_ROBIN(2s), 3=PRIORIDADE(2s), 4=FIFO(0s).
- Ao final, relatório: concluídas/total, tempo total, média de resposta, preempções, throughput.

## Execução (Flask UI + API)
```cmd
cd Projeto_SO_flask
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python app.py
```
- Acesse: `http://127.0.0.1:5000`.
- Endpoints:
  - `POST /start` {strategy, quantum, tasksPath?}
  - `POST /stop`
  - `GET /status` — snapshot (carga, fila, ativos) por servidor
  - `GET /metrics` — CPU/memória média, pico, throughput, tempos
  - `GET /results`
  - `GET /events` — SSE (eventos e keepalive)

## Acesso em LAN/VLAN
- UI (Flask): em `app.py`, use `app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)`.
```cmd
netsh advfirewall firewall add rule name="BSB Flask 5000" dir=in action=allow protocol=TCP localport=5000
```
- Servidores TCP (se quiser expor a rede): bind em `('0.0.0.0', porta)` e cliente conectando ao IP da máquina.
```cmd
netsh advfirewall firewall add rule name="BSB Servers 5001-5010" dir=in action=allow protocol=TCP localport=5001-5010
```
- Acesso: `http://<SEU_IP>:5000` (UI) e `<SEU_IP>:500X` (sockets, se expostos).

## Troubleshooting
- Porta ocupada: verifique `5000` (Flask) e `5001–500N` (servidores). `SO_REUSEADDR` ajuda em restarts.
- SSE sem dados: é necessário execução ativa; envio de `keepalive` a cada 30s.
- Performance: ajuste `quantum`, capacidades dos servidores e volume de tarefas.
- Firewall: abra regras conforme exemplos acima; evite exposição pública sem autenticação/TLS.
- JSON inválido: valide `tasks.json` (chaves e tipos). Erros de schema impedem o carregamento.
# Projeto SO Console — Orquestrador Distribuído via Sockets TCP/IP

## Visão Geral
- **Processos servidores** (multiprocessing): cada servidor escuta em TCP e processa tarefas em **threads** (paralelismo real).
- **Orquestrador**: lê `tasks.json`, aloca tarefas, monitora carga, coleta métricas e imprime relatório final.
- **Protocolo**: mensagens JSON delimitadas por `\n` (uma por linha), full-duplex.
- **Balanceamento**: Quick Fit (Least Connections) com desempate por capacidade e ID.
- **Estratégias**: `SJF`, `ROUND_ROBIN`, `PRIORIDADE`, `FIFO` (quantum 0).

## Estrutura do Projeto
```
Projeto_SO/
  main.py          # Orquestrador, servidores TCP, Quick Fit e logger
  tasks.json       # Configuração: servidores e lista de requisições
```

- `tasks.json` (exemplo esperado):
```json
{
  "servidores": [
    {"id": 1, "capacidade": 2},
    {"id": 2, "capacidade": 2},
    {"id": 3, "capacidade": 2}
  ],
  "requisicoes": [
    {"id": 1, "tempo_exec": 3.0, "prioridade": 1},
    {"id": 2, "tempo_exec": 5.0, "prioridade": 2}
  ]
}
```

## Fluxo Lógico
1. **Inicialização**: `main.py` lê `tasks.json`, configura estratégia/quantum e sobe N servidores em processos (porta 5001..500N).
2. **Ponte TCP**: Para cada servidor, um thread de ponte conecta, envia tarefas e recebe respostas.
3. **Orquestração**: Um loop ordena a lista global conforme a estratégia e tenta alocar tarefas usando o estado atual dos servidores.
4. **Execução nos servidores**: Ao receber uma tarefa, o servidor cria uma thread para processá-la. Se `quantum > 0`, pode haver preempção; com `quantum == 0`, executa até terminar.
5. **Resposta**: O servidor envia `CONCLUSAO` ou `PREEMPCAO`. O orquestrador decrementa a carga ativa e, no caso de preempção, reinsere a tarefa com o tempo restante.
6. **Métricas e logs**: `RealtimeLogger` registra eventos e coleta métricas como CPU e número de preempções. Ao final, imprime um resumo.

## Quick Fit Adaptado (Balanceamento)
- **Estado**: Para cada servidor, o orquestrador mantém `carga_atual` (tarefas em execução) e conhece a `capacidade`.
- **Seleção**: Escolhe o servidor com menor `carga_atual`; desempata por maior `capacidade` e depois por menor `id`.
- **Aceitação**: Só aloca quando `carga_atual < capacidade`.

## Estratégias de Escalonamento
- **SJF**: Ordena por `tempo_exec` e executa sem quantum (tempo contínuo).
- **Round Robin**: Usa quantum (ex.: 2s) e pode gerar preempções.
- **Prioridade**: Ordena por `prioridade` (1=Alta, 2=Média, 3=Baixa) e por `id`.
- **FIFO**: Round Robin com `quantum=0` para execução contínua.

## Setup e Execução (Windows CMD)
No diretório do projeto:

```cmd
python -m venv .venv
.venv\Scripts\activate
python -m pip install psutil
python Projeto_SO_console\main.py
```

- Selecione a estratégia:
  - `1` = SJF
  - `2` = ROUND_ROBIN (quantum=2s)
  - `3` = PRIORIDADE
  - `4` = FIFO (quantum=0)

## Detalhes de Implementação
- **Mensagens**: JSON por linha (`\n`) para delimitação simples no TCP stream.
- **Paralelismo**: `threading.Thread` por tarefa dentro de cada servidor.
- **Contabilidade de carga**: `server_load` controlado com `Lock` no orquestrador; decremento ao receber resposta.
- **Métricas**: CPU via `psutil` em thread dedicada; preempções contadas ao receber `PREEMPCAO`.
- **Shutdown**: `STOP` nas filas, join das pontes e `terminate` dos processos.

## Troubleshooting
- **Portas ocupadas**: Verifique se 5001–500N estão livres.
- **Permissões**: Execute o CMD com privilégios suficientes para abrir sockets.
- **Dependências**: Garanta `psutil` instalado.
- **JSON inválido**: Valide `tasks.json` (chaves e tipos corretos).
- **Performance**: ajuste `quantum`, capacidades dos servidores e quantidades de tarefas.

## Licença
Uso acadêmico/didático. Ajuste conforme sua necessidade.
