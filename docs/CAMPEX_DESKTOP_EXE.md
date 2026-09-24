# CAMPEX Desktop / .exe

O caminho recomendado e manter o produto como aplicacao local primeiro e
empacotar o CAMPEX Node como `.exe` depois. O aplicativo sobe:

- app local de pareamento/diagnostico em `http://127.0.0.1:8787`;
- servico local 24/7 do CAMPEX Node;
- runtime local com RTSP/ONVIF, captura de frames, IA, SQLite e evidencias temporarias no PC.

Arquitetura alvo:

```text
Cameras da empresa -> CAMPEX Node (.exe/servico) -> CAMPEX Cloud/API -> Painel Web
```

O painel web deve falar com a Cloud. O Node deve fazer conexoes de saida para a
Cloud, evitando portas abertas no cliente e funcionando atras de NAT/firewall.

## Arquitetura para muitos PCs

Cada instalacao Windows deve ser independente. O aplicativo roda no PC do
cliente e grava dados operacionais no perfil do usuario:

```text
%LOCALAPPDATA%\CAMPEX\
├── campex.sqlite3
├── evidence\
├── video_uploads\
└── node\
    ├── node.sqlite3
    └── node_id
```

O executavel pode ser atualizado ou reinstalado sem apagar esse diretorio. Isso
e importante para operacao em dezenas ou centenas de computadores.

Banco local recomendado:

- `campex.sqlite3`: cameras, zonas, regras, eventos, investigacoes e
  configuracoes do backend visual.
- `node\node.sqlite3`: identidade do Node, fila de sincronizacao, heartbeats e
  telemetria local.
- Evidencias, imagens e videos ficam em pastas locais; o SQLite guarda apenas
  caminhos e metadados.

Esse desenho permite rodar offline, manter as cameras dentro da rede local e
sincronizar com a nuvem depois, quando existir conectividade.

## Backup e fechamento mensal

Para instalacoes em muitos PCs, o CAMPEX nao deve acumular dados infinitamente.
O fechamento mensal recomendado e:

1. Gerar backup consistente do SQLite.
2. Gerar relatorio mensal em JSON e Markdown.
3. Compactar evidencias do periodo em `.zip`.
4. Manter configuracoes operacionais: cameras, zonas, regras, maquinas e
   preferencias.
5. Limpar dados transacionais do periodo apenas depois do backup: eventos,
   investigacoes, analises, entregas, metricas e transicoes.
6. Rodar `VACUUM` no SQLite para recuperar espaco em disco.

Arquivos gerados:

```text
%LOCALAPPDATA%\CAMPEX\archives\YYYY-MM\
├── campex-YYYY-MM.sqlite3
├── report-YYYY-MM.json
├── report-YYYY-MM.md
└── evidence-YYYY-MM.zip
```

Comando local para validar sem apagar:

```powershell
python scripts\archive_month.py --desktop --year 2026 --month 9
```

Comando local para arquivar e limpar o periodo:

```powershell
python scripts\archive_month.py --desktop --year 2026 --month 9 --purge
```

No `.exe`, esse mesmo fluxo deve ser chamado pelo botao de fechamento mensal e,
depois, por uma tarefa agendada do Windows criada pelo instalador.

## O que o .exe deve fazer

1. Validar portas locais livres.
2. Inicializar ou migrar o SQLite.
3. Subir o backend local de captura/YOLO.
4. Servir a interface local ou abrir uma janela WebView.
5. Abrir a tela do operador.
6. Manter logs e dados em `%LOCALAPPDATA%\CAMPEX`.
7. Encerrar os processos internos ao fechar o aplicativo.

## Rodar a versao web local

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install rfdetr==1.10.1 supervision==0.30.2 trackers==2.6.0
.\.venv\Scripts\python.exe scripts\run_desktop_web.py
```

Abra `http://127.0.0.1:5174`. O launcher tambem tenta abrir o navegador
automaticamente.

## Gerar o .exe experimental

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\.venv\Scripts\python.exe -m PyInstaller packaging\windows\CAMPEX-Desktop.spec
```

Saida esperada:

```text
dist\CAMPEX-Desktop.exe
```

## Observacoes

- Esta primeira versao do `.exe` e um launcher com janela de console. Ela e boa
  para validar dependencias, portas, YOLO, cameras e permissao de rede.
- Depois da validacao, o proximo passo e trocar a abertura no navegador por uma
  janela nativa com WebView, mantendo o backend igual.
- Se a interface estiver publicada na Vercel, o PC instalado pode rodar apenas
  `python scripts/run_local_connector.py` ou o futuro `.exe` de connector.
- Tokens e senhas ficam no `.env` local, que nao deve ser versionado.
