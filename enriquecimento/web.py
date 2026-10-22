"""
ACESSO WEB EDUCADO (usado pela busca de CNPJ e pela coleta de contatos)
=====================================================================
Regras desta camada:
  - respeita o robots.txt de cada site (se proíbe, não acessa);
  - pausa entre requisições ao mesmo domínio;
  - cache em disco (data/cache_web): a mesma página não é baixada duas vezes;
  - NÃO contorna bloqueios: se o site mostrar captcha, "Cloudflare" ou devolver
    403/429, o motivo é registrado e a fonte é pulada;
  - NÃO faz login nem usa conta de terceiros: o que está atrás de "cadastre-se"
    fica como informação indisponível.

Sites de consulta de CNPJ costumam restringir acesso automatizado nos termos de
uso. Por isso esta camada é opcional (--sem-busca-web no pipeline) e tudo que
ela encontra é só CANDIDATO: a confirmação final vem sempre da BrasilAPI.
=====================================================================
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.robotparser
from pathlib import Path
from urllib.parse import urlparse

import requests

log = logging.getLogger("web")

RAIZ = Path(__file__).resolve().parent.parent
DIR_CACHE = RAIZ / "data" / "cache_web"

USER_AGENT = "LeadQualifierPoC/1.0 (projeto academico de empresa junior; uso moderado)"
PAUSA_DOMINIO = 3.0
TAMANHO_MAX = 600_000

_ultimo_acesso: dict[str, float] = {}
_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

SINAIS_BLOQUEIO = ("captcha", "just a moment", "attention required", "cf-chl", "access denied")

# Resultados de busca que NÃO são o site da própria empresa
DOMINIOS_IGNORADOS = (
    "cnpj.biz", "casadosdados.com.br", "cnpja.com", "econodata.com.br", "empresaqui.com.br",
    "cnpj.info", "consultasocio.com", "linkedin.com", "facebook.com", "instagram.com",
    "youtube.com", "twitter.com", "x.com", "wikipedia.org", "jusbrasil.com.br",
    "reclameaqui.com.br", "google.com", "gov.br", "programacentelha.com.br",
    "tiktok.com", "medium.com", "glassdoor.com.br", "apontador.com.br",
)


def dominio(url: str) -> str:
    return (urlparse(url).netloc or "").lower().removeprefix("www.")


def _caminho_cache(url: str) -> Path:
    return DIR_CACHE / (hashlib.sha1(url.encode("utf-8")).hexdigest() + ".json")


def permitido(url: str) -> bool:
    """Consulta o robots.txt (uma vez por domínio). Sem robots.txt (404) = permitido;
    erro de rede ou 5xx = por cautela, não acessa."""
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = requests.get(base + "/robots.txt", timeout=6, headers={"User-Agent": USER_AGENT})
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
                _robots[base] = rp
            elif 400 <= r.status_code < 500:
                _robots[base] = None  # sem robots.txt
            else:
                _robots[base] = rp
                rp.disallow_all = True
        except requests.RequestException:
            _robots[base] = rp
            rp.disallow_all = True
    rp = _robots[base]
    return True if rp is None else rp.can_fetch(USER_AGENT, url)


def baixar(url: str, usar_cache: bool = True, timeout: int = 12) -> tuple[str | None, str]:
    """Devolve (html, motivo). motivo: 'ok', 'cache', 'robots', 'bloqueado', 'erro'."""
    cache = _caminho_cache(url)
    if usar_cache and cache.exists():
        try:
            d = json.loads(cache.read_text(encoding="utf-8"))
            return d.get("html"), "cache" if d.get("html") else d.get("motivo", "erro")
        except (json.JSONDecodeError, OSError):
            pass

    if not permitido(url):
        log.info("robots.txt não permite: %s", url)
        return None, "robots"

    dom = dominio(url)
    espera = _ultimo_acesso.get(dom, 0) + PAUSA_DOMINIO - time.monotonic()
    if espera > 0:
        time.sleep(espera)
    _ultimo_acesso[dom] = time.monotonic()

    html, motivo = None, "erro"
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT, "Accept-Language": "pt-BR,pt;q=0.9"})
        if r.status_code in (403, 429, 503):
            motivo = "bloqueado"
        elif r.status_code == 200:
            texto = r.text[:TAMANHO_MAX]
            if any(s in texto[:4000].lower() for s in SINAIS_BLOQUEIO) and len(texto) < 20_000:
                motivo = "bloqueado"
            else:
                html, motivo = texto, "ok"
        else:
            motivo = "erro"
    except requests.RequestException as e:
        log.info("Falha ao baixar %s: %s", url, e)

    try:
        DIR_CACHE.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"url": url, "html": html, "motivo": motivo}), encoding="utf-8")
    except OSError:
        pass
    if motivo != "ok":
        log.info("%s -> %s", url, motivo)
    return html, motivo


def buscar_web(consulta: str, max_resultados: int = 8, pausa: float = 2.0) -> list[dict]:
    """Busca gratuita (sem chave) via DuckDuckGo. Devolve [{'title','href','body'}].
    Falhas (limite, rede, pacote ausente) devolvem lista vazia, nunca exceção."""
    cache = _caminho_cache("busca:" + consulta)
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))["resultados"]
        except (json.JSONDecodeError, OSError, KeyError):
            pass
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS  # nome antigo do mesmo pacote
        time.sleep(pausa)
        with DDGS() as d:
            res = list(d.text(consulta, region="br-pt", max_results=max_resultados))
    except Exception as e:  # noqa: BLE001
        log.info("Busca web indisponível (%s): %s", type(e).__name__, e)
        return []
    res = [{"title": r.get("title", ""), "href": r.get("href", ""), "body": r.get("body", "")} for r in res]
    try:
        DIR_CACHE.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"resultados": res}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return res
