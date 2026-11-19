# 📡 CoH3 Brasil — Radar de Lobby Ao Vivo (`coh3-br-lobby-radar`)

Projeto dedicado e independente para monitorar **quem está jogando Company of Heroes 3 agora** ou **online na Steam pronto para convite** entre os 32 Veteranos Brasileiros monitorados — direto em um canal exclusivo do Discord.

## Arquitetura (Zero Chaves da Steam • 100% Gratuito)

1. **Presença Pública XML da Comunidade Steam (`steam_presence.py`)**:
   - Consulta `https://steamcommunity.com/profiles/<SteamID64>/?xml=1` em paralelo (~1,3s para as 33 contas) com TLS 1.2+, allowlist estrita e limite de 64 KB.
   - Detecta quem está com o **CoH3 aberto** (`AppID 1677280`, `<gameName>` ou `<stateMessage>`), quem está **Online na Steam** e extrai o avatar atualizado.
2. **Enriquecimento Oficial na API da Relic (`RelicActivityClient`)**:
   - Cruza com `getRecentMatchHistoryByProfileId` e `getPersonalStat` para indicar quem **acabou de sair de uma partida** (`<= 15 min` = provável menu/fila!) e quem jogou nos últimos **35 min** mesmo com Steam privada/invisível.
3. **Painel Vivo Fixo + Aviso Curto Auto-Limpante (`card_renderer.py`, `discord_lobby.py`, `lobby_radar.py`)**:
   - **Painel Vivo Fixo**: editado in-place (`PATCH`) no canal do Discord apenas quando há mudança real de presença, acompanhado de links clicáveis para abrir o perfil Steam com 1 clique.
   - **Aviso Curto**: enviado quando `>= 2` veteranos estão com o CoH3 aberto (respeitando *cooldown* de 60 min) e **apagado automaticamente após 2 horas**, preservando sempre o Painel Vivo Fixo.

## Configuração no GitHub Actions

Em **`Settings -> Secrets and variables -> Actions`**, adicione o secret:
- **`DISCORD_LOBBY_WEBHOOK_URL`**: URL do Webhook do canal de Lobby no Discord.
