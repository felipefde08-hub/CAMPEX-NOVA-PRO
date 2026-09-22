# CAMPEX - Deploy na Vercel

Este projeto deve ser publicado como dois projetos Vercel separados:

- CAMPEX Backend/API: raiz do repositório.
- CAMPEX Frontend: diretório `frontend/`.

## Backend

1. Crie um novo projeto na Vercel.
2. Conecte o repositório CAMPEX.
3. Configure `Root Directory` como a raiz do repositório.
4. Use `Framework Preset` como FastAPI, Python ou Other.
5. Não configure build command manual.
6. Garanta que o arquivo `vercel.json` da raiz seja usado.
7. Configure as variáveis de ambiente listadas abaixo.
8. Faça o deploy.
9. Teste primeiro:

```text
https://SEU-BACKEND.vercel.app/api/v1/health
```

O entrypoint de produção é `api/index.py`, que exporta `app` de `backend.main`.

### Variáveis Recomendadas Do Backend

| Nome | Finalidade | Obrigatória |
| --- | --- | --- |
| `CAMPEX_RUNTIME` | Define modo serverless. Use `serverless` na Vercel. | Sim |
| `CAMPEX_ENV` | Ambiente lógico. Use `production`. | Sim |
| `CAMPEX_SERVICE_NAME` | Nome retornado no health check. | Não |
| `CAMPEX_VERSION` | Versão retornada no health check. | Não |
| `CAMPEX_LOG_LEVEL` | Nível de log. | Não |
| `CAMPEX_FRONTEND_ORIGINS` | Allowlist CORS, separada por vírgulas. | Sim |
| `CAMPEX_API_TOKEN` | Token para proteger rotas `/api/v1`, exceto health. | Recomendado |
| `DATABASE_URL` | Banco atual. SQLite é temporário em serverless. | Não para MVP |
| `NVIDIA_API_KEY` | Chave Nemotron/NVIDIA. | Só para IA |
| `NEMOTRON_BASE_URL` | URL base NVIDIA. | Não |
| `NEMOTRON_MODEL` | Modelo Nemotron. | Não |
| `NEMOTRON_TIMEOUT_SECONDS` | Timeout da chamada IA. | Não |
| `TELEGRAM_BOT_TOKEN` | Bot Telegram. | Só para Telegram |
| `SMTP_HOST` | Servidor SMTP. | Só para e-mail |
| `SMTP_PORT` | Porta SMTP. | Só para e-mail |
| `SMTP_USERNAME` | Usuário SMTP. | Só para e-mail |
| `SMTP_PASSWORD` | Senha SMTP. | Só para e-mail |
| `SMTP_FROM_EMAIL` | Remetente SMTP. | Só para e-mail |
| `VIDEO_UPLOAD_DIR` | Diretório de upload. Use `/tmp/campex_video_uploads` na Vercel. | Não |
| `VIDEO_MAX_UPLOAD_MB` | Limite de upload MP4. | Não |

Nunca coloque valores reais de segredo em `.env.example`.

## Frontend

1. Crie um segundo projeto na Vercel.
2. Conecte o mesmo repositório.
3. Configure `Root Directory` como `frontend`.
4. Use `Framework Preset` como Other/Static.
5. Não configure build command.
6. Configure a URL pública do backend em `frontend/config.js` antes do deploy:

```js
window.CAMPEX_API_BASE_URL = "https://SEU-BACKEND.vercel.app/api/v1";
```

7. Faça o deploy.
8. Abra o frontend e confira o status do backend no topo da interface.

Também é possível testar temporariamente com query string:

```text
https://SEU-FRONTEND.vercel.app/?api=https://SEU-BACKEND.vercel.app/api/v1
```

## Teste De Comunicação

1. Abra `https://SEU-BACKEND.vercel.app/api/v1/health`.
2. Deve responder JSON com `status: "ok"`.
3. Configure `CAMPEX_FRONTEND_ORIGINS` no backend com a URL exata do frontend, por exemplo:

```text
https://SEU-FRONTEND.vercel.app,http://127.0.0.1:5500,http://localhost:5500
```

4. Configure `frontend/config.js` com a URL do backend.
5. Refaça deploy de frontend e backend.
6. Abra o frontend e confirme que o status mostra o backend online.

## Limitações Na Vercel

- SQLite em `/tmp` funciona apenas como armazenamento efêmero. Não há persistência permanente entre instâncias, regiões ou redeploys.
- Uploads MP4 em `/tmp` também são efêmeros.
- Processamento pesado de vídeo pode estourar timeout, CPU, memória ou limite de payload.
- Em `CAMPEX_RUNTIME=serverless`, a análise MP4 roda dentro da própria requisição; não depende de thread em background depois da resposta.
- Câmeras RTSP, webcams, loops contínuos e workers permanentes não são apropriados para Vercel Serverless.
- A API sobe em modo serverless sem iniciar câmeras, stream RTSP, detector YOLO/RF-DETR ou workers de visão.
- As dependências pesadas `ultralytics`, `rfdetr`, `supervision` e `trackers` não ficam em `requirements.txt` de produção para reduzir risco de build. Instale-as em ambiente local/worker quando precisar de visão completa.
- Nemotron, Telegram e SMTP são chamados apenas quando endpoints/serviços específicos forem usados; falhas dessas integrações não devem impedir o health check.

## Infraestrutura Posterior Recomendada

- Banco persistente: Postgres gerenciado, Neon, Supabase ou outro serviço externo.
- Arquivos e vídeos: object storage, como Vercel Blob, S3 ou equivalente.
- Processamento de vídeo pesado: worker dedicado, fila, container ou VM com CPU/GPU.
- RTSP/câmeras contínuas: edge worker local, serviço persistente ou máquina na rede da fábrica.
