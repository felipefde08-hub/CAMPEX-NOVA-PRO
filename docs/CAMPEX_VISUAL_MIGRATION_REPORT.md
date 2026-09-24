# CAMPEX Visual Migration Report

Data: 2026-09-24

## Inventario

| Pagina | Produto | Antes | Depois | Status |
| --- | --- | --- | --- | --- |
| Login | Cloud | Auth com estilos antigos/parciais | CSS alinhado ao novo sistema, card minimo, inputs e botoes padronizados | MIGRADA |
| Painel | Cloud | `ops-toolbar`, `settings-grid`, `ops-grid` antigos | Componentes antigos remapeados para tokens, paineis e listas do DS | MIGRADA |
| Ao vivo | Cloud | Controles e tiles com padroes proprios | Tiles, hover controls, filtros e botoes normalizados por DS | MIGRADA |
| Mural | Cloud | Videowall com toolbar antiga | Grid e tiles normalizados, foco em video | MIGRADA |
| Ativos | Cloud | Lista/modal de maquinas com visual antigo | Listas, forms, modal e badges normalizados | MIGRADA |
| Eventos | Cloud | Feed antigo em linhas/cards | Feed com `cx-list`, tabs e drawer lateral | MIGRADA |
| Indicadores | Cloud | Grid antigo de metricas | Metricas, graficos/listas e paineis normalizados | MIGRADA |
| Relatorios / Analise MP4 | Cloud | Layout antigo com grids e paineis | Grids, player, metricas e estados normalizados | MIGRADA |
| Investigacoes | Cloud | Formulario e lista antigos | `ops-*` migrado pela camada de DS | MIGRADA |
| Cameras | Cloud | Cards/lista, wizard e modal antigos | Listas, modal, stepper, inputs e acoes normalizados | MIGRADA |
| Areas & Zonas | Cloud | Designer com `ops-grid` antigo | Paineis, lista, ferramentas e forms normalizados | MIGRADA |
| Automacoes | Cloud | Formulario tecnico e modal antigo | Lista, builder/form, modal e badges normalizados | MIGRADA |
| Nodes | Cloud | Hero/card grande e pareamento inline | Lista operacional, metricas inline e drawer de pareamento | MIGRADA |
| Diagnostico | Cloud | Grid tecnico antigo | Metricas, codigo mono e paineis normalizados | MIGRADA |
| Configuracoes | Cloud | Cards e tabs heterogeneos | Tabs, cards, forms, callouts, listas e runtime normalizados | MIGRADA |
| Visao geral | Node | Dashboard inicial parcial | Mantida e alinhada a tokens/spacing do Node | MIGRADA |
| Cameras | Node | Grade abstrata | Lista/grade sem video bruto, sem senha RTSP | MIGRADA |
| Processamento | Node | Ausente | Nova pagina de IA local, FPS, CPU/RAM e modelos | MIGRADA |
| Eventos | Node | Lista simples | Lista operacional sem video bruto continuo | MIGRADA |
| Sincronizacao | Node | Pareamento e fila parciais | Fila, Cloud e pareamento local no padrao Node | MIGRADA |
| Diagnostico | Node | Ausente | Nova pagina de sistema, rede, banco, fila e versao | MIGRADA |
| Configuracoes | Node | Lista simples | Secoes Geral, Inicializacao, Armazenamento, Rede, Atualizacoes e Avancado | MIGRADA |
| Sobre | Node | Conteudo institucional | Mantido no padrao Node | MIGRADA |

## Componentes Refatorados

- Tokens, tipografia, cores, radius e sombras centralizados em `frontend/css/product-system.css`.
- Toolbars, paineis, cards, listas, linhas, forms, inputs, selects, textareas, badges, toasts, modais, wizard, settings tabs e empty states normalizados por uma camada unica.
- Eventos Cloud usa drawer padronizado.
- Nodes Cloud usa drawer padronizado para pareamento.
- Confirmacao destrutiva de revogar Node passou de `window.confirm` para modal do produto.
- Node recebeu paginas dedicadas de Processamento e Diagnostico.

## CSS Legado

O CSS legado ainda existe em `components.css`, `layout.css` e `global.css`, mas a camada nova foi carregada por ultimo e sobrepoe os componentes acessiveis. Remocao fisica completa do legado deve ser feita com teste visual em navegador para evitar apagar estilos ainda usados por fluxos condicionais.

## Encoding

- Corrigido mojibake global principal em `frontend/js/app.js`.
- Busca por `Ã`, `Â` e `�` nao encontrou mojibake restante relevante no frontend; o match restante e texto correto em portugues (`ATENÇÃO`).

## Responsividade e Acessibilidade

- Adicionados breakpoints para 1180px e 860px.
- Listas e grids colapsam para uma coluna.
- Modais ocupam tela cheia em mobile.
- Focus visible padronizado em inputs, selects, textareas e botoes.
- Drawers e modais usam `role="dialog"`/`aria-modal` nos novos fluxos.

## Funcionalidades Preservadas

- APIs, rotas, autenticacao, cameras, Node, Telegram, e-mail, Nemotron e banco nao foram alterados por esta refatoracao visual.
- Pareamento Node novo e legado preservados.
- Wizard de camera preservado.
- Fluxos de regras, maquinas, eventos, investigacoes e configuracoes preservados.

## Validacao

- `python -m py_compile` passou para Node UI, local app, previews e scripts novos.
- `http://127.0.0.1:8899/` respondeu 200.
- `http://127.0.0.1:8787/` respondeu 200.
- O HTML do Node preview contem `page-processing` e `page-diagnostics`.

## Pendencias

- Rodar verificacao visual real em navegador com Playwright/agent-browser quando a ferramenta estiver disponivel.
- Rodar `node --check` quando Node.js estiver instalado.
- Substituir `window.prompt` de renomear Node por modal proprio.
- Remover fisicamente CSS legado somente depois de uma varredura visual/pixel em todas as rotas.
