# Relatório de Arquitetura CAMPEX

## Visão do produto

A CAMPEX está sendo organizada em duas frentes bem definidas: CAMPEX Cloud e CAMPEX Node.

A CAMPEX Cloud é o painel central. Ela concentra usuários, empresas, autenticação, cadastro de câmeras, dashboards, relatórios, eventos, métricas, notificações e dados históricos. Ela roda em ambiente web, como Vercel/backend cloud, e deve ser acessível de qualquer lugar autorizado.

O CAMPEX Node é o software local instalado dentro da empresa do cliente. Ele fica na mesma rede das câmeras IP/RTSP, roda 24/7 e continua funcionando mesmo se o navegador do painel estiver fechado. Ele conversa com as câmeras locais, processa dados localmente e envia para a Cloud apenas eventos, métricas e estados operacionais.

O fluxo alvo é:

```text
Câmeras locais → CAMPEX Node → processamento local → eventos/métricas → CAMPEX Cloud → dashboards/notificações
```

Em termos de rede, a direção principal deve ser:

```text
CAMPEX Node → API Cloud ← Frontend
```

O Painel Web não precisa acessar o `.exe` diretamente pela rede local. Isso
evita portas abertas no servidor do cliente, funciona atrás de NAT/firewall e
permite operar muitos Nodes em paralelo:

```text
Empresa A → Node A ┐
Empresa B → Node B ┼→ CAMPEX Cloud → Banco/API → Dashboard
Empresa C → Node C ┘
```

## Por que essa arquitetura é necessária

Câmeras RTSP com IP privado, como endereços `192.168.x.x`, normalmente não podem ser acessadas diretamente pela Vercel ou por outro backend cloud. A Cloud está fora da rede local do cliente. Por isso, o painel web não deve tentar abrir ou testar diretamente uma câmera privada.

O CAMPEX Node resolve esse problema porque roda dentro da rede do cliente. Ele acessa o RTSP local, mantém conexão com as câmeras e envia dados para a Cloud por HTTPS usando autenticação própria de Node.

Essa divisão também evita expor senhas RTSP no frontend público e reduz tráfego de vídeo para a internet. A Cloud passa a receber dados operacionais, não vídeo bruto contínuo.

Exemplo de dado sincronizado:

```text
camera_07 → pessoa entrou → setor estoque → 09:42:17
```

O Node envia eventos, contagens, métricas, alertas e snapshots pontuais quando
necessário. Ele não deve transmitir vídeo bruto continuamente para a Cloud.

## O que já foi construído

### Base do CAMPEX Node

Foi criado um componente Python independente do frontend, com estrutura modular:

```text
campex_node/
├── cameras/
├── cloud/
├── core/
├── storage/
├── telemetry/
├── workers/
├── local_app.py
├── main.py
├── requirements.txt
└── README.md
```

O Node já possui:

- carregamento de configuração por variáveis de ambiente;
- leitura opcional de `.env` local;
- identificador único de Node;
- logs com sanitização;
- banco SQLite local;
- gerenciador de múltiplas câmeras;
- workers independentes por câmera;
- captura RTSP via backend de câmera existente;
- reconexão automática;
- isolamento entre câmeras, para uma câmera offline não derrubar as outras;
- modo de serviço de longa duração;
- aplicativo local leve em `http://127.0.0.1:8787`.

### Pareamento Node ↔ Cloud

A Cloud agora possui fluxo de pareamento:

1. o painel Cloud gera um código temporário;
2. o usuário informa esse código no aplicativo local do Node;
3. a Cloud emite `node_id` e `node_token`;
4. o Node guarda esses dados localmente;
5. o Node passa a buscar configuração e enviar dados usando `Authorization: Bearer {CAMPEX_NODE_TOKEN}`.

O token administrativo do backend, configurado como `CAMPEXTOKEN` ou `CAMPEX_API_TOKEN`, permanece separado do token do Node.

### Configuração pela Cloud

O painel Cloud já pode cadastrar câmeras. O Node busca a configuração na Cloud e aplica localmente. Isso permite o modelo desejado: o cliente configura câmeras pelo frontend, a Cloud registra essa configuração e o software local baixa essa configuração para operar dentro da rede.

A Cloud entrega ao Node a URL RTSP real para uso local. Ao listar câmeras para o frontend, as credenciais RTSP são mascaradas.

### Correção de autenticação frontend/backend

As rotas protegidas passaram a receber o token esperado pelo backend. O frontend usa cabeçalhos adequados para chamadas protegidas, evitando os erros 401 que estavam acontecendo em rotas como:

- `/api/v1/cameras`
- `/api/v1/cameras/test-source`

O nome de variável usado em produção pode ser `CAMPEXTOKEN`, mantendo compatibilidade com `CAMPEX_API_TOKEN`.

### Sincronização Node → Cloud

Foi criado o módulo de sincronização do Node com fila local. Quando a Cloud está indisponível, os dados ficam no SQLite local e são reenviados depois.

A Cloud recebeu endpoints dedicados para ingestão autenticada por token do Node:

- `POST /api/v1/node-sync/events`
- `POST /api/v1/node-sync/metrics`
- `POST /api/v1/node-sync/batch`

Esses endpoints são idempotentes. Se o Node reenviar o mesmo item, a Cloud não duplica o dado.

### Telemetria operacional local

A fase atual adicionou o coletor de telemetria do Node. Ele observa o estado das câmeras e enfileira métricas operacionais, sem IA.

Métricas geradas por câmera:

- `camera_online`
- `camera_frames_received`
- `camera_reconnect_attempts`
- `camera_consecutive_failures`

Eventos gerados:

- `camera_status_changed`, emitido quando uma câmera muda de estado.

Esses dados seguem pelo mesmo outbox local e pela mesma sincronização segura com a Cloud.

## Segurança aplicada

A arquitetura evita colocar segredos no código público.

Regras já aplicadas:

- tokens vêm de variáveis de ambiente ou pareamento;
- `.env` local não é versionado;
- URLs RTSP são sanitizadas em logs e respostas públicas;
- o frontend não deve receber senha de câmera;
- o Node usa token próprio, diferente do token administrativo da Cloud;
- comunicação Cloud/Node foi preparada para HTTPS e `Authorization: Bearer`.

## Como executar localmente hoje

Backend local:

```bash
python scripts/run_local_connector.py
```

Node App local:

```bash
python -m campex_node.main --app
```

Painel local do Node:

```text
http://127.0.0.1:8787
```

Health do backend:

```text
http://127.0.0.1:8000/api/v1/health
```

Variáveis principais:

```bash
CAMPEX_NODE_CLOUD_URL=http://127.0.0.1:8000/api/v1
CAMPEX_NODE_TOKEN=token_emitido_no_pareamento
CAMPEX_NODE_ID=node_emitido_no_pareamento
CAMPEX_NODE_SYNC_SECONDS=10
CAMPEX_NODE_TELEMETRY_SECONDS=30
CAMPEX_NODE_HEARTBEAT_SECONDS=30
```

Na Vercel/backend Cloud, o token administrativo pode ser configurado como:

```bash
CAMPEXTOKEN=valor_privado
```

## Estado atual dos testes

A base atual foi validada com testes automatizados cobrindo:

- pareamento do Node;
- autenticação por token de Node;
- listagem/configuração de câmeras;
- heartbeat;
- outbox local;
- sincronização de eventos/métricas;
- idempotência na Cloud;
- telemetria de câmera;
- sanitização de credenciais RTSP.

Resultado da última validação:

```text
14 passed
```

## Limitações atuais

Ainda não existe IA nesta etapa. Isso é intencional.

Ainda não há transmissão de vídeo contínuo para a Cloud. A arquitetura atual prioriza saúde, configuração e dados operacionais.

Câmeras RTSP locais com IP `192.168.x.x` só abrem no computador/servidor que está na mesma rede delas. A Vercel não consegue acessar essas câmeras diretamente.

O painel Cloud ainda precisa evoluir para exibir de forma completa as métricas e eventos sincronizados pelo Node.

## Para onde queremos chegar

O objetivo é transformar o CAMPEX em uma plataforma híbrida robusta:

1. a Cloud será o centro de configuração, gestão e inteligência do negócio;
2. o Node será o agente local confiável, sempre ligado na empresa;
3. as câmeras continuarão dentro da rede local;
4. os dados úteis irão para a Cloud com segurança;
5. a IA será adicionada no Node quando a base operacional estiver estável.

Próximas fases recomendadas:

### Fase 5: Painel Cloud de telemetria

Exibir no frontend:

- status de cada Node;
- status de cada câmera por Node;
- fila pendente;
- último heartbeat;
- métricas recentes;
- eventos de troca de status.

### Fase 6: Instalador e operação 24/7

Preparar distribuição do Node como aplicativo baixável:

- empacotamento para macOS/Windows;
- inicialização automática;
- modo Windows Service ou systemd;
- tela local simples para pareamento e diagnóstico.

### Fase 7: pipeline local de visão

Adicionar uma camada de processamento local, ainda com regras simples antes de IA pesada:

- amostragem de frames;
- detecção de movimento;
- captura de evidências pontuais;
- envio de eventos com snapshots seguros quando necessário.

### Fase 8: IA local

Depois da base estável, introduzir modelos de visão:

- detecção de pessoas/objetos;
- zonas de interesse;
- permanência em área;
- contagem operacional;
- regras por cliente.

## Direção técnica

A decisão central é manter o CAMPEX Node leve, modular e resiliente. Ele deve funcionar mesmo com internet instável e nunca depender do painel aberto no navegador.

A CAMPEX Cloud deve comandar configuração, autenticação e visualização. O Node deve executar a parte local, guardar dados temporariamente e sincronizar quando possível.

Essa separação é o caminho correto para clientes com câmeras IP privadas, ambientes industriais, lojas, condomínios, galpões e redes fechadas.
