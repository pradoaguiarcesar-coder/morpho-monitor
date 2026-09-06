#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
  MONITOR DO VAULT MORPHO / MAIN STREET  (versao GitHub Actions / nuvem)  v2
  Vault: Alpha USDC Delta V2  (curador: AlphaPing)
================================================================================
Roda na NUVEM do GitHub (de graca), independente do seu PC estar ligado.

A cada execucao (agendada pelo GitHub) ele faz UMA checagem:
  1) VAULT Alpha USDC Delta V2 (API oficial da Morpho) -> avisa se aparecer
     qualquer liquidez sacavel.  (Hoje o vault esta zerado: a posicao no
     mercado msY foi baixada/queimada. O alerta continua armado por seguranca.)
  2) MERCADO msY/USDC na Morpho (onde o dinheiro ficou preso) -> avisa se:
       - aparecer liquidez no mercado (alguem pagou divida / entrou USDC)
       - a utilizacao sair de 100%
       - o preco do msY subir ou cair de forma relevante (recompra/buyback)
       - houver bad debt realizado
  3) NOTICIAS (Google News) sobre AlphaPing / Main Street / msY / msUSD.
  4) X / Twitter de @0xAlphaping e @Main_St_Finance, com destaque especial
     quando o post fala de claim / snapshot / resgate / recovery.
  5) HEARTBEAT diario com um resumo do mercado (pra voce saber que esta vivo).

O estado (o que ja foi avisado) e salvo em 'state.json', que o proprio workflow
do GitHub regrava no repositorio. Assim ele nao repete alertas.

SEGREDOS: o token do bot e o seu chat_id NAO ficam no codigo. Eles vem das
variaveis de ambiente BOT_TOKEN e CHAT_ID (GitHub -> Settings -> Secrets).

Uso local:
  python monitor.py --once     (uma checagem)
  python monitor.py --whoami   (descobre seu chat_id)
  python monitor.py --test     (manda mensagem de teste)
  python monitor.py --status   (imprime o resumo do mercado sem mandar nada)
  python monitor.py            (loop continuo no PC)
================================================================================
"""

import json
import os
import re
import sys
import time
import html
import random
import urllib.parse
import urllib.request
from datetime import datetime

# ==============================================================================
#  CONFIG
# ==============================================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID   = os.environ.get("CHAT_ID", "").strip()

# Vault Alpha USDC Delta V2 (Morpho Vault V2, Ethereum)
VAULT_ADDRESS  = "0x0bF0164D17469241B6E086dA4016DCc54FEAA334"
VAULT_CHAIN_ID = 1
MORPHO_API     = "https://blue-api.morpho.org/graphql"

# Mercado msY/USDC (LLTV 91.5%) onde a posicao do vault ficou presa a 100% de uso
MSY_MARKET_ID  = "0xb317d11c2bc2c0c8e6ea3c6517731cf667c86f2c716624be50319a6d32a97e8d"
MSY_ADDRESS    = "0x890A5122Aa1dA30fEC4286DE7904Ff808F0bd74A"
MSUSD_ADDRESS  = "0xab5eB14c09D416F0aC63661E57EDB7AEcDb9BEfA"

# Limiares de alerta
MIN_LIQUIDITY_USD   = float(os.environ.get("MIN_LIQUIDITY_USD") or "1000")  # liquidez minima (vault ou mercado)
PRICE_MOVE_PCT      = float(os.environ.get("PRICE_MOVE_PCT") or "10")       # variacao do msY que gera alerta (%)
UTIL_ALERT_BELOW    = 0.995                                                # utilizacao abaixo disso = saiu de 100%

REALERT_HOURS   = 6    # re-alerta enquanto a condicao continuar
HEARTBEAT_HOURS = 24   # resumo diario (0 desliga)

MONITOR_NEWS = True
MONITOR_X    = True

NEWS_QUERIES = [
    "AlphaPing Morpho vault",
    "Alpha USDC Delta Morpho",
    "Main Street Finance msY",
    "Main Street msUSD",
    "msY token Morpho",
]

# ATENCAO: o handle certo da AlphaPing e 0xAlphaping (nao existe @alphaping)
X_HANDLES = ["0xAlphaping", "Main_St_Finance"]

# Palavras que marcam um post como "importante" (recebe destaque no Telegram)
X_HOT_WORDS = re.compile(
    r"claim|snapshot|redemption|redeem|recovery|distribut|buyback|repay|"
    r"morpho|msy|msusd|mainstreet|main street|resgate|devolu",
    re.I,
)

# Espelhos Nitter usados como plano B quando a Syndication do X da 429.
# Pode sobrescrever com a variavel NITTER_INSTANCES="https://a.com,https://b.com"
NITTER_INSTANCES = [
    s.strip() for s in (
        os.environ.get("NITTER_INSTANCES")
        or "https://xcancel.com,https://nitter.poast.org,https://nitter.privacydev.net"
    ).split(",") if s.strip()
]

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

HTTP_TIMEOUT = 25
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

MORPHO_VAULT_URL  = f"https://app.morpho.org/ethereum/vault/{VAULT_ADDRESS}"
MORPHO_MARKET_URL = f"https://app.morpho.org/ethereum/market/{MSY_MARKET_ID}"

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
            json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log(f"AVISO: nao consegui salvar o estado: {e}")


def fmt_usd(v):
    try:
        return "${:,.2f}".format(float(v))
    except Exception:
        return str(v)


def fmt_pct(v):
    try:
        return "{:.1f}%".format(float(v) * 100)
    except Exception:
        return str(v)


def now_ts():
    return time.time()


def should_alert(slot, condition):
    """Controla re-alertas: avisa na 1a vez e depois a cada REALERT_HOURS
    enquanto a condicao continuar. Rearma quando a condicao some."""
    if condition:
        last = slot.get("last_alert_ts", 0)
        if (not slot.get("alerted")) or (now_ts() - last >= REALERT_HOURS * 3600):
            return True
        return False
    if slot.get("alerted"):
        slot["alerted"] = False
    return False


def mark_alerted(slot):
    slot["alerted"] = True
    slot["last_alert_ts"] = now_ts()


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
#  Morpho API
# ==============================================================================

def morpho_query(query, variables):
    resp = http_post_json(MORPHO_API, {"query": query, "variables": variables})
    if resp.get("errors"):
        raise RuntimeError(f"API Morpho: {resp['errors']}")
    return resp["data"]


VAULT_QUERY = """query($a:String!,$c:Int!){
  vaultV2ByAddress(address:$a, chainId:$c){
    name symbol totalAssetsUsd liquidityUsd forceDeallocatableLiquidityUsd sharePrice
  }
}"""

MARKET_QUERY = """query($id:String!,$c:Int!){
  markets(first:1, where:{ uniqueKey_in:[$id], chainId_in:[$c] }){
    items{
      marketId listed
      state{ utilization supplyAssetsUsd borrowAssetsUsd liquidityAssetsUsd
             collateralAssetsUsd supplyApy borrowApy }
      badDebt{ usd } realizedBadDebt{ usd }
    }
  }
}"""

ASSETS_QUERY = """query($addrs:[String!]!,$c:[Int!]!){
  assets(where:{ address_in:$addrs, chainId_in:$c }){
    items{ symbol address price{ usd } }
  }
}"""


def fetch_vault():
    return morpho_query(VAULT_QUERY, {"a": VAULT_ADDRESS, "c": VAULT_CHAIN_ID})["vaultV2ByAddress"]


def fetch_market():
    items = morpho_query(MARKET_QUERY, {"id": MSY_MARKET_ID, "c": VAULT_CHAIN_ID})["markets"]["items"]
    if not items:
        raise RuntimeError("mercado msY nao encontrado na API")
    return items[0]


def fetch_prices():
    data = morpho_query(ASSETS_QUERY, {"addrs": [MSY_ADDRESS, MSUSD_ADDRESS], "c": [VAULT_CHAIN_ID]})
    out = {}
    for it in data["assets"]["items"]:
        price = (it.get("price") or {}).get("usd")
        if price is not None:
            out[it["symbol"]] = float(price)
    return out


def market_snapshot():
    """Junta vault + mercado + precos num dicionario simples (None quando falha)."""
    snap = {"vault": None, "market": None, "prices": {}}
    try:
        snap["vault"] = fetch_vault()
    except Exception as e:
        log(f"Vault: erro ao consultar API ({e})")
    try:
        snap["market"] = fetch_market()
    except Exception as e:
        log(f"Mercado msY: erro ao consultar API ({e})")
    try:
        snap["prices"] = fetch_prices()
    except Exception as e:
        log(f"Precos: erro ao consultar API ({e})")
    return snap


def summary_text(snap):
    lines = []
    v = snap.get("vault")
    if v:
        lines.append(f"<b>Vault {html.escape(v.get('name', ''))}</b>\n"
                     f"  • Sacavel: {fmt_usd(v.get('liquidityUsd') or 0)} | "
                     f"Total: {fmt_usd(v.get('totalAssetsUsd') or 0)}")
    m = snap.get("market")
    if m:
        s = m.get("state") or {}
        lines.append("<b>Mercado msY/USDC</b>\n"
                     f"  • Utilizacao: {fmt_pct(s.get('utilization') or 0)}\n"
                     f"  • Liquidez: {fmt_usd(s.get('liquidityAssetsUsd') or 0)}\n"
                     f"  • Emprestado: {fmt_usd(s.get('borrowAssetsUsd') or 0)}\n"
                     f"  • Colateral msY: {fmt_usd(s.get('collateralAssetsUsd') or 0)}\n"
                     f"  • Bad debt realizado: {fmt_usd((m.get('realizedBadDebt') or {}).get('usd') or 0)}")
    p = snap.get("prices") or {}
    if p:
        lines.append("<b>Precos</b>\n" + "\n".join(
            f"  • {html.escape(k)}: ${v:.4f}" for k, v in sorted(p.items())))
    return "\n".join(lines) if lines else "Sem dados da Morpho agora."


# ==============================================================================
#  1) Vault  +  2) Mercado msY
# ==============================================================================

def check_vault(state, snap):
    v = snap.get("vault")
    if not v:
        return
    liq = float(v.get("liquidityUsd") or 0)
    force = float(v.get("forceDeallocatableLiquidityUsd") or 0)
    actionable = max(liq, force)
    log(f"Vault: sacavel={fmt_usd(liq)} | forcavel={fmt_usd(force)} | total={fmt_usd(v.get('totalAssetsUsd') or 0)}")
    slot = state.setdefault("vault", {"alerted": False, "last_alert_ts": 0})
    if should_alert(slot, actionable >= MIN_LIQUIDITY_USD):
        kind = "sacavel" if liq >= MIN_LIQUIDITY_USD else "desalocavel (force deallocate)"
        msg = ("\U0001F7E2 <b>LIQUIDEZ NO VAULT!</b>\n"
               f"<b>{html.escape(v.get('name', ''))}</b>\n\n"
               f"Liquidez {kind}: <b>{fmt_usd(actionable)}</b>\n"
               f"  • Sacavel agora: {fmt_usd(liq)}\n"
               f"  • Forcavel: {fmt_usd(force)}\n"
               f"Total no vault: {fmt_usd(v.get('totalAssetsUsd') or 0)}\n\n"
               f"\U0001F449 Va sacar/reduzir sua posicao:\n{MORPHO_VAULT_URL}")
        if tg_send(msg):
            log(">>> ALERTA DE LIQUIDEZ (vault) enviado.")
            mark_alerted(slot)


def check_market(state, snap):
    m = snap.get("market")
    if not m:
        return
    s = m.get("state") or {}
    util = float(s.get("utilization") or 0)
    liq = float(s.get("liquidityAssetsUsd") or 0)
    borrow = float(s.get("borrowAssetsUsd") or 0)
    coll = float(s.get("collateralAssetsUsd") or 0)
    realized = float((m.get("realizedBadDebt") or {}).get("usd") or 0)
    log(f"Mercado msY: util={fmt_pct(util)} | liq={fmt_usd(liq)} | emprestado={fmt_usd(borrow)} | colateral={fmt_usd(coll)} | badDebt={fmt_usd(realized)}")

    ms = state.setdefault("market", {})

    # a) Liquidez apareceu no mercado
    slot = ms.setdefault("liquidity", {"alerted": False, "last_alert_ts": 0})
    if should_alert(slot, liq >= MIN_LIQUIDITY_USD):
        msg = ("\U0001F7E2 <b>LIQUIDEZ NO MERCADO msY/USDC!</b>\n\n"
               f"Liquidez disponivel: <b>{fmt_usd(liq)}</b>\n"
               f"Utilizacao: {fmt_pct(util)}\n"
               f"Emprestado: {fmt_usd(borrow)}\n\n"
               "Alguem pagou divida ou entrou USDC no mercado. "
               "Pode ser o inicio do processo de recuperacao.\n"
               f"{MORPHO_MARKET_URL}")
        if tg_send(msg):
            log(">>> ALERTA DE LIQUIDEZ (mercado) enviado.")
            mark_alerted(slot)

    # b) Utilizacao saiu de 100%
    slot = ms.setdefault("utilization", {"alerted": False, "last_alert_ts": 0})
    if should_alert(slot, 0 < util < UTIL_ALERT_BELOW):
        msg = ("\U0001F4C9 <b>Utilizacao do mercado msY saiu de 100%</b>\n"
               f"Agora: <b>{fmt_pct(util)}</b> | Liquidez: {fmt_usd(liq)}\n"
               f"{MORPHO_MARKET_URL}")
        if tg_send(msg):
            mark_alerted(slot)

    # c) Bad debt realizado mudou (baixa contabil da divida)
    prev_bd = ms.get("realized_bad_debt_usd")
    if prev_bd is not None and abs(realized - float(prev_bd)) >= 1000:
        msg = ("⚠️ <b>Bad debt realizado mudou no mercado msY</b>\n"
               f"Antes: {fmt_usd(prev_bd)}  ->  Agora: <b>{fmt_usd(realized)}</b>\n"
               f"{MORPHO_MARKET_URL}")
        tg_send(msg)
    ms["realized_bad_debt_usd"] = realized

    # d) Preco do msY / msUSD se moveu de forma relevante
    prices = snap.get("prices") or {}
    ref = ms.setdefault("price_ref", {})
    for sym, price in prices.items():
        base = ref.get(sym)
        if base is None:
            ref[sym] = price
            continue
        if base > 0:
            move = (price - base) / base * 100
            if abs(move) >= PRICE_MOVE_PCT:
                arrow = "\U0001F4C8" if move > 0 else "\U0001F4C9"
                msg = (f"{arrow} <b>{html.escape(sym)} moveu {move:+.1f}%</b>\n"
                       f"De ${base:.4f} para <b>${price:.4f}</b>\n"
                       "Recompra (buyback) da Main Street costuma aparecer como alta gradual do msY.")
                if tg_send(msg):
                    ref[sym] = price   # nova referencia so depois de avisar


# ==============================================================================
#  3) Noticias (Google News RSS)
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
    seen_list = list(state.get("news_seen", []))
    seen_set = set(seen_list)
    new_items = []
    for query in NEWS_QUERIES:
        try:
            xml = http_get(google_news_url(query))
            for it in parse_rss(xml):
                if it["id"] and it["id"] not in seen_set:
                    seen_set.add(it["id"])
                    seen_list.append(it["id"])
                    new_items.append(it)
        except Exception as e:
            log(f"Noticias '{query}': erro ({e})")
    if not state.get("news_initialized"):
        state["news_initialized"] = True
        state["news_seen"] = seen_list[-500:]
        log(f"Noticias: base inicial memorizada ({len(seen_set)} itens).")
        return
    for it in new_items[:8]:
        src = f" — {html.escape(it['source'])}" if it.get("source") else ""
        msg = (f"\U0001F4F0 <b>Noticia nova</b>\n{html.escape(it['title'])}{src}\n{it['link']}")
        if tg_send(msg, disable_preview=False):
            log(f">>> Noticia enviada: {it['title'][:70]}")
    state["news_seen"] = seen_list[-500:]


# ==============================================================================
#  4) X / Twitter  (Syndication -> espelhos Nitter -> desiste com log)
# ==============================================================================

def _x_from_syndication(handle):
    url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}"
    html_text = http_get(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    if "Rate limit exceeded" in html_text[:500]:
        raise RuntimeError("HTTP 429 (rate limit) na Syndication")
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html_text, re.S)
    if not m:
        return []
    data = json.loads(m.group(1))
    found = {}

    def walk(obj):
        if isinstance(obj, dict):
            idv = obj.get("id_str") or obj.get("rest_id")
            txt = obj.get("full_text") or obj.get("text")
            user = (obj.get("user") or {}).get("screen_name") if isinstance(obj.get("user"), dict) else None
            if idv and txt and isinstance(txt, str) and str(idv).isdigit():
                # ignora tweets de outras contas que aparecem citados/retuitados
                if not user or user.lower() == handle.lower():
                    found[str(idv)] = txt
            for val in obj.values():
                walk(val)
        elif isinstance(obj, list):
            for val in obj:
                walk(val)
    walk(data)
    return [{"id": k, "text": v} for k, v in found.items()]


def _x_from_nitter(handle):
    last_err = None
    for base in NITTER_INSTANCES:
        try:
            xml = http_get(f"{base.rstrip('/')}/{handle}/rss",
                           headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml,text/xml,*/*"})
            posts = []
            for it in parse_rss(xml):
                m = re.search(r"/status/(\d+)", it.get("link") or it.get("id") or "")
                if m:
                    posts.append({"id": m.group(1), "text": it["title"]})
            if posts:
                log(f"X @{handle}: lido via espelho {base}")
                return posts
        except Exception as e:
            last_err = e
            continue
    if last_err:
        raise RuntimeError(f"espelhos Nitter falharam ({last_err})")
    return []


def fetch_x_posts(handle):
    try:
        posts = _x_from_syndication(handle)
        if posts:
            return posts
    except Exception as e:
        log(f"X @{handle}: Syndication falhou ({e}); tentando espelhos...")
    time.sleep(random.uniform(0.5, 2.0))
    return _x_from_nitter(handle)


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
        # ids de tweet crescem com o tempo: guardar os maiores = os mais novos
        seen[handle] = sorted(seen_ids | {p["id"] for p in posts}, key=int)[-300:]
        if not inited.get(handle):
            inited[handle] = True
            log(f"X @{handle}: base inicial memorizada ({len(posts)} posts).")
            continue
        new_posts.sort(key=lambda p: int(p["id"]))
        for p in new_posts[-5:]:
            preview = p["text"].strip().replace("\n", " ")
            if len(preview) > 600:
                preview = preview[:597] + "..."
            hot = bool(X_HOT_WORDS.search(p["text"]))
            head = ("\U0001F6A8 <b>IMPORTANTE — @{h} postou sobre o caso</b>" if hot
                    else "\U0001F426 <b>@{h} postou no X</b>").format(h=html.escape(handle))
            msg = f"{head}\n{html.escape(preview)}\nhttps://x.com/{handle}/status/{p['id']}"
            if tg_send(msg, disable_preview=False):
                log(f">>> Post de @{handle} enviado{' (IMPORTANTE)' if hot else ''}.")


# ==============================================================================
#  5) Heartbeat + ciclo
# ==============================================================================

def maybe_heartbeat(state, snap):
    if HEARTBEAT_HOURS <= 0:
        return
    if now_ts() - state.get("last_heartbeat_ts", 0) < HEARTBEAT_HOURS * 3600:
        return
    tg_send("✅ <b>Monitor ativo (nuvem)</b> — resumo diario\n\n" + summary_text(snap) +
            "\n\nTe aviso assim que algo mudar ou sair post/noticia.")
    state["last_heartbeat_ts"] = now_ts()


def cloud_once(state):
    """Uma checagem completa, usada pelo GitHub Actions (--once)."""
    snap = market_snapshot()
    if not state.get("cloud_started_v2"):
        tg_send("\U0001F916 <b>Monitor v2 na nuvem ligado</b>\n"
                "Agora vigiando tambem o mercado msY/USDC, o preco do msY e os posts de "
                "@0xAlphaping e @Main_St_Finance.\n\n" + summary_text(snap))
        state["cloud_started_v2"] = True
        state["cloud_started"] = True
        state["last_heartbeat_ts"] = now_ts()
    check_vault(state, snap)
    check_market(state, snap)
    if MONITOR_NEWS:
        check_news(state)
    if MONITOR_X:
        check_x(state)
    maybe_heartbeat(state, snap)


def run_once(state):
    snap = market_snapshot()
    check_vault(state, snap)
    check_market(state, snap)
    if MONITOR_NEWS:
        check_news(state)
    if MONITOR_X:
        check_x(state)
    return snap


def main_loop():
    state = load_state()
    log("Monitor (loop local) iniciado.")
    POLL = 300
    while True:
        try:
            snap = run_once(state)
            maybe_heartbeat(state, snap)
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
    elif arg == "--status":
        print(re.sub(r"<[^>]+>", "", summary_text(market_snapshot())))
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
