"""
ETAPA 1: EXTRAÇÃO (Scraping)
=====================================================================
Pega a lista REAL de aprovados de um edital (ex.: Centelha) a partir de:
  - uma página web (Playwright descobre os PDFs de resultado nela), ou
  - a URL/arquivo de um PDF, ou
  - uma tabela HTML na própria página.

As listas oficiais trazem: Nº | Título | Proponente | Temática | Município.
Elas NÃO trazem CNPJ nem resumo. O CNPJ é resolvido na etapa 2.

Como rodar (sempre a partir da RAIZ do projeto):
    pip install -r requirements.txt
    playwright install chromium

    python -m extracao.scraper --pagina "https://programacentelha.com.br/df/"
    python -m extracao.scraper --pdf "https://.../lista.pdf"      # ou caminho local
    python -m extracao.scraper --pdf data/brutos/lista.pdf --inspecionar
    # --inspecionar mostra as linhas como o parser as enxerga, para você
    # acertar as colunas com --colunas "titulo,proponente,tematica,municipio"
    # (índices; use - para coluna inexistente). Ex.: --colunas "2,0,-,3"

Mais adiante, o painel final roda com:
    streamlit run app.py        # abre em http://localhost:8501
=====================================================================
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import urlparse, unquote

import pdfplumber
import requests
from extracao.validacao import cnpj_valido, limpar_cnpj
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

log = logging.getLogger("extracao")

RAIZ = Path(__file__).resolve().parent.parent
DIR_BRUTOS = RAIZ / "data" / "brutos"

USER_AGENT = "LeadQualifierPoC/1.0 (projeto academico de empresa junior; uso moderado)"
TAMANHO_MAX_PDF = 20 * 1024 * 1024

# Posição de cada campo na linha da tabela (0 = coluna "Nº").
# AJUSTE aqui se o PDF do seu estado tiver outra ordem de colunas.
# Use None para uma coluna que não existe no PDF.
COLUNAS: dict[str, int | None] = {
    "titulo_projeto": 1, "proponente": 2, "tematica": 3, "municipio": 4,
}

# Links de resultado, por ordem de prioridade (o 1º que casar vem primeiro).
# "contratad" vem antes: projetos já contratados têm empresa constituída (CNPJ).
PALAVRAS_CHAVE_PADRAO = ("contratad", "final", "aprovad", "resultado", "lista", "homolog")
# Links ignorados (resultados parciais, editais, retificações).
EXCLUIR_PADRAO = ("prelimin", "retifica", "edital")

# CNPJ formatado (aceita letras) ou 14 dígitos corridos
REGEX_CNPJ = re.compile(
    r"\b[0-9A-Z]{2}\.[0-9A-Z]{3}\.[0-9A-Z]{3}/[0-9A-Z]{4}-\d{2}\b|\b\d{14}\b"
)
TEXTO_SETTINGS = {"vertical_strategy": "text", "horizontal_strategy": "text"}


# ---------------------------------------------------------------
# 1. MODELO DE DADOS
# ---------------------------------------------------------------
@dataclass
class Lead:
    titulo_projeto: str
    proponente: str
    tematica: str
    municipio: str
    cnpj: str | None   # quase sempre None aqui; resolvido na etapa 2
    fonte: str         # "pdf", "html" ou "demo"
    origem: str        # URL ou caminho de onde veio


def _limpar_celula(valor: str | None) -> str:
    return " ".join((valor or "").split())


def definir_colunas(spec: str) -> None:
    """spec = 'titulo,proponente,tematica,municipio' em índices; '-' = ausente."""
    partes = [p.strip() for p in spec.split(",")]
    if len(partes) != 4:
        raise ValueError("--colunas precisa de 4 valores: titulo,proponente,tematica,municipio")
    COLUNAS.update({
        campo: (None if p == "-" else int(p))
        for campo, p in zip(("titulo_projeto", "proponente", "tematica", "municipio"), partes)
    })


def _cnpj_da_linha(celulas: list[str]) -> str | None:
    """Procura na linha um CNPJ com dígito verificador válido."""
    for c in celulas:
        for achado in REGEX_CNPJ.findall(c.upper()):
            if cnpj_valido(achado):
                return limpar_cnpj(achado)
    return None


def _linhas_para_leads(linhas: list[list[str]], fonte: str, origem: str) -> list[Lead]:
    """Converte linhas de tabela (lista de células) em Leads, descartando
    cabeçalhos, rodapés e duplicatas. Vale a linha cuja 1ª célula é o Nº
    OU que contenha um CNPJ válido (listas de contratados às vezes não têm Nº)."""
    leads: list[Lead] = []
    vistos: set[tuple[str, str]] = set()
    indices = [i for i in COLUNAS.values() if i is not None]
    maior_indice = max(indices)

    for linha in linhas:
        celulas = [_limpar_celula(c) for c in linha]
        cnpj = _cnpj_da_linha(celulas)
        if len(celulas) <= maior_indice or not (celulas[0].isdigit() or cnpj):
            continue

        campos = {c: (celulas[i] if i is not None else "") for c, i in COLUNAS.items()}
        lead = Lead(**campos, cnpj=cnpj, fonte=fonte, origem=origem)
        chave = (lead.titulo_projeto.lower(), lead.proponente.lower())
        if not (lead.titulo_projeto or lead.proponente) or chave in vistos:
            continue
        vistos.add(chave)
        leads.append(lead)

    return leads


# ---------------------------------------------------------------
# 2. PDF (inclui links do Google Drive, que é onde o Centelha publica)
# ---------------------------------------------------------------
def _id_drive(url: str) -> str | None:
    if "drive.google.com" not in url:
        return None
    m = re.search(r"/file/d/([\w-]+)", url) or re.search(r"[?&]id=([\w-]+)", url)
    return m.group(1) if m else None


def _url_download(url: str) -> tuple[str, str | None]:
    """Converte link de visualização do Drive em link de download direto."""
    id_arquivo = _id_drive(url)
    if id_arquivo:
        return f"https://drive.google.com/uc?export=download&id={id_arquivo}", f"drive_{id_arquivo}.pdf"
    return url, None


def baixar_pdf(url: str, timeout: int = 30) -> Path | None:
    """Baixa o PDF para data/brutos/. Se a rede falhar (ou o Drive devolver
    uma página em vez do PDF) mas já houver cópia baixada, usa a cópia."""
    DIR_BRUTOS.mkdir(parents=True, exist_ok=True)
    url_real, nome_drive = _url_download(url)
    nome = nome_drive or re.sub(r"[^\w.\-]", "_", Path(unquote(urlparse(url).path)).name) or "lista.pdf"
    if not nome.lower().endswith(".pdf"):
        nome += ".pdf"
    destino = DIR_BRUTOS / nome

    try:
        with requests.get(url_real, timeout=timeout, stream=True, headers={"User-Agent": USER_AGENT}) as resp:
            resp.raise_for_status()
            conteudo = resp.raw.read(TAMANHO_MAX_PDF + 1, decode_content=True)
        if len(conteudo) > TAMANHO_MAX_PDF:
            raise ValueError(f"o arquivo passa de {TAMANHO_MAX_PDF // (1024 * 1024)} MB")
        if not conteudo.startswith(b"%PDF"):
            raise ValueError("a resposta não é um PDF (link restrito, pede login ou confirmação)")
        destino.write_bytes(conteudo)
        log.info("PDF baixado: %s", destino.name)
        return destino
    except (requests.RequestException, ValueError) as e:
        if destino.exists():
            log.warning("Falha ao baixar (%s). Usando cópia local %s.", e, destino.name)
            return destino
        log.error(
            "Falha ao baixar %s: %s. Plano B: baixe o PDF no navegador, salve em "
            "data/brutos/ e rode com --pdf data/brutos/ARQUIVO.pdf", url, e,
        )
        return None


def _abrir_pdf(fonte: str) -> Path | None:
    return baixar_pdf(fonte) if fonte.startswith("http") else Path(fonte)


def _tabelas(pagina) -> list:
    """Tabelas com bordas; se não achar, tenta detectar por alinhamento do texto."""
    return pagina.extract_tables() or pagina.extract_tables(TEXTO_SETTINGS)


def extrair_tabela_pdf(fonte: str) -> list[Lead]:
    """`fonte` pode ser URL (http...) ou caminho de arquivo local."""
    caminho = _abrir_pdf(fonte)
    if not caminho:
        return []

    linhas: list[list[str]] = []
    try:
        with pdfplumber.open(caminho) as pdf:
            for pagina in pdf.pages:
                for tabela in _tabelas(pagina):
                    for linha in tabela:
                        # Remove células vazias (colunas-espaçador comuns em PDF)
                        linhas.append([c for c in linha if c and c.strip()])
        leads = _linhas_para_leads(linhas, "pdf", fonte)
        com_cnpj = sum(1 for l in leads if l.cnpj)
        log.info("PDF %s: %d projetos extraídos (%d com CNPJ).",
                 Path(str(caminho)).name, len(leads), com_cnpj)
        if not leads:
            log.warning(
                "Nenhum projeto reconhecido. Rode com --inspecionar para ver as "
                "linhas e ajuste com --colunas."
            )
        return leads
    except FileNotFoundError:
        log.error("PDF não encontrado: %s", caminho)
    except Exception as e:  # PDF corrompido, protegido etc.
        log.error("Falha ao ler PDF %s: %s", caminho, e)
    return []


def inspecionar_pdf(fonte: str, max_linhas: int = 10) -> None:
    """Mostra as primeiras linhas exatamente como o parser as vê, com índices."""
    caminho = _abrir_pdf(fonte)
    if not caminho:
        return
    with pdfplumber.open(caminho) as pdf:
        print(f"\n=== {Path(str(caminho)).name}: {len(pdf.pages)} páginas ===")
        mostradas = 0
        for n, pagina in enumerate(pdf.pages, 1):
            for tabela in _tabelas(pagina):
                for linha in tabela:
                    cel = [_limpar_celula(c) for c in linha if c and c.strip()]
                    if cel:
                        print(f"p{n} | " + " | ".join(f"[{j}] {c[:35]}" for j, c in enumerate(cel)))
                        mostradas += 1
                        if mostradas >= max_linhas:
                            return
        if not mostradas:
            print("Nenhuma tabela detectada. Texto bruto da 1ª página:\n")
            print((pdf.pages[0].extract_text() or "")[:800])


# ---------------------------------------------------------------
# 3. WEB (Playwright)
# ---------------------------------------------------------------
def _abrir(p, url: str, timeout_ms: int):
    navegador = p.chromium.launch(headless=True)
    pagina = navegador.new_context(user_agent=USER_AGENT).new_page()
    pagina.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:  # páginas com JS: espera assentar, mas não falha se nunca ficar ociosa
        pagina.wait_for_load_state("networkidle", timeout=timeout_ms // 2)
    except PlaywrightTimeout:
        pass
    return navegador, pagina


def _eh_arquivo(href: str) -> bool:
    h = href.lower()
    return ".pdf" in h or "drive.google.com/file/d/" in h


def _filtrar_links(links: list[dict], palavras: tuple[str, ...],
                   excluir: tuple[str, ...]) -> list[dict]:
    """Fica com links de arquivo cujo texto/URL casa com `palavras`, sem
    `excluir`. Ordena pela prioridade da palavra e, no empate, pela ordem na página."""
    candidatos = []
    vistos: set[str] = set()
    for ordem, item in enumerate(links):
        href = item["href"]
        texto = " ".join((item.get("texto") or "").split())
        alvo = f"{href} {texto}".lower()
        if not _eh_arquivo(href) or href in vistos or any(x in alvo for x in excluir):
            continue
        prioridade = next((i for i, k in enumerate(palavras) if k in alvo), None)
        if prioridade is None:
            continue
        vistos.add(href)
        candidatos.append((prioridade, ordem, {"url": href, "texto": texto}))
    return [c for _, _, c in sorted(candidatos, key=lambda x: (x[0], x[1]))]


def coletar_links_pdf(url_pagina: str,
                      palavras_chave: tuple[str, ...] = PALAVRAS_CHAVE_PADRAO,
                      excluir: tuple[str, ...] = EXCLUIR_PADRAO,
                      timeout_ms: int = 25_000) -> list[dict]:
    """Abre a página real e devolve [{url, texto}] dos arquivos de resultado,
    já ordenados por relevância."""
    try:
        with sync_playwright() as p:
            navegador, pagina = _abrir(p, url_pagina, timeout_ms)
            links = pagina.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => ({href: e.href, texto: e.innerText || ''}))",
            )
            navegador.close()
    except PlaywrightTimeout:
        log.error("Timeout ao abrir %s (site lento ou bloqueando).", url_pagina)
        return []
    except Exception as e:  # DNS, bloqueio, Chromium não instalado...
        log.error("Falha ao abrir %s: %s", url_pagina, e)
        return []

    achados = _filtrar_links(links, palavras_chave, excluir)
    log.info("Página %s: %d arquivos candidatos.", url_pagina, len(achados))
    for a in achados:
        log.info("  candidato: %s", a["texto"] or a["url"])
    return achados


def extrair_tabela_html(url_pagina: str, seletor_linhas: str = "table tr",
                        timeout_ms: int = 25_000) -> list[Lead]:
    """Para páginas que publicam a lista direto em tabela HTML."""
    try:
        with sync_playwright() as p:
            navegador, pagina = _abrir(p, url_pagina, timeout_ms)
            linhas = pagina.eval_on_selector_all(
                seletor_linhas,
                "trs => trs.map(tr => Array.from(tr.querySelectorAll('td,th'))"
                ".map(c => c.innerText))",
            )
            navegador.close()
        leads = _linhas_para_leads(linhas, "html", url_pagina)
        log.info("HTML %s: %d projetos extraídos.", url_pagina, len(leads))
        return leads
    except PlaywrightTimeout:
        log.error("Timeout ao ler tabela de %s.", url_pagina)
    except Exception as e:
        log.error("Falha ao ler tabela HTML de %s: %s", url_pagina, e)
    return []


# ---------------------------------------------------------------
# 4. DEMO (só com --demo; dados FICTÍCIOS, nunca usado por padrão)
# ---------------------------------------------------------------
def leads_demo() -> list[Lead]:
    base = [
        ("Sensores IoT para irrigação de pequenas propriedades", "Fulano de Tal Demo",
         "Internet das Coisas (IoT)", "Goiânia"),
        ("Triagem clínica com IA para clínicas populares", "Beltrana Exemplo",
         "Inteligência Artificial e Machine Learning", "Vitória"),
    ]
    return [Lead(t, p, th, m, None, "demo", "dados_ficticios") for t, p, th, m in base]


# ---------------------------------------------------------------
# 5. ORQUESTRADOR (chamado pelo pipeline.py)
# ---------------------------------------------------------------
def coletar_leads(pdf: str | None = None, pagina: str | None = None,
                  palavras_chave: tuple[str, ...] = PALAVRAS_CHAVE_PADRAO,
                  limite_pdfs: int = 1, demo: bool = False) -> list[dict]:
    leads: list[Lead] = []

    if pdf:
        leads += extrair_tabela_pdf(pdf)

    if pagina:
        da_pagina: list[Lead] = []
        for link in coletar_links_pdf(pagina, palavras_chave)[:limite_pdfs]:
            log.info("Lendo: %s", link["texto"] or link["url"])
            da_pagina += extrair_tabela_pdf(link["url"])
        if not da_pagina:  # sem PDF útil: tenta tabela HTML da própria página
            da_pagina = extrair_tabela_html(pagina)
        leads += da_pagina

    if not leads and demo:
        log.warning("Nenhum lead real extraído. Usando dados FICTÍCIOS de demo.")
        leads = leads_demo()

    return [asdict(l) for l in leads]


def adicionar_args_cli(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--pdf", help="URL ou caminho do PDF de aprovados")
    ap.add_argument("--pagina", help="URL da página com o resultado do edital")
    ap.add_argument("--palavras", help="palavras-chave dos links, por prioridade, separadas por vírgula")
    ap.add_argument("--limite", type=int, default=1, help="quantos PDFs da página ler (padrão 1)")
    ap.add_argument("--colunas", help='índices: "titulo,proponente,tematica,municipio" (ex.: "1,2,3,4"; - = ausente)')
    ap.add_argument("--demo", action="store_true", help="dados fictícios se nada for extraído")


def aplicar_args_cli(args) -> tuple[str, ...]:
    if args.colunas:
        definir_colunas(args.colunas)
    if args.palavras:
        return tuple(p.strip().lower() for p in args.palavras.split(",") if p.strip())
    return PALAVRAS_CHAVE_PADRAO


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    ap = argparse.ArgumentParser(description="Etapa 1: extração de leads")
    adicionar_args_cli(ap)
    ap.add_argument("--inspecionar", action="store_true",
                    help="só mostra as linhas do PDF como o parser as vê (para acertar --colunas)")
    args = ap.parse_args()
    palavras = aplicar_args_cli(args)

    if args.inspecionar:
        fontes = [args.pdf] if args.pdf else [
            l["url"] for l in coletar_links_pdf(args.pagina, palavras)[: args.limite]
        ] if args.pagina else []
        for f in fontes:
            inspecionar_pdf(f)
    else:
        resultado = coletar_leads(pdf=args.pdf, pagina=args.pagina, palavras_chave=palavras,
                                  limite_pdfs=args.limite, demo=args.demo)
        print(json.dumps(resultado[:3], ensure_ascii=False, indent=2))
        print(f"\nTotal de leads: {len(resultado)}")