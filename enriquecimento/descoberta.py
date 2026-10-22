"""
DESCOBERTA DE CNPJ (nome -> candidatos)
=====================================================================
Procura candidatos a CNPJ do proponente/startup em páginas de busca
públicas (cnpj.biz, buscador web). Esta etapa só GERA CANDIDATOS: o
texto da página é varrido atrás de números com dígito verificador válido.
Quem decide se o candidato é mesmo da pessoa/startup é a identidade.py,
conferindo o cadastro oficial. Assim, mudanças no layout dos sites não
quebram a lógica, e um homônimo nunca é atribuído por engano.

Boas práticas embutidas (não são opcionais):
  - respeita o robots.txt de cada site;
  - 1 requisição a cada ~2,5 s por site;
  - resultados guardados em cache (data/cache_cnpj.json);
  - se o site bloquear (captcha, 403/429, "verifique que você é humano"),
    o código registra e SEGUE EM FRENTE. Não tenta burlar bloqueio.
Os termos de uso de cada site podem restringir acesso automatizado;
confira antes de usar em produção. Para o hackathon/PoC, mantenha o
volume baixo. Para ajustar as fontes, edite FONTES_BUSCA.
=====================================================================
"""

from __future__ import annotations

import logging
import re
import time
from urllib import robotparser
from urllib.parse import quote, unquote, urlparse

import requests

from extracao.validacao import cnpj_valido, limpar_cnpj

log = logging.getLogger("descoberta")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Cada fonte: nome, URL com {termo}, método ("navegador" = Playwright; "http" = requests),
# aspas (busca frase exata) e sufixo extra na consulta. CONFIRME as URLs no navegador:
# sites mudam. Para desligar uma fonte, apague/comente o dicionário.
FONTES_BUSCA = (
    {"nome": "cnpj.biz", "url": "https://cnpj.biz/procura/{termo}", "metodo": "navegador",
     "aspas": False, "sufixo": ""},
    {"nome": "yahoo", "url": "https://br.search.yahoo.com/search?p={termo}", "metodo": "http",
     "aspas": True, "sufixo": " CNPJ"},
)

MARCADORES_BLOQUEIO = (
    "captcha", "just a moment", "cf-chl", "attention required", "access denied",
    "unusual traffic", "verifique que você é humano", "verify you are human",
)
_RE_FORMATADO = re.compile(
    r"(?<![0-9A-Za-z])[0-9A-Z]{2}\.[0-9A-Z]{3}\.[0-9A-Z]{3}/[0-9A-Z]{4}-\d{2}(?![0-9A-Za-z])")
_RE_CORRIDO = re.compile(r"(?<!\d)\d{14}(?!\d)")


def extrair_cnpjs(texto: str | None) -> list[str]:
    """CNPJs válidos (dígito verificador) em ordem de aparição, sem repetir."""
    texto = unquote(unquote(texto or ""))
    achados = sorted(
        [(m.start(), m.group()) for m in _RE_FORMATADO.finditer(texto)]
        + [(m.start(), m.group()) for m in _RE_CORRIDO.finditer(texto)]
    )
    saida: list[str] = []
    for _, bruto in achados:
        c = limpar_cnpj(bruto)
        if cnpj_valido(c) and c not in saida:
            saida.append(c)
    return saida


def bloqueado(status: int, html: str) -> bool:
    if status in (401, 403, 429, 503):
        return True
    amostra = (html or "")[:6000].lower()
    return any(m in amostra for m in MARCADORES_BLOQUEIO)


class Buscador:
    """Faz as buscas respeitando robots.txt e intervalo. Use como `with Buscador() as b:`."""

    def __init__(self, fontes=FONTES_BUSCA, intervalo: float = 2.5, max_por_fonte: int = 8):
        self.fontes = fontes
        self.intervalo = intervalo
        self.max_por_fonte = max_por_fonte
        self._robots: dict[str, robotparser.RobotFileParser] = {}
        self._ultimo: dict[str, float] = {}
        self._pw = self._nav = self._ctx = None
        self._navegador_indisponivel = False

    # ----- ciclo de vida -----
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.fechar()

    def fechar(self) -> None:
        for obj in (self._nav, self._pw):
            try:
                if obj:
                    obj.close() if obj is self._nav else obj.stop()
            except Exception:  # noqa: BLE001  (limpeza: nunca derrubar o pipeline)
                pass
        self._pw = self._nav = self._ctx = None

    # ----- cortesia com os sites -----
    def _permitido(self, url: str) -> bool:
        p = urlparse(url)
        rp = self._robots.get(p.netloc)
        if rp is None:
            rp = robotparser.RobotFileParser()
            try:
                r = requests.get(f"{p.scheme}://{p.netloc}/robots.txt", timeout=8,
                                 headers={"User-Agent": USER_AGENT})
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                else:
                    rp.allow_all = True
            except requests.RequestException:
                rp.allow_all = True
            self._robots[p.netloc] = rp
        return rp.can_fetch("*", url)

    def _esperar(self, host: str) -> None:
        decorrido = time.monotonic() - self._ultimo.get(host, 0.0)
        falta = self.intervalo - decorrido
        if falta > 0:
            time.sleep(falta)
        self._ultimo[host] = time.monotonic()

    # ----- download -----
    def _html_navegador(self, url: str) -> tuple[int, str]:
        if self._navegador_indisponivel:
            raise RuntimeError("navegador indisponível")
        if self._pw is None:
            try:
                from playwright.sync_api import sync_playwright
                self._pw = sync_playwright().start()
                self._nav = self._pw.chromium.launch(headless=True)
                self._ctx = self._nav.new_context(user_agent=USER_AGENT)
            except Exception as e:  # noqa: BLE001
                self._navegador_indisponivel = True
                self.fechar()
                raise RuntimeError(
                    f"Chromium indisponível ({type(e).__name__}); rode: python -m playwright install chromium") from e
        pagina = self._ctx.new_page()
        try:
            resp = pagina.goto(url, wait_until="domcontentloaded", timeout=25_000)
            try:
                pagina.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:  # noqa: BLE001  (páginas que nunca ficam ociosas)
                pass
            return (resp.status if resp else 0), pagina.content()
        finally:
            pagina.close()

    def _html_http(self, url: str) -> tuple[int, str]:
        r = requests.get(url, timeout=20, headers={"User-Agent": USER_AGENT, "Accept-Language": "pt-BR,pt;q=0.9"})
        return r.status_code, r.text

    # ----- busca -----
    def buscar(self, termo: str) -> tuple[list[str], list[str]]:
        """Devolve (cnpjs_candidatos, diagnóstico_por_fonte)."""
        cnpjs: list[str] = []
        diag: list[str] = []
        for f in self.fontes:
            consulta = (f'"{termo}"' if f.get("aspas") else termo) + f.get("sufixo", "")
            url = f["url"].format(termo=quote(consulta))
            host = urlparse(url).netloc
            if not self._permitido(url):
                diag.append(f"{f['nome']}: robots.txt não permite; pulado")
                continue
            self._esperar(host)
            try:
                status, html = (self._html_navegador(url) if f["metodo"] == "navegador"
                                else self._html_http(url))
            except Exception as e:  # noqa: BLE001  (rede, timeout, navegador)
                diag.append(f"{f['nome']}: falhou ({type(e).__name__}: {str(e)[:80]})")
                continue
            if bloqueado(status, html):
                diag.append(f"{f['nome']}: bloqueado/anti-bot (HTTP {status}); não tento contornar")
                continue
            achados = extrair_cnpjs(html)[: self.max_por_fonte]
            diag.append(f"{f['nome']}: HTTP {status}, {len(achados)} CNPJs na página")
            for c in achados:
                if c not in cnpjs:
                    cnpjs.append(c)
        return cnpjs, diag