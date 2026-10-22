"""
COLETA DE CONTATOS (telefone e e-mail) COM REGRA DE CONFIANÇA
=====================================================================
REGRA PRINCIPAL: um telefone/e-mail só vai para os campos principais
(`telefone_confirmado`, `email_confirmado`) se estiver COMPLETO e for
CLARAMENTE da pessoa, da startup ou do laboratório dela. Todo o resto fica
registrado em `contatos`, com a classe e o MOTIVO escritos:

  confirmado  -> completo e com prova de titularidade
  duvidoso    -> completo, mas ninguém prova que é da pessoa/startup
                 (ex.: só consta no cadastro da Receita, que costuma trazer o
                 contador). Aparece no painel como "não confirmado".
  descartado  -> mascarado/incompleto, ou de contabilidade, coworking,
                 recepção/central de atendimento

Fontes, da mais forte para a mais fraca:
  1. site próprio da startup (descoberto por busca + conferido no conteúdo);
  2. e-mail com o nome do titular ou no domínio do site próprio;
  3. cadastro da Receita (BrasilAPI) e sites agregadores (cnpj.biz, Casa dos
     Dados...). Os agregadores só espelham a Receita, então NÃO contam como
     confirmação independente. Dígitos escondidos por "cadastre-se" ficam como
     "mascarado": não há tentativa de burlar o acesso.
=====================================================================
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from classificacao.guardrails import _norm
from enriquecimento import web
from enriquecimento.cnpj_resolver import tokens

log = logging.getLogger("contatos")

CONFIRMADO, DUVIDOSO, DESCARTADO = "confirmado", "duvidoso", "descartado"

DDDS = {11, 12, 13, 14, 15, 16, 17, 18, 19, 21, 22, 24, 27, 28, 31, 32, 33, 34, 35, 37, 38,
        41, 42, 43, 44, 45, 46, 47, 48, 49, 51, 53, 54, 55, 61, 62, 63, 64, 65, 66, 67, 68, 69,
        71, 73, 74, 75, 77, 79, 81, 82, 83, 84, 85, 86, 87, 88, 89, 91, 92, 93, 94, 95, 96, 97, 98, 99}

TERMOS_CONTABILIDADE = ("contabil", "contador", "contabilista", "assessoria", "escritorio contabil",
                        "bpo", "abertura de empresa", "fiscal")
TERMOS_COWORKING = ("coworking", "cowork", "escritorio virtual", "endereco fiscal",
                    "espaco compartilhado", "sala compartilhada", "hub de")
TERMOS_RECEPCAO = ("recepcao", "portaria", "secretaria", "telefonista", "central de atendimento",
                   "sac", "ramal", "atendimento ao cliente", "ouvidoria")
EMAIL_GENERICO_RECEPCAO = ("recepcao", "portaria", "secretaria", "sac", "ouvidoria", "atendimento")
DOMINIOS_LIVRES = ("gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "yahoo.com.br",
                   "uol.com.br", "bol.com.br", "icloud.com", "live.com", "msn.com", "proton.me")
RE_INSTITUCIONAL = re.compile(r"\.edu\.br$|\.gov\.br$|(^|\.)(unb|usp|unicamp|unesp|ifb|ufg|ufmg|ufrj|ufsc|ufpe|ufba|ufrgs|ufc|ufpr)\.br$")

RE_EMAIL = re.compile(r"[A-Za-z0-9._%+\-*]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
RE_TEL = re.compile(r"(?:\+?55\s*)?\(?\d{2}\)?[\s.\-]*(?:9[\s.\-]?)?[\d*xX•●#]{4}[\s.\-]?[\d*xX•●#]{4}")
RE_MASCARA = re.compile(r"[*xX•●#]{2,}|…|\.{3}")


# ---------------------------------------------------------------
# Normalização de telefone
# ---------------------------------------------------------------
def analisar_telefone(texto: str) -> dict:
    """{'estado': 'completo'|'mascarado'|'incompleto'|'central', 'normalizado', 'formatado'}"""
    bruto = (texto or "").strip()
    if RE_MASCARA.search(bruto):
        return {"estado": "mascarado", "normalizado": None, "formatado": bruto}
    d = re.sub(r"\D", "", bruto)
    if d.startswith("0800") or (d.startswith(("4003", "4004", "3003")) and len(d) == 8):
        return {"estado": "central", "normalizado": None, "formatado": bruto}
    if d.startswith("55") and len(d) in (12, 13):
        d = d[2:]
    if len(d) not in (10, 11) or int(d[:2]) not in DDDS:
        return {"estado": "incompleto", "normalizado": None, "formatado": bruto}
    if len(d) == 11 and d[2] != "9":
        return {"estado": "incompleto", "normalizado": None, "formatado": bruto}
    if len(d) == 10 and d[2] not in "2345":
        return {"estado": "incompleto", "normalizado": None, "formatado": bruto}
    fmt = f"({d[:2]}) {d[2:7]}-{d[7:]}" if len(d) == 11 else f"({d[:2]}) {d[2:6]}-{d[6:]}"
    return {"estado": "completo", "normalizado": d, "formatado": fmt}


# ---------------------------------------------------------------
# Extração de uma página
# ---------------------------------------------------------------
def extrair_contatos_html(html: str, janela: int = 90) -> list[dict]:
    """[{'tipo','valor','contexto'}] a partir de links tel:/mailto: e do texto."""
    soup = BeautifulSoup(html or "", "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    achados: list[dict] = []

    for a in soup.select("a[href^='tel:'], a[href^='mailto:']"):
        href = a.get("href", "")
        tipo = "telefone" if href.startswith("tel:") else "email"
        valor = href.split(":", 1)[1].split("?")[0].strip()
        ctx = (a.parent.get_text(" ", strip=True) if a.parent else a.get_text(" ", strip=True))[:2 * janela]
        achados.append({"tipo": tipo, "valor": valor, "contexto": ctx})

    texto = soup.get_text(" ", strip=True)
    for m in RE_EMAIL.finditer(texto):
        achados.append({"tipo": "email", "valor": m.group(), "contexto": texto[max(0, m.start() - janela):m.end() + janela]})
    for m in RE_TEL.finditer(texto):
        achados.append({"tipo": "telefone", "valor": m.group().strip(),
                        "contexto": texto[max(0, m.start() - janela):m.end() + janela]})
    return achados


# ---------------------------------------------------------------
# Classificação (a regra do usuário)
# ---------------------------------------------------------------
def _tem(termos, texto_norm: str) -> str | None:
    for t in termos:
        # termos curtos ("sac", "bpo") só valem como palavra inteira
        padrao = r"\b" + re.escape(t) + (r"\b" if len(t) <= 4 else "")
        if re.search(padrao, texto_norm):
            return t
    return None


def classificar(tipo: str, valor: str, fontes: list[str], contexto: str, lead: dict) -> dict:
    """Devolve {'classe', 'motivo', 'valor_exibicao'}."""
    ctx = _norm(contexto)
    endereco = _norm(" ".join(str(lead.get(c) or "") for c in ("logradouro_cnpj", "complemento_cnpj")))
    site = web.dominio(lead.get("site_proprio") or "")

    # --- 1. completo?
    if tipo == "telefone":
        a = analisar_telefone(valor)
        if a["estado"] == "mascarado":
            return {"classe": DESCARTADO, "motivo": "número com dígitos escondidos (mascarado)", "valor_exibicao": a["formatado"]}
        if a["estado"] == "incompleto":
            return {"classe": DESCARTADO, "motivo": "número incompleto ou inválido", "valor_exibicao": a["formatado"]}
        if a["estado"] == "central":
            return {"classe": DESCARTADO, "motivo": "central de atendimento (0800/4003)", "valor_exibicao": a["formatado"]}
        exibicao = a["formatado"]
        dominio_email = ""
        local = ""
    else:
        v = valor.strip().lower()
        if RE_MASCARA.search(v):
            return {"classe": DESCARTADO, "motivo": "e-mail com caracteres escondidos (mascarado)", "valor_exibicao": v}
        if "@" not in v:
            return {"classe": DESCARTADO, "motivo": "e-mail inválido", "valor_exibicao": v}
        local, dominio_email = v.split("@", 1)
        exibicao = v

    # --- 2. contabilidade / coworking / recepção
    base_ctx = ctx + " " + _norm(dominio_email + " " + local)
    if _tem(TERMOS_CONTABILIDADE, base_ctx):
        return {"classe": DESCARTADO, "motivo": "pertence a contabilidade/assessoria", "valor_exibicao": "—"}
    if _tem(TERMOS_COWORKING, base_ctx) or (tipo == "telefone" and _tem(TERMOS_COWORKING, endereco)):
        return {"classe": DESCARTADO, "motivo": "pertence a coworking/endereço compartilhado", "valor_exibicao": "—"}
    if _tem(TERMOS_RECEPCAO, ctx) or (tipo == "email" and any(local.startswith(g) for g in EMAIL_GENERICO_RECEPCAO)):
        return {"classe": DESCARTADO, "motivo": "recepção/central de atendimento", "valor_exibicao": "—"}

    # --- 3. prova de titularidade
    if "site_proprio" in fontes:
        return {"classe": CONFIRMADO, "motivo": "consta no site próprio da startup", "valor_exibicao": exibicao}
    if tipo == "email":
        toks = tokens(lead.get("proponente", ""))
        nomes_local = _norm(local.replace(".", " ").replace("_", " ").replace("-", " "))
        if site and dominio_email == site:
            return {"classe": CONFIRMADO, "motivo": "e-mail no domínio do site próprio", "valor_exibicao": exibicao}
        if toks and sum(t in nomes_local for t in toks) >= 2:
            if RE_INSTITUCIONAL.search(dominio_email):
                return {"classe": CONFIRMADO, "motivo": "e-mail institucional com o nome do titular (laboratório/universidade)", "valor_exibicao": exibicao}
            return {"classe": CONFIRMADO, "motivo": "e-mail com o nome completo do titular", "valor_exibicao": exibicao}
        if dominio_email in DOMINIOS_LIVRES:
            return {"classe": DUVIDOSO, "motivo": "e-mail pessoal sem relação clara com o titular", "valor_exibicao": exibicao}
        if RE_INSTITUCIONAL.search(dominio_email):
            return {"classe": DUVIDOSO, "motivo": "e-mail institucional: confirmar se é do laboratório do proponente", "valor_exibicao": exibicao}
    return {"classe": DUVIDOSO,
            "motivo": "só consta no cadastro da Receita/agregadores; titularidade não confirmada",
            "valor_exibicao": exibicao}


# ---------------------------------------------------------------
# Coleta
# ---------------------------------------------------------------
def _contatos_da_receita(lead: dict) -> list[dict]:
    out = []
    for campo in ("ddd_telefone_1", "ddd_telefone_2"):
        v = lead.get(campo)
        if v:
            out.append({"tipo": "telefone", "valor": str(v), "contexto": "", "fonte": "receita"})
    if lead.get("email_receita"):
        out.append({"tipo": "email", "valor": lead["email_receita"], "contexto": "", "fonte": "receita"})
    return out


def descobrir_site_proprio(lead: dict, buscar=web.buscar_web, baixar=web.baixar) -> str | None:
    """Acha o site da startup e confere se o conteúdo cita o nome/projeto."""
    if lead.get("site") or lead.get("site_proprio"):
        return lead.get("site") or lead.get("site_proprio")
    nomes = [lead.get("nome_fantasia"), lead.get("razao_social"), lead.get("titulo_projeto")]
    nomes = [n for n in nomes if n and not re.fullmatch(r"\d{8}\s.*", n)]  # MEI: "12345678 NOME" não ajuda
    if not nomes:
        return None
    distintivos = {t for n in nomes for t in tokens(n) if len(t) >= 5}
    for r in buscar(f'"{nomes[0]}" {lead.get("municipio", "")} site oficial', max_resultados=6):
        href = r.get("href", "")
        dom = web.dominio(href)
        if not href or any(dom == d or dom.endswith("." + d) for d in web.DOMINIOS_IGNORADOS):
            continue
        html, _ = baixar(href)
        if html and distintivos & set(_norm(BeautifulSoup(html, "html.parser").get_text(" ")).split()):
            return href
    return None


def coletar_contatos(lead: dict, usar_web: bool = True, buscar=web.buscar_web,
                     baixar=web.baixar) -> dict:
    """Devolve os campos de contato a acrescentar ao lead."""
    bruto: list[dict] = _contatos_da_receita(lead)
    site = None

    if usar_web:
        site = descobrir_site_proprio(lead, buscar, baixar)
        if site:
            lead = {**lead, "site_proprio": site}
            base = site.rstrip("/")
            for url in (site, base + "/contato", base + "/contact", base + "/fale-conosco"):
                html, _ = baixar(url)
                for c in extrair_contatos_html(html or ""):
                    bruto.append({**c, "fonte": "site_proprio"})
        if lead.get("cnpj"):  # agregadores: espelham a Receita (fonte "agregador")
            c = lead["cnpj"]
            c_fmt = f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}" if len(c) == 14 else c
            for r in buscar(f'"{c_fmt}" telefone e-mail', max_resultados=5):
                dom = web.dominio(r.get("href", ""))
                if dom in ("cnpj.biz", "casadosdados.com.br"):
                    html, _ = baixar(r["href"])
                    for c in extrair_contatos_html(html or ""):
                        bruto.append({**c, "fonte": f"agregador:{dom}"})

    # agrupa por valor
    grupos: dict[tuple, dict] = {}
    for c in bruto:
        a = analisar_telefone(c["valor"]) if c["tipo"] == "telefone" else None
        chave = (c["tipo"], (a["normalizado"] if a and a["normalizado"] else c["valor"].strip().lower()))
        g = grupos.setdefault(chave, {"tipo": c["tipo"], "valor": c["valor"], "fontes": [], "contexto": ""})
        if c["fonte"] not in g["fontes"]:
            g["fontes"].append(c["fonte"])
        g["contexto"] += " " + c.get("contexto", "")

    contatos = []
    for g in grupos.values():
        r = classificar(g["tipo"], g["valor"], g["fontes"], g["contexto"], lead)
        contatos.append({"tipo": g["tipo"], "valor": r["valor_exibicao"], "classe": r["classe"],
                         "motivo": r["motivo"], "fontes": g["fontes"]})

    ordem = {CONFIRMADO: 0, DUVIDOSO: 1, DESCARTADO: 2}
    contatos.sort(key=lambda c: ordem[c["classe"]])
    tel = next((c["valor"] for c in contatos if c["tipo"] == "telefone" and c["classe"] == CONFIRMADO), None)
    mail = next((c["valor"] for c in contatos if c["tipo"] == "email" and c["classe"] == CONFIRMADO), None)
    if tel or mail:
        status = "confirmado"
    elif any(c["classe"] == DUVIDOSO for c in contatos):
        status = "so_nao_confirmado"
    elif contatos:
        status = "so_descartado"
    else:
        status = "nenhum"
    return {"site_proprio": site, "telefone_confirmado": tel, "email_confirmado": mail,
            "contatos": contatos, "contato_status": status}


def coletar_contatos_leads(leads: list[dict], usar_web: bool = True, max_leads: int | None = None) -> list[dict]:
    saida, feitos = [], 0
    for lead in leads:
        lead = dict(lead)
        if not lead.get("cnpj"):
            lead.update({"contatos": [], "contato_status": "sem_cnpj",
                         "telefone_confirmado": None, "email_confirmado": None})
        elif max_leads is not None and feitos >= max_leads:
            lead.update({"contatos": [], "contato_status": "nao_processado",
                         "telefone_confirmado": None, "email_confirmado": None})
        else:
            lead.update(coletar_contatos(lead, usar_web=usar_web))
            feitos += 1
        saida.append(lead)
    return saida
