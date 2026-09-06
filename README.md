# morpho-monitor (v2)

Robô que roda no GitHub Actions e avisa no Telegram sobre o caso **Alpha USDC Delta V2 / Main Street (msY)**.

## O que ele vigia

| Fonte | O que dispara alerta |
|---|---|
| Vault Alpha USDC Delta V2 (API Morpho) | qualquer liquidez sacável ≥ `MIN_LIQUIDITY_USD` (hoje o vault está zerado — a posição foi baixada) |
| Mercado msY/USDC na Morpho (`0xb317d1…`) | liquidez aparece, utilização sai de 100%, bad debt realizado muda |
| Preço do msY e do msUSD | variação ≥ `PRICE_MOVE_PCT` (padrão 10%) — recompra da Main Street tende a aparecer aqui |
| X: @0xAlphaping e @Main_St_Finance | post novo; se falar de claim / snapshot / redemption / recovery vem marcado como 🚨 IMPORTANTE |
| Google News | notícia nova sobre AlphaPing / Main Street / msY / msUSD |
| Heartbeat | resumo diário com os números acima |

## Configuração

1. Secrets (*Settings → Secrets and variables → Actions*): `BOT_TOKEN` e `CHAT_ID`.
2. Opcional, em *Variables*: `MIN_LIQUIDITY_USD`, `PRICE_MOVE_PCT`, `NITTER_INSTANCES` (lista separada por vírgula).
3. **Não** apague o `state.json` — é a memória do que já foi avisado.
4. Para testar: *Actions → Monitor Vault Morpho → Run workflow*.

## O que mudou em relação à v1

- Handle da AlphaPing corrigido: era `alphaping` (conta inexistente), agora `0xAlphaping`. Por isso o `state.json` nunca teve posts da AlphaPing.
- Cron de 5 min → 30 min: a Syndication do X estava devolvendo `429 Too Many Requests` em toda rodada e o repositório acumulou ~1.400 commits só de estado.
- Plano B para o X: se a Syndication falhar, tenta espelhos Nitter (RSS).
- Monitoramento do **mercado msY/USDC** e dos **preços** (o vault em si mostra $0 e não vai voltar a ter liquidez; a devolução virá por um contrato de claim da Main Street).
- Alertas com re-aviso a cada 6 h enquanto a condição persistir, e destaque para posts com palavras-chave do processo de recuperação.
- `state.json` gravado com chaves ordenadas → menos commits inúteis.
- Variáveis vazias não quebram mais o script (`float("")`).

## Uso local

```bash
export BOT_TOKEN=... CHAT_ID=...
python monitor.py --status   # resumo sem enviar nada
python monitor.py --test     # testa o Telegram
python monitor.py --once     # uma checagem completa
```
