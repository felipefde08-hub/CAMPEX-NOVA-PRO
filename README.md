# CAMPEX

CAMPEX e um MVP de operacao visual: transforma cameras, webcams ou videos locais em sensores operacionais com deteccao de pessoas, tracking, zonas, eventos, evidencias e revisao pelo operador.

## Fluxo principal

```text
Camera/video
-> Detection
-> Tracking
-> Zona
-> Evento
-> Evidencia
-> Revisao operacional
```

O caso principal do MVP e detectar uma pessoa entrando em uma zona restrita.

## Rodar o projeto

```bash
./start.sh
```

O script cria/atualiza o ambiente, instala dependencias, inicializa o SQLite e sobe:

- Backend: `http://127.0.0.1:8000`
- Frontend: `http://127.0.0.1:5174`

No Windows, para validar o caminho do futuro `.exe` com interface web local:

```powershell
.\.venv\Scripts\python.exe scripts\run_desktop_web.py
```

Esse launcher sobe o backend local com captura/visao em `127.0.0.1:8000`,
serve a interface em `127.0.0.1:5174` e abre o navegador. Instrucoes de
empacotamento estao em `docs/CAMPEX_DESKTOP_EXE.md`.

Portas customizadas:

```bash
CAMPEX_BACKEND_PORT=8001 CAMPEX_FRONTEND_PORT=5175 ./start.sh
```

## Como testar com video local

1. Abra `http://127.0.0.1:5174`.
2. Entre em `Câmeras` e cadastre um `video_file`, por exemplo `palace.mp4`.
3. Entre em `Áreas & zonas`.
4. Selecione a câmera/video e desenhe uma zona com pelo menos 3 pontos.
5. Use tipo `Restrita` para gerar evento crítico.
6. Entre em `Ao vivo` e ligue `Vision: ON`.
7. Abra `Eventos` para revisar ocorrências e evidências.

## Funcionalidades atuais

- CRUD de câmeras.
- Fontes `webcam`, `video_file` e `rtsp`.
- Stream MJPEG com overlay.
- RF-DETR com fallback configurado.
- Tracking por câmera.
- Mapeamento/pose quando o modelo estiver disponível.
- Desenho visual de zonas sobre vídeo/câmera.
- Eventos estruturados por regras determinísticas.
- Evidência automática por evento: frame limpo, overlay e metadata.
- Investigações persistidas no SQLite.
- Painel operacional, eventos, zonas, regras, evidências, diagnóstico e configurações.

## Endpoints principais

```text
GET    /api/v1/health
GET    /api/v1/cameras
POST   /api/v1/cameras
POST   /api/v1/cameras/{id}/vision/start
POST   /api/v1/cameras/{id}/vision/stop
GET    /api/v1/cameras/{id}/stream
GET    /api/v1/events
GET    /api/v1/events/{id}
GET    /api/v1/events/{id}/evidence
GET    /api/v1/zones
POST   /api/v1/zones
GET    /api/v1/operations/summary
GET    /api/v1/operations/diagnostics
GET    /api/v1/investigations
```

## Testes

```bash
.venv/bin/python -m pytest tests --maxfail=1 -q
node --check frontend/js/app.js
node --check frontend/js/api.js
```

## Observações

- RTSP precisa ser validado com câmera real no ambiente alvo.
- O primeiro uso de modelos de IA pode baixar pesos e demorar.
- `VISION_VIDEO_LOOP=true` e videos locais são úteis para desenvolvimento.
- Evidências ficam em `storage/evidence/`.

### Autenticação da API no navegador

Configure `CAMPEXTOKEN` no ambiente do backend. O nome antigo
`CAMPEX_API_TOKEN` continua aceito para compatibilidade. No `.env` local,
que é ignorado pelo Git, use o mesmo valor e reinicie o serviço. Em Configurações do frontend,
preencha **Token API (somente nesta aba)** com o mesmo valor e salve.
As requisições JSON, exclusões e uploads enviam `X-CAMPEX-Token`.
O token fica no `sessionStorage` da aba e é removido ao sair da conta;
credenciais antigas no `localStorage` são migradas e apagadas.
O login local do frontend não substitui a autenticação da API.

Não coloque o segredo em JavaScript público, parâmetros de URL ou variáveis
públicas de build. Use HTTPS em produção. Configure `CAMPEX_FRONTEND_ORIGINS`
com a origem exata do frontend. O preflight CORS não exige token; as chamadas
protegidas continuam retornando 401 quando o token está ausente ou incorreto.
`/api/v1/health` permanece público e a autenticação continua opcional quando
nenhum dos dois nomes de token está definido. Tokens de organização são independentes.

### Interface na Vercel com câmeras da rede local

O backend serverless não alcança endereços privados como `192.168.x.x` e,
neste projeto, não executa a captura contínua. Mantenha a interface na Vercel
e execute o conector no computador da mesma rede das câmeras:

```bash
python scripts/run_local_connector.py
```

Use o Python do ambiente virtual com as dependências de `requirements.txt`.
O conector escuta somente em `127.0.0.1:8000` e utiliza `CAMPEXTOKEN` ou
`CAMPEX_API_TOKEN` do ambiente ou `.env`. Se estiver ausente, gera um segredo e salva no `.env`
ignorado pelo Git, sem mostrá-lo nos logs.

Na interface da Vercel, abra **Configurações → Conexão com as câmeras**,
selecione **Neste computador**, informe o token do `.env` local e clique em
**Testar e conectar**. Permita o acesso à rede local quando o navegador pedir.
O computador precisa ficar ligado e o navegador deve estar nesse computador.
O backend da nuvem continua disponível na outra opção. Os dados pertencem ao
backend selecionado; não há sincronização automática entre as duas bases.

Imagens, vídeos e eventos ao vivo passam por um service worker da interface,
que adiciona `X-CAMPEX-Token` ao pedido ao backend. O segredo não vai na URL,
nem é gravado pelo worker. O frontend deve publicar também `media-worker.js`
e `js/media-auth.js`. Use HTTPS (ou localhost para desenvolvimento).

Verificação opcional em Chrome com Playwright instalado:
`node scripts/check_media_browser.mjs`.
