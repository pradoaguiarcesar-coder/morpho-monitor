#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
  MONITOR DO VAULT MORPHO (versao GitHub Actions / nuvem)
  Vault: Alpha USDC Delta V2  (curador: AlphaPing)
================================================================================
Roda na NUVEM do GitHub (de graca), independente do seu PC estar ligado.

A cada execucao (agendada pelo GitHub a cada ~5 min) ele faz UMA checagem:
  1) LIQUIDEZ do vault via API oficial da Morpho -> avisa no Telegram assim que
     aparecer qualquer valor sacavel (o campo 'liquidity' deixar de ser 0).
  2) NOTICIAS sobre o vault / AlphaPing / Main Street (msY/msUSD) via Google News.
  3) X / Twitter de @alphaping e @Main_St_Finance (melhor esforco).

O estado (o que ja foi avisado) e salvo em 'state.json', que o proprio workflow
do GitHub regrava no repositorio. Assim ele nao repete alertas entre execucoes.

SEGREDOS: o token do bot e o seu chat_id NAO ficam no codigo. Eles vem das
variaveis de ambiente BOT_TOKEN e CHAT_ID, que voce cadastra em
  GitHub -> repo -> Settings -> Secrets and variables -> Actions.

Tambem roda localmente:
  python monitor.py --once     (uma checagem)
  python monitor.py --whoami   (descobre seu chat_id)
  python monitor.py --test     (manda mensagem de teste)
  python monitor.py            (loop continuo, se quiser rodar no PC)
================================================================================
"""

import json
import os
import re
import sys
import time
import html
import urllib.parse
import urllib.request
from datetime import datetime

# ==============================================================================
#  CONFIG
# ==============================================================================
# Segredos vem do ambiente (GitHub Secrets). Localmente, voce pode exportar as
# variaveis OU colar os valores entre as aspas abaixo (so para teste local).
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip() or ""
CHAT_ID   = os.environ.get("CHAT_ID", "").strip() or ""

# Vault (Alpha USDC Delta V2 na Ethereum)
VAULT_ADDRESS = "0x0bF0164D17469241B6E086dA4016DCc54FEAA334"
VAULT_CHAIN_ID = 1
MORPHO_API = "https://blue-api.morpho.org/graphql"

# Avisa quando a liquidez sacavel (USD) for >= este valor (1.0 = qualquer liquidez)
MIN_LIQUIDITY_USD = float(os.environ.get("MIN_LIQUIDITY_USD", "1.0"))

# Re-alerta se a liquidez continuar aberta (horas)
REALERT_HOURS = 6
# Sinal de vida (horas). Tambem serve para manter o repo ativo. 0 = desliga.
HEARTBEAT_HOURS = 24

MONITOR_NEWS = True
MONITOR_X = True

NEWS_QUERIES = [
    "AlphaPing Morpho vault",
    "Alpha USDC Delta Morpho",
    "Main Street Finance msY",
    "Main Street msUSD",
    "msY token Morpho",
]
X_HANDLES = ["alphaping", "Main_St_Finance"]

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

HTTP_TIMEOUT = 25
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# ==============================================================================
#  Utilidades
# ==============================================================================

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def http_get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def http_post_json(url, payload, headers=None):
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log(f"AVISO: nao consegui salvar o estado: {e}")


def fmt_usd(v):
    try:
        return "${:,.2f}".format(float(v))
    except Exception:
        return str(v)


def now_ts():
    return time.time()


# ==============================================================================
#  Telegram
# ==============================================================================

def tg_send(text, disable_preview=True):
    if not BOT_TOKEN or not CHAT_ID:
        log("ERRO: BOT_TOKEN/CHAT_ID ausentes (configure os Secrets).")
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": disable_preview}
    try:
        resp = http_post_json(url, payload)
        if not resp.get("ok"):
            log(f"Telegram recusou: {resp}")
            return False
        return True
    except Exception as e:
        log(f"Falha ao enviar Telegram: {e}")
        return False


def tg_whoami():
    if not BOT_TOKEN:
        print("Defina BOT_TOKEN (variavel de ambiente) primeiro.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    try:
        resp = http_post_json(url, {})
    except Exception as e:
        print(f"Erro ao falar com o Telegram: {e}")
        return
    seen = {}
    for upd in resp.get("result", []):
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            seen[chat["id"]] = chat.get("username") or chat.get("first_name") or ""
    if not seen:
        print("Nenhuma conversa. Mande /start pro seu bot e rode de novo.")
        return
    print("\nChat(s) encontrado(s):")
    for cid, who in seen.items():
        print(f"   CHAT_ID = {cid}   ({who})")
    print()


# ==============================================================================
#  1) Liquidez do vault
# ==============================================================================

VAULT_QUERY = """query($a:String!,$c:Int!){
  vaultV2ByAddress(address:$a, chainId:$c){
    name symbol asset { symbol decimals }
    totalAssets totalAssetsUsd
    idleAssets idleAssetsUsd
    liquidity liquidityUsd
    forceDeallocatableLiquidity forceDeallocatableLiquidityUsd
    sharePrice netApy
  }
}"""


def fetch_vault():
    resp = http_post_json(MORPHO_API, {
        "query": VAULT_QUERY,
        "variables": {"a": VAULT_ADDRESS, "c": VAULT_CHAIN_ID},
    })
    if "errors" in resp and resp["errors"]:
        raise RuntimeError(f"API Morpho: {resp['errors']}")
    return resp["data"]["vaultV2ByAddress"]


def vault_withdrawable_usd(v):
    liq = float(v.get("liquidityUsd") or 0)
    force = float(v.get("forceDeallocatableLiquidityUsd") or 0)
    return liq, force


def check_vault(state):
    try:
        v = fetch_vault()
    except Exception as e:
        log(f"Vault: erro ao consultar API ({e})")
        return
    liq, force = vault_withdrawable_usd(v)
    actionable = max(liq, force)
    log(f"Vault liquidez: sacavel={fmt_usd(liq)} | forcavel={fmt_usd(force)} "
        f"| total={fmt_usd(v.get('totalAssetsUsd'))}")
    vs = state.setdefault("vault", {"alerted": False, "last_alert_ts": 0})
    if actionable >= MIN_LIQUIDITY_USD:
        last = vs.get("last_alert_ts", 0)
        if (not vs.get("alerted")) or (now_ts() - last >= REALERT_HOURS * 3600):
            kind = "sacavel" if liq >= MIN_LIQUIDITY_USD else "desalocavel (force deallocate)"
            msg = ("\U0001F7E2 <b>LIQUIDEZ NO VAULT!</b>\n"
                   f"<b>{html.escape(v.get('name',''))}</b>\n\n"
                   f"Liquidez {kind}: <b>{fmt_usd(actionable)}</b>\n"
                   f"  • Sacavel agora: {fmt_usd(liq)}\n"
                   f"  • Forcavel: {fmt_usd(force)}\n"
                   f"Total no vault: {fmt_usd(v.get('totalAssetsUsd'))}\n\n"
                   "\U0001F449 Va sacar/reduzir sua posicao:\n"
                   f"https://app.morpho.org/ethereum/vault/{VAULT_ADDRESS}")
            if tg_send(msg):
                log(">>> ALERTA DE LIQUIDEZ enviado.")
                vs["alerted"] = True
                vs["last_alert_ts"] = now_ts()
    else:
        if vs.get("alerted"):
            log("Liquidez voltou a ~0. Alerta rearmado.")
        vs["alerted"] = False


# ==============================================================================
#  2) Noticias (Google News RSS)
# ==============================================================================

def google_news_url(query):
    q = urllib.parse.quote(query)
    return f"https://news.google.com/rss/search?q={q}&hl=pt-BR&gl=BR&ceid=BR:pt"


def parse_rss(xml_text):
    items = []
    for block in re.findall(r"<item>(.*?)</item>", xml_text, re.S | re.I):
        def grab(tag):
            m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.S | re.I)
            if not m:
                return ""
            txt = m.group(1)
            cd = re.search(r"<!\[CDATA\[(.*?)\]\]>", txt, re.S)
            if cd:
                txt = cd.group(1)
            return html.unescape(re.sub(r"<[^>]+>", "", txt)).strip()
        link = grab("link")
        guid = grab("guid") or link
        items.append({"id": guid, "title": grab("title"), "link": link,
                      "source": grab("source"), "published": grab("pubDate")})
    return items


def check_news(state):
    seen_set = set(state.get("news_seen", []))
    new_items = []
    for query in NEWS_QUERIES:
        try:
            xml = http_get(google_news_url(query))
            for it in parse_rss(xml):
                if it["id"] and it["id"] not in seen_set:
                    seen_set.add(it["id"])
                    new_items.append(it)
        except Exception as e:
            log(f"Noticias '{query}': erro ({e})")
    if not state.get("news_initialized"):
        state["news_initialized"] = True
        state["news_seen"] = list(seen_set)[-500:]
        log(f"Noticias: base inicial memorizada ({len(seen_set)} itens).")
        return
    for it in new_items[:8]:
        src = f" — {html.escape(it['source'])}" if it.get("source") else ""
        msg = (f"\U0001F4F0 <b>Noticia nova</b>\n{html.escape(it['title'])}{src}\n{it['link']}")
        if tg_send(msg, disable_preview=False):
            log(f">>> Noticia enviada: {it['title'][:70]}")
    state["news_seen"] = list(seen_set)[-500:]


# ==============================================================================
#  3) X / Twitter (melhor esforco)
# ==============================================================================

def fetch_x_posts(handle):
    url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}"
    html_text = http_get(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html_text, re.S)
    if not m:
        return []
    data = json.loads(m.group(1))
    found = {}

    def walk(obj):
        if isinstance(obj, dict):
            idv = obj.get("id_str") or obj.get("rest_id")
            txt = obj.get("full_text") or obj.get("text")
            if idv and txt and isinstance(txt, str):
                found[str(idv)] = txt
            for val in obj.values():
                walk(val)
        elif isinstance(obj, list):
            for val in obj:
                walk(val)
    walk(data)
    return [{"id": k, "text": v} for k, v in found.items()]


def check_x(state):
    seen = state.setdefault("x_seen", {})
    inited = state.setdefault("x_initialized", {})
    for handle in X_HANDLES:
        try:
            posts = fetch_x_posts(handle)
        except Exception as e:
            log(f"X @{handle}: indisponivel agora ({e}).")
            continue
        if not posts:
            log(f"X @{handle}: sem posts legiveis.")
            continue
        seen_ids = set(seen.get(handle, []))
        new_posts = [p for p in posts if p["id"] not in seen_ids]
        for p in posts:
            seen_ids.add(p["id"])
        seen[handle] = list(seen_ids)[-300:]
        if not inited.get(handle):
            inited[handle] = True
            log(f"X @{handle}: base inicial memorizada ({len(posts)} posts).")
            continue
        for p in new_posts[:5]:
            preview = p["text"].strip().replace("\n", " ")
            if len(preview) > 280:
                preview = preview[:277] + "..."
            msg = (f"\U0001F426 <b>@{html.escape(handle)} postou no X</b>\n"
                   f"{html.escape(preview)}\nhttps://x.com/{handle}/status/{p['id']}")
            if tg_send(msg, disable_preview=False):
                log(f">>> Post de @{handle} enviado.")


# ==============================================================================
#  Heartbeat + ciclo
# ==============================================================================

def maybe_heartbeat(state):
    if HEARTBEAT_HOURS <= 0:
        return
    if now_ts() - state.get("last_heartbeat_ts", 0) < HEARTBEAT_HOURS * 3600:
        return
    try:
        v = fetch_vault()
        liq, force = vault_withdrawable_usd(v)
        tg_send("✅ <b>Monitor ativo (nuvem)</b>\n"
                f"Liquidez sacavel agora: {fmt_usd(liq)} (forcavel: {fmt_usd(force)})\n"
                "Te aviso assim que mudar.")
    except Exception as e:
        tg_send("✅ Monitor ativo (nuvem).")
        log(f"Heartbeat sem dados: {e}")
    state["last_heartbeat_ts"] = now_ts()


def cloud_once(state):
    """Uma checagem completa, usada pelo GitHub Actions (--once)."""
    # mensagem de inicio: so na primeira execucao de todas
    if not state.get("cloud_started"):
        try:
            v = fetch_vault()
            liq, force = vault_withdrawable_usd(v)
            tg_send("\U0001F916 <b>Monitor na nuvem ligado</b>\n"
                    f"Vigiando <b>{html.escape(v.get('name',''))}</b> 24/7 pelo GitHub.\n"
                    f"Liquidez sacavel agora: <b>{fmt_usd(liq)}</b>.\n"
                    "Te aviso aqui quando mudar ou sair novidade.")
        except Exception as e:
            log(f"Init sem dados do vault: {e}")
            tg_send("\U0001F916 Monitor na nuvem ligado.")
        state["cloud_started"] = True
        state["last_heartbeat_ts"] = now_ts()
    check_vault(state)
    if MONITOR_NEWS:
        check_news(state)
    if MONITOR_X:
        check_x(state)
    maybe_heartbeat(state)


def run_once(state):
    check_vault(state)
    if MONITOR_NEWS:
        check_news(state)
    if MONITOR_X:
        check_x(state)


def main_loop():
    state = load_state()
    log("Monitor (loop local) iniciado.")
    POLL = 60
    while True:
        try:
            run_once(state)
            maybe_heartbeat(state)
            save_state(state)
        except Exception as e:
            log(f"Erro no ciclo (sigo rodando): {e}")
        time.sleep(POLL)


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--whoami":
        tg_whoami()
    elif arg == "--test":
        ok = tg_send("✅ Teste do monitor: se recebeu isto, o Telegram esta OK.")
        print("Mensagem enviada." if ok else "Falhou. Confira BOT_TOKEN/CHAT_ID.")
    elif arg == "--once":
        if not BOT_TOKEN or not CHAT_ID:
            print("ERRO: defina os Secrets BOT_TOKEN e CHAT_ID no GitHub.")
            sys.exit(1)
        state = load_state()
        cloud_once(state)
        save_state(state)
        print("Checagem unica concluida.")
    elif arg in ("-h", "--help"):
        print(__doc__)
    else:
        try:
            main_loop()
        except KeyboardInterrupt:
            print("\nEncerrado.")


if __name__ == "__main__":
    main()
