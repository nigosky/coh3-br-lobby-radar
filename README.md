# 📡 CoH3 Brasil — Radar de Lobby Ao Vivo (`coh3-br-lobby-radar`)

Projeto dedicado e independente para monitorar **quem está jogando Company of Heroes 3 agora**, **online na Steam pronto para convite** ou **invisível / Steam privada ativo recentemente** em duas categorias separadas no Discord:
- **Veteranos (sempre acima)**: os 46 jogadores (46 contas independentes) monitorados no tracker de partidas — **sempre exibidos integralmente**, independente de ELO ou atividade recente.
- **Outros Brasileiros (sempre abaixo)**: somente jogadores brasileiros **ativos no CoH3** (`country == "br"` com partida no CoH3 nos últimos **30 dias**, descartando automaticamente inativos), com **destaque (`★` / `⭐`) para maior ELO (em qualquer modo ou facção) + maior atividade**, e **baixa evidência compacta/discreta** para ELO mais baixo e pouca atividade.

### Convenção de Nomes para Contas Smurf (`players_roster.json`)
- **Smurf com nick diferente da conta principal**: `<NomeDaSmurf> (<NomeOriginal>)` com `pin_alias: true` (ex.: `Charles Bronson (Chacineiro)`, `Shadow (Alekel)`).
- **Múltiplas contas com o mesmo nome**: `<NomeOriginal> (Principal)`, `<NomeOriginal> (Smurf 1)`, `<NomeOriginal> (Smurf 2)`... com `pin_alias: true` (ex.: as 3 contas do `General Winter`).

## Arquitetura (Zero Chaves da Steam • 100% Gratuito)

1. **Presença Pública XML da Comunidade Steam (`steam_presence.py`)**:
   - Consulta `https://steamcommunity.com/profiles/<SteamID64>/?xml=1&l=english&_=<ts>` com TLS 1.2+, allowlist estrita, `Cache-Control: no-cache`, proteção anti-XXE/DTD, limite de 64 KB e *circuit-breaker* anti-429 para os demais brasileiros (preservando prioridade total aos Veteranos).
   - Lê exclusivamente os nós diretos `<profile>` e `<profile>/<inGameInfo>` (ignorando `<mostPlayedGames>` e resumos livres), exigindo `privacyState == "public"`, `visibilityState == "3"` e consistência com `<stateMessage>` para eliminar falsos positivos de jogadores offline.
   - Registra diagnóstico leve de visibilidade Steam dos Veteranos (`veteranos_steam_privacy` em `state.json`) para identificar perfis privados, somente amigos ou com detalhes de jogos ocultos.
2. **Descoberta Automática da Comunidade BR + Enriquecimento na API da Relic (`RelicActivityClient`)**:
   - Atualiza automaticamente `br_community.json` a cada 12h via **COH3 Stats Open Data** (`storage.coh3stats.com`), fallback na **Relic API América do Sul** (`getLeaderboard2`) e descoberta passiva de participantes brasileiros (`country == "br"`) nas partidas dos Veteranos.
   - Detecta jogadores **invisíveis / Steam privada** ativos nos últimos **35 min** cruzando `getRecentMatchHistoryByProfileId` e `getPersonalStat` em lote (`lastmatchdate`).
3. **Painel Vivo em Três Mensagens (2 Cards Horizontais Grandes + Links Verticais) (`card_renderer.py`, `discord_lobby.py`, `lobby_radar.py`)**:
   - **Painel Vivo Separado (Atualizado a cada 5 min)**: publica **três mensagens separadas** no Discord (`POST`) e **apaga automaticamente o conjunto anterior** (`DELETE`):
     1. **Mensagem 1 — Card Gráfico Grande `Veteranos` (`1100px`, proporção horizontal)**: anexo PNG puro (sem `embed`) ocupando a largura máxima de exibição do Discord (`~550px`), exibindo **todos os Veteranos** sem corte.
     2. **Mensagem 2 — Card Gráfico Grande `Outros Brasileiros` (`1100px`, proporção horizontal)**: anexo PNG puro (sem `embed`) em largura máxima no Discord (`~525–550px`), com destaque dourado `★` para ELO alto + ativos e faixa discreta ao final para baixa evidência.
     3. **Mensagem 3 — Links Diretos da Steam (Listas Verticais)**: dois blocos verticais com links clicáveis dos perfis Steam (**Veteranos** acima sem corte e **Outros Brasileiros** abaixo com `⭐` no topo e subtexto `-#` para baixa evidência).
   - **Aviso Curto**: enviado quando `>= 2` Veteranos estão com o CoH3 aberto (respeitando *cooldown* de 60 min) e **apagado automaticamente após 2 horas**, preservando sempre todas as mensagens do Painel Vivo Fixo.

## Configuração no GitHub Actions

Em **`Settings -> Secrets and variables -> Actions`**, adicione o secret:
- **`DISCORD_LOBBY_WEBHOOK_URL`**: URL do Webhook do canal de Lobby no Discord.

### Motor Autônomo Anti-Falha de Cron (`Self-Relay` 100% Seguro)

Para contornar atrasos ou descartes silenciosos do agendador `schedule` interno do GitHub Actions, [`.github/workflows/radar_sync.yml`](.github/workflows/radar_sync.yml) opera com **Cadeia Autônoma Endurecida (`Self-Relay`)**:
- Cada sessão executa **12 ciclos exatos de 5 minutos** (`60 min`) dentro do runner e, ao finalizar, salva o `state.json` e auto-agenda a próxima sessão via `workflow_dispatch` usando apenas o **`GITHUB_TOKEN` efêmero** (zero Personal Access Tokens extras).
- **Travas de Segurança em 1º Lugar:**
  - `concurrency` exclusivo (`cancel-in-progress: false`) + checagem via API antes do re-disparo garantem **no máximo 1 instância ativa** (impossível empilhar ou multiplicar jobs).
  - Escopo estrito a `github.repository == 'nigosky/coh3-br-lobby-radar'` e `refs/heads/main`, com `actions/checkout` fixado por SHA imutável e zero scripts/pip de terceiros.
  - **Kill-switch imediato:** para pausar a cadeia autônoma sem editar código, basta cancelar a execução em andamento na aba *Actions* (passos com `!cancelled()`) ou definir a variável de repositório `RADAR_AUTO_RELAY_ENABLED=false`.
