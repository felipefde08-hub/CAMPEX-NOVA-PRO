# CAMPEX Node como serviço local

Esta fase prepara o CAMPEX Node para rodar 24/7 dentro da rede do cliente.

O Node deve ficar ligado mesmo quando o painel web estiver fechado. O painel Cloud continua na Vercel, mas as câmeras RTSP privadas são acessadas pelo Node local.

Arquitetura de rede alvo:

```text
Câmeras da empresa → CAMPEX Node (.exe/serviço local) → CAMPEX Cloud/API → Painel Web
```

O Painel Web conversa com a Cloud. Ele não precisa abrir conexão direta com o
`.exe` dentro da rede do cliente. O Node faz conexões HTTPS de saída para
buscar configurações e enviar eventos, métricas, alertas e snapshots pontuais.

## Fluxo de instalação alvo

1. Baixar ou copiar o projeto CAMPEX Node para o computador/servidor local.
2. Configurar Python e dependências.
3. Iniciar o aplicativo local.
4. Abrir `http://127.0.0.1:8787`.
5. Gerar um código em `Central de Nodes` na Cloud.
6. Informar o código no app local.
7. O Node fica pareado e passa a iniciar automaticamente.

## macOS

Para instalar como LaunchAgent do usuário atual:

```bash
./packaging/macos/install_launch_agent.sh
```

Para remover:

```bash
./packaging/macos/uninstall_launch_agent.sh
```

O serviço roda:

```text
python -m campex_node.main --app --host 127.0.0.1 --port 8787
```

Logs ficam em:

```text
storage/campex_node/logs/
```

## Linux systemd

Copie o projeto para `/opt/campex` ou informe outro caminho:

```bash
sudo CAMPEX_PROJECT_DIR=/opt/campex ./packaging/linux/install_systemd.sh
```

O instalador cria/usa o usuário de sistema `campex`, habilita o serviço e inicia:

```bash
sudo systemctl status campex-node
sudo journalctl -u campex-node -f
```

Template do serviço:

```text
packaging/linux/campex-node.service
```

## Windows

Em PowerShell como Administrador:

```powershell
.\packaging\windows\install_task.ps1
```

Para remover:

```powershell
.\packaging\windows\uninstall_task.ps1
```

A tarefa agendada inicia o Node no boot e reinicia em caso de falha.

## Diagnóstico local

Com o Node App rodando:

```text
GET http://127.0.0.1:8787/api/diagnostics
```

Esse endpoint retorna:

- versão;
- plataforma;
- caminho do banco local;
- se a Cloud está configurada;
- se o Node está pareado;
- tamanho da fila local;
- serviços internos ativos;
- resumo das câmeras.

Ele não retorna token do Node, token administrativo, senha RTSP ou segredo.

## Segurança

Os scripts de serviço não incluem tokens, URLs privadas ou credenciais de câmera. O pareamento deve acontecer pelo app local ou por variáveis de ambiente locais.

Arquivos que não devem ser versionados:

- `.env`;
- banco SQLite local;
- logs;
- tokens persistidos no diretório local de dados.

## Próximo passo

A próxima fase deve empacotar o Node como aplicativo baixável com identidade visual CAMPEX, removendo a necessidade de executar comandos manualmente em clientes finais.
