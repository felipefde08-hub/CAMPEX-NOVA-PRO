# Passo a passo CAMPEX Node

Este guia descreve como testar, gerar e executar o CAMPEX Node como aplicativo Windows.

## 1. Objetivo

O CAMPEX Node roda localmente no computador do cliente para:

- parear com a CAMPEX Cloud;
- manter credenciais do Node e configuracoes locais em disco persistente;
- capturar cameras RTSP;
- manter workers de camera com reconexao automatica;
- enviar heartbeat, metricas e eventos para a Cloud;
- manter fila local quando a internet ou a Cloud estiver indisponivel;
- continuar funcionando sem depender do dashboard/browser.

## 2. Onde os dados ficam no Windows

O executavel usa uma pasta persistente do usuario:

```powershell
%LOCALAPPDATA%\CAMPEX\node
```

Conteudo principal:

- `node.sqlite3`: banco local com pareamento, fila, cache de cameras e metadados;
- `logs\campex-node.log`: logs persistentes rotativos;
- `logs\campex-node-bootstrap.log`: log de inicializacao do executavel;
- `node_id`: identidade local quando aplicavel.

Tokens, senhas e dados de pareamento nao ficam embutidos no executavel.

## 3. Modo desenvolvimento

Instalar dependencias especificas do Node:

```powershell
python -m pip install -r campex_node\requirements.txt
```

Rodar a interface local:

```powershell
python -m campex_node.desktop_launcher --host 127.0.0.1 --port 8787
```

Rodar sem abrir navegador:

```powershell
python -m campex_node.desktop_launcher --no-browser --host 127.0.0.1 --port 8787
```

Validar inicializacao rapida:

```powershell
python -m campex_node.main --once
```

## 4. Build do executavel Windows

Na raiz do projeto:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
```

Resultado esperado:

```text
dist\CampexNode\CampexNode.exe
```

O build usa PyInstaller em modo `onedir`, sem console visivel.

## 5. Executar o CAMPEX Node

Abrir manualmente:

```powershell
dist\CampexNode\CampexNode.exe
```

Executar em background:

```powershell
dist\CampexNode\CampexNode.exe --no-browser --host 127.0.0.1 --port 8787
```

A interface local fica em:

```text
http://127.0.0.1:8787
```

Em desenvolvimento, prefira iniciar com o script abaixo para evitar erro por Node antigo ainda preso na porta `8787`:

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\windows\start_node_local.ps1
```

Ele encerra qualquer processo antigo escutando a porta `8787` e sobe o Node atual do repositorio.

## 5.1. Usar o frontend local com o Node

Enquanto nao houver backend Cloud, use o frontend da pasta `frontend/` apontando para o Node local.

1. Inicie o Node:

```powershell
dist\CampexNode\CampexNode.exe --no-browser --host 127.0.0.1 --port 8787
```

Ou, durante o desenvolvimento:

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\windows\start_node_local.ps1
```

2. Abra `frontend/index.html` pelo Live Server.

3. O arquivo `frontend/config.js` ja usa, em localhost:

```text
http://127.0.0.1:8787/api
```

4. A tela de cameras do frontend passa a conversar com o Node local para:

- verificar health;
- listar cameras;
- adicionar camera RTSP;
- testar fonte RTSP;
- ativar/desativar camera;
- consultar health;
- abrir snapshot/stream quando houver frame.

Se o navegador ainda tentar chamar `campexback.vercel.app`, limpe o `localStorage` do site no DevTools ou abra com:

```text
http://127.0.0.1:5500/frontend/index.html?api=http://127.0.0.1:8787/api
```

No modo local sem CAMPEX Cloud, o codigo gerado na tela do Node nao autoriza nada na nuvem. Ele serve apenas para manter o fluxo preparado para quando a Cloud voltar. Para testar cameras agora, mantenha o Node rodando em `127.0.0.1:8787` e use o frontend local apontando para `http://127.0.0.1:8787/api`.

Na tela `Configuracoes > Nodes` do frontend local, o botao `Adicionar Node` chama o proprio Node local em `/api/nodes/pair/request`. Se aparecer erro de CORS ou `422`, pare o Node antigo, rode `git pull`, reinicie o Node e, se estiver usando executavel, gere o build novamente.

## 6. Instalar como tarefa agendada

Depois de gerar o executavel:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\install_task.ps1
```

Remover a tarefa:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\uninstall_task.ps1
```

A tarefa usa o executavel se ele existir em `dist\CampexNode\CampexNode.exe`.
Caso contrario, pode usar o Python de desenvolvimento informado pelo parametro `-Python`.

## 7. Pareamento com CAMPEX Cloud

1. Abra o CAMPEX Node.
2. Acesse a aba de sincronizacao/pareamento.
3. Informe a URL da CAMPEX Cloud.
4. Gere o codigo local ou use o codigo legado.
5. Autorize o Node no dashboard Cloud.
6. Confirme que a tela mostra:
   - Node pareado;
   - Node ID;
   - organizacao;
   - status da conexao;
   - ultima sincronizacao.

Depois do pareamento, o Node salva localmente `node_id`, token do Node, organizacao e URL da Cloud.

## 8. Cameras RTSP

Na aba de cameras:

1. Informe nome da camera.
2. Informe URL RTSP.
3. Clique em testar conexao.
4. Se o teste passar, salve e ative.
5. O worker local inicia e tenta reconectar automaticamente se o stream cair.

As cameras vindas da Cloud tambem sao cacheadas localmente para permitir reinicio em modo offline.

## 9. Funcionamento offline

Quando a Cloud ou internet ficam indisponiveis:

- heartbeats nao entregues entram na fila local;
- metricas e eventos permanecem pendentes;
- o processamento local continua;
- quando a conexao volta, o SyncService tenta reenviar.

## 10. Checklist manual

- Abrir `CAMPEX-Node.exe` sem console.
- Confirmar UI local em `http://127.0.0.1:8787`.
- Confirmar criacao de `%LOCALAPPDATA%\CAMPEX\node`.
- Confirmar logs em `%LOCALAPPDATA%\CAMPEX\node\logs`.
- Parear com CAMPEX Cloud.
- Fechar e abrir novamente; confirmar que o pareamento persistiu.
- Adicionar camera RTSP.
- Testar conexao da camera.
- Ativar camera e verificar online/offline.
- Derrubar a internet e verificar fila local.
- Restaurar internet e verificar sincronizacao.
- Fechar o navegador e confirmar que o processo continua se iniciado em background.
- Instalar Scheduled Task e testar reinicio/login do Windows.

## 11. Testes automatizados usados

```powershell
python -m compileall campex_node packaging\windows
python -m pytest tests\campex_node -q
python -m campex_node.main --once
```

Smoke test do executavel:

```powershell
dist\CampexNode\CampexNode.exe --no-browser --host 127.0.0.1 --port 8799
Invoke-RestMethod http://127.0.0.1:8799/api/status
```
