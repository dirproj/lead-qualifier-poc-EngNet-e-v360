"""
DESCOBERTA DE CNPJ (nome -> candidatos) e contatos exibidos na página
=====================================================================
Fluxo principal (o mesmo que uma pessoa faria na Casa dos Dados):
  1. abre  https://casadosdados.com.br/solucao/cnpj?q=<NOME EXATO DO PROPONENTE>
  2. lê os links de empresa da lista (/solucao/cnpj/<razao-social>-<CNPJ>) e
     coloca no topo os que combinam com o TÍTULO DO PROJETO / nome da startup;
  3. (opcional) abre a página da empresa e lê o Telefone e o E-mail exibidos.

Se o site bloquear (anti-bot), as fontes seguintes são tentadas, em ordem:
endpoint público de pesquisa da Casa dos Dados, buscador web (pacote ddgs)
e cnpj.biz. Para no primeiro site que devolver candidatos.

Esta etapa só GERA CANDIDATOS e TEXTO. Quem decide se o candidato é mesmo da
pessoa/startup é a identidade.py, conferindo o cadastro oficial. Quem decide
se um telefone/e-mail pode ir para a planilha é a contato.py.

Boas práticas embutidas (não são opcionais):
  - respeita o robots.txt de cada site;
  - 1 requisição a cada ~2,5 s por site e cache dos resultados;
  - se o site bloquear (captcha, 403/429, "verifique que você é humano"), o
    código registra e SEGUE EM FRENTE. Não usa stealth, não resolve captcha.
    Opção legítima: NAVEGADOR_VISIVEL=1 (ou --navegador-visivel) abre o Chromium
    com janela, como um navegador comum.
Os termos de uso dos sites podem restringir acesso automatizado; confira antes
de usar além do PoC. Para ajustar as fontes, edite FONTES_BUSCA.
=====================================================================
"""

from __future__ import annotations

import logging
import os
import re
import time
from urllib import robotparser
from urllib.parse import quote, quote_plus, unquote, urlparse

import requests

from extracao.validacao import cnpj_valido, limpar_cnpj
from enriquecimento.identidade import normalizar_nome, nome_da_startup, _tokens, GENERICOS

from dotenv import load_dotenv
load_dotenv()

log = logging.getLogger("descoberta")
logging.getLogger("ddgs").setLevel(logging.WARNING)
logging.getLogger("primp").setLevel(logging.WARNING)

USER_AGENT = "LeadQualifierPoC/1.0 (projeto academico de empresa junior; uso moderado)"
TAMANHO_MAX_PAGINA = 600_000
CDD_BASE = "https://casadosdados.com.br"

FONTES_BUSCA = (
    {"nome": "casadosdados-api", "metodo": "api_casadosdados",
     "url": "https://api.casadosdados.com.br/v5/public/cnpj/pesquisa"},
    {"nome": "casadosdados-site", "metodo": "cdd_site",
     "url": CDD_BASE + "/solucao/cnpj?q={termo_plus}"},
    {"nome": "ddgs", "metodo": "ddgs"},
    {"nome": "cnpj.biz", "metodo": "navegador", "url": "https://cnpj.biz/procura/{termo}",
     "aspas": False, "sufixo": ""},
)

MARCADORES_BLOQUEIO = (
    "captcha", "just a moment", "cf-chl", "attention required", "access denied",
    "unusual traffic", "verifique que você é humano", "verify you are human",
    "bot detection",
)
_RE_FORMATADO = re.compile(
    r"(?<![0-9A-Za-z])[0-9A-Z]{2}\.[0-9A-Z]{3}\.[0-9A-Z]{3}/[0-9A-Z]{4}-\d{2}(?![0-9A-Za-z])")
_RE_CORRIDO = re.compile(r"(?<!\d)\d{14}(?!\d)")
_RE_LINK_EMPRESA = re.compile(
    r"/solucao/cnpj/([a-z0-9][a-z0-9\-]*?)-([0-9a-z]{14})(?=[\"'/?#\s<>&]|$)", re.I)
_RE_EMAIL = re.compile(r"[\w.+\-*•]+@[\w\-]+(?:\.[\w\-]+)+")
_RE_TEL = re.compile(r"(?:\(?\d{2}\)?[\s.-]*)?[\d*•xX]{4,5}[\s.-]?[\d*•X]{4}(?!\d)")
DOMINIOS_DO_SITE = ("casadosdados.com.br", "cnpj.biz", "cnpja.com")


def extrair_cnpjs(texto: str | None) -> list[str]:
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


def extrair_links_empresa(html: str | None) -> list[tuple[str, str]]:
    texto = unquote(html or "")
    vistos: dict[str, str] = {}
    for m in _RE_LINK_EMPRESA.finditer(texto):
        c = limpar_cnpj(m.group(2))
        if cnpj_valido(c) and c not in vistos:
            vistos[c] = m.group(1).lower()
    return list(vistos.items())


def extrair_contatos_texto(texto: str | None) -> dict[str, list[str]]:
    linhas = [l.strip() for l in (texto or "").splitlines() if l.strip()]
    tels: list[str] = []
    mails: list[str] = []
    for i, linha in enumerate(linhas):
        rotulo = normalizar_nome(linha.split(":")[0])[:20]
        janela = linha + " " + (linhas[i + 1] if i + 1 < len(linhas) else "")
        if rotulo.startswith(("TELEFONE", "FONE", "CELULAR", "WHATSAPP")):
            for m in _RE_TEL.finditer(janela):
                v = m.group().strip()
                if len(re.sub(r"\D", "", v)) + len(re.findall(r"[*•xX]", v)) >= 8 and v not in tels:
                    tels.append(v)
        if rotulo.startswith(("E MAIL", "EMAIL", "CORREIO")):
            for m in _RE_EMAIL.finditer(janela):
                v = m.group().strip(".").lower()
                if not v.endswith(DOMINIOS_DO_SITE) and v not in mails:
                    mails.append(v)
    return {"telefones": tels, "emails": mails}


def bloqueado(status: int, html: str) -> bool:
    if status in (401, 403, 429, 503):
        return True
    amostra = (html or "")[:6000].lower()
    return any(m in amostra for m in MARCADORES_BLOQUEIO)


def _pontos_titulo(slug: str, titulo: str | None) -> int:
    nome = nome_da_startup(titulo) or ""
    sig = [t for t in _tokens(nome) if t not in GENERICOS]
    alvo = normalizar_nome(slug.replace("-", " "))
    return sum(1 for t in sig if t in alvo)


class Buscador:
    def __init__(self, fontes=FONTES_BUSCA, intervalo: float = 2.5, max_por_fonte: int = 8,
                 uf: str = "DF", parar_no_primeiro: bool = True, visivel: bool | None = None):
        self.fontes = fontes
        self.uf = uf
        self.parar_no_primeiro = parar_no_primeiro
        self.intervalo = intervalo
        self.max_por_fonte = max_por_fonte
        self.visivel = (os.getenv("NAVEGADOR_VISIVEL", "").lower() in ("1", "true", "sim")
                        if visivel is None else visivel)
        self.slugs: dict[str, str] = {}
        self._robots: dict[str, robotparser.RobotFileParser] = {}
        self._ultimo: dict[str, float] = {}
        self._pw = self._nav = self._ctx = None
        self._navegador_indisponivel = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.fechar()

    def fechar(self) -> None:
        for obj, metodo in ((self._nav, "close"), (self._pw, "stop")):
            try:
                if obj:
                    getattr(obj, metodo)()
            except Exception:
                pass
        self._pw = self._nav = self._ctx = None

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
                elif 400 <= r.status_code < 500:
                    rp.allow_all = True
                else:
                    rp.disallow_all = True
            except requests.RequestException:
                rp.disallow_all = True
            self._robots[p.netloc] = rp
        return rp.can_fetch(USER_AGENT, url)

    def _esperar(self, host: str) -> None:
        falta = self.intervalo - (time.monotonic() - self._ultimo.get(host, 0.0))
        if falta > 0:
            time.sleep(falta)
        self._ultimo[host] = time.monotonic()

    def _html_navegador(self, url: str, esperar_seletor: str | None = None,
                        texto: bool = False) -> tuple[int, str]:
        if self._navegador_indisponivel:
            raise RuntimeError("navegador indisponível")
        if self._pw is None:
            try:
                from playwright.sync_api import sync_playwright
                self._pw = sync_playwright().start()
                self._nav = self._pw.chromium.launch(headless=not self.visivel)
                self._ctx = self._nav.new_context(user_agent=USER_AGENT, locale="pt-BR")
            except Exception as e:
                self._navegador_indisponivel = True
                self.fechar()
                raise RuntimeError(
                    f"Chromium indisponível ({type(e).__name__}); rode: python -m playwright install chromium") from e
        pagina = self._ctx.new_page()
        try:
            resp = pagina.goto(url, wait_until="domcontentloaded", timeout=25_000)
            if esperar_seletor:
                try:
                    pagina.wait_for_selector(esperar_seletor, timeout=10_000)
                except Exception:
                    pass
            try:
                pagina.wait_for_load_state("networkidle", timeout=6_000)
            except Exception:
                pass
            conteudo = pagina.inner_text("body") if texto else pagina.content()
            return (resp.status if resp else 0), conteudo[:TAMANHO_MAX_PAGINA]
        finally:
            pagina.close()

    def _html_http(self, url: str) -> tuple[int, str]:
        with requests.get(url, timeout=20, stream=True,
                          headers={"User-Agent": USER_AGENT, "Accept-Language": "pt-BR,pt;q=0.9"}) as r:
            corpo = r.raw.read(TAMANHO_MAX_PAGINA, decode_content=True)
            return r.status_code, corpo.decode(r.encoding or "utf-8", errors="replace")

    def _buscar_cdd_site(self, f: dict, termo: str, diag: list[str]) -> list[str]:
        url = f["url"].format(termo_plus=quote_plus(termo.strip()))
        host = urlparse(url).netloc
        if not self._permitido(url):
            diag.append(f"{f['nome']}: robots.txt não permite; pulado")
            return []
        self._esperar(host)
        try:
            status, html = self._html_navegador(url, esperar_seletor='a[href*="/solucao/cnpj/"]')
        except Exception as e:
            diag.append(f"{f['nome']}: falhou ({type(e).__name__}: {str(e)[:90]})")
            return []
        if bloqueado(status, html):
            diag.append(f"{f['nome']}: bloqueado/anti-bot (HTTP {status}); não tento contornar.")
            return []
        itens = extrair_links_empresa(html)
        for c, slug in itens:
            self.slugs[c] = slug
        diag.append(f"{f['nome']}: HTTP {status}, {len(itens)} empresas listadas")
        return [c for c, _ in itens][: self.max_por_fonte]

    def _buscar_casadosdados(self, f: dict, termo: str, tipo: str, diag: list[str]) -> list[str]:
        url = f["url"]
        host = urlparse(url).netloc
        nome = normalizar_nome(termo)
        if not nome:
            return []
        if not self._permitido(url):
            diag.append(f"{f['nome']}: robots.txt não permite; pulado")
            return []
        # Em vez de tentar exata e radical, use radical como padrão (mais tolerante)
        if tipo == "pessoa":
            flags, ordem = {"razao_social": True, "nome_fantasia": False, "nome_socio": True}, ("radical",)
        else:
            flags, ordem = {"razao_social": True, "nome_fantasia": True, "nome_socio": False}, ("radical",)
        
        cabecalhos = {"User-Agent": USER_AGENT, "Accept": "application/json", "api-key": os.getenv("CASADOSDADOS_API_KEY", "")}
        
        for tipo_busca in ordem:
            payload = {
                "busca_textual": [{"texto": [nome], "tipo_busca": tipo_busca, **flags}],
                "situacao_cadastral": ["ATIVA"],
                "uf": [self.uf],
                "limite": 20,
                "pagina": 1,
            }
            self._esperar(host)
            try:
                r = requests.post(url, json=payload, headers=cabecalhos, timeout=20)
            except requests.RequestException as e:
                diag.append(f"{f['nome']}: falhou ({type(e).__name__})")
                return []
            if bloqueado(r.status_code, r.text):
                diag.append(f"{f['nome']}: bloqueado/anti-bot (HTTP {r.status_code}); não tento contornar")
                return []
            if r.status_code in (400, 422):
                diag.append(f"{f['nome']}[{tipo}]: tipo_busca='{tipo_busca}' recusado "
                            f"(HTTP {r.status_code}: {r.text[:120]!r})")
                continue
            if r.status_code != 200:
                diag.append(f"{f['nome']}[{tipo}]: HTTP {r.status_code} ({r.text[:120]!r})")
                return []
            achados = extrair_cnpjs(r.text)[: self.max_por_fonte]
            diag.append(f"{f['nome']}[{tipo}/{tipo_busca}]: HTTP 200, {len(achados)} CNPJs")
            if achados:
                return achados
        return []

    def _buscar_ddgs(self, f: dict, termo: str, tipo: str, diag: list[str]) -> list[str]:
        try:
            from ddgs import DDGS
        except ImportError:
            diag.append(f"{f['nome']}: pacote 'ddgs' não instalado (pip install ddgs)")
            return []
        consulta = f'"{termo}" CNPJ' + (f" {self.uf}" if tipo == "startup" else "")
        self._esperar("ddgs")
        try:
            resultados = DDGS().text(consulta, region="br-pt", max_results=10) or []
        except Exception as e:
            diag.append(f"{f['nome']}: falhou ({type(e).__name__}: {str(e)[:80]})")
            return []
        texto = "\n".join(f"{r.get('title', '')} {r.get('href', '')} {r.get('body', '')}" for r in resultados)
        achados = extrair_cnpjs(texto)[: self.max_por_fonte]
        diag.append(f"{f['nome']}: {len(resultados)} resultados, {len(achados)} CNPJs")
        return achados

    def _buscar_pagina(self, f: dict, termo: str, diag: list[str]) -> list[str]:
        consulta = (f'"{termo}"' if f.get("aspas") else termo) + f.get("sufixo", "")
        url = f["url"].format(termo=quote(consulta))
        host = urlparse(url).netloc
        if not self._permitido(url):
            diag.append(f"{f['nome']}: robots.txt não permite; pulado")
            return []
        self._esperar(host)
        try:
            status, html = (self._html_navegador(url) if f["metodo"] == "navegador"
                            else self._html_http(url))
        except Exception as e:
            diag.append(f"{f['nome']}: falhou ({type(e).__name__}: {str(e)[:80]})")
            return []
        if bloqueado(status, html):
            diag.append(f"{f['nome']}: bloqueado/anti-bot (HTTP {status}); não tento contornar")
            return []
        achados = extrair_cnpjs(html)[: self.max_por_fonte]
        diag.append(f"{f['nome']}: HTTP {status}, {len(achados)} CNPJs na página")
        return achados

    def buscar(self, termo: str, tipo: str = "pessoa",
               titulo: str | None = None) -> tuple[list[str], list[str]]:
        cnpjs: list[str] = []
        diag: list[str] = []
        for f in self.fontes:
            m = f["metodo"]
            if m == "cdd_site":
                achados = self._buscar_cdd_site(f, termo, diag)
            elif m == "api_casadosdados":
                achados = self._buscar_casadosdados(f, termo, tipo, diag)
            elif m == "ddgs":
                achados = self._buscar_ddgs(f, termo, tipo, diag)
            else:
                achados = self._buscar_pagina(f, termo, diag)
            for c in achados:
                if c not in cnpjs:
                    cnpjs.append(c)
            if cnpjs and self.parar_no_primeiro:
                break
        if titulo and cnpjs:
            cnpjs.sort(key=lambda c: -_pontos_titulo(self.slugs.get(c, ""), titulo))
        return cnpjs, diag

    def contatos_da_pagina(self, cnpj: str) -> dict[str, list[str]] | None:
        slug = self.slugs.get(limpar_cnpj(cnpj))
        if not slug:
            return None
        url = f"{CDD_BASE}/solucao/cnpj/{slug}-{limpar_cnpj(cnpj).lower()}"
        if not self._permitido(url):
            return None
        self._esperar(urlparse(url).netloc)
        try:
            status, texto = self._html_navegador(url, esperar_seletor="body", texto=True)
        except Exception as e:
            log.warning("Página da empresa indisponível (%s): %s", type(e).__name__, str(e)[:80])
            return None
        if bloqueado(status, texto):
            log.warning("Página da empresa bloqueada (HTTP %s); não tento contornar.", status)
            return None
        return extrair_contatos_texto(texto)