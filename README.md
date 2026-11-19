# 📡 CoH3 Brasil — Radar de Lobby Ao Vivo (`coh3-br-lobby-radar`)

Projeto dedicado e independente para monitorar **quem está jogando Company of Heroes 3 agora** ou **online na Steam pronto para convite** entre os 32 Veteranos Brasileiros monitorados — direto em um canal exclusivo do Discord.

## Arquitetura (Zero Chaves da Steam • 100% Gratuito)

1. **Presença Pública XML da Comunidade Steam (`steam_presence.py`)**:
   - Consulta `https://steamcommunity.com/profiles/<SteamID64>/?xml=1` em paralelo (~1,3s para as 33 contas) com TLS 1.2+, allowlist estrita e limite de 64 KB.
   - Detecta quem está com o **CoH3 aberto** (`AppID 1677280`, `<gameName>` ou `<stateMessage>`), quem está **Online na Steam** e extrai o avatar atualizado.
2. **Enriquecimento Oficial na API da Relic (`RelicActivityClient`)**:
   - Cruza com `getRecentMatchHistoryByProfileId` e `getPersonalStat` para indicar quem **acabou de sair de uma partida** (`<= 15 min` = provável menu/fila!) e quem jogou nos últimos **35 min** mesmo com Steam privada/invisível.
3. **Painel Vivo Fixo + Aviso Curto Auto-Limpante (`card_renderer.py`, `discord_lobby.py`, `lobby_radar.py`)**:
   - **Painel Vivo Sempre Limpo**: a cada mudança real de presença (ou a cada 15 min), renderiza uma nova imagem PNG (`lobby_radar_<timestamp>.png`), envia uma **nova mensagem** no canal do Discord (`POST`) e **apaga automaticamente a mensagem antiga** do painel (`DELETE`), evitando cache visual do Discord e mantendo o canal limpo.
   - **Aviso Curto**: enviado quando `>= 2` veteranos estão com o CoH3 aberto (respeitando *cooldown* de 60 min) e **apagado automaticamente após 2 horas**, preservando sempre o Painel Vivo Fixo.

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
