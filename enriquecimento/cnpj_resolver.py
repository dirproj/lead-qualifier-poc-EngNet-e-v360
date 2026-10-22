"""
RESOLVEDOR AUTOMÁTICO DE CNPJ
=====================================================================
As listas de aprovados não trazem CNPJ e a BrasilAPI só consulta POR CNPJ.
Este módulo descobre o CNPJ da startup (ou do MEI do fundador) assim:

  1. CANDIDATOS, em etapas (para na primeira que dá resultado confiável):
       a) Casa dos Dados: NOME DO PROPONENTE (como sócio e como razão social/MEI)
       b) Casa dos Dados: TÍTULO DO PROJETO (nome fantasia/razão social)
       c) página de busca do cnpj.biz (se o robots.txt deixar)
       d) busca web gratuita por nome (inclusive restrita a cnpj.biz/Casa dos Dados)
     Nada é digitado à mão: o data/cnpjs_manuais.csv é só um atalho opcional.
  2. VERIFICAÇÃO (oficial): cada candidato é validado (dígito verificador) e
     consultado na BrasilAPI. Só vale se os dados da Receita baterem com o lead:
       - MEI: a razão social contém o nome completo do titular;
       - empresa (Ltda etc.): o proponente consta no quadro societário (QSA);
       - situação cadastral ATIVA; município igual ao do projeto (reforço).
  3. SAÍDA com confiança explícita:
       alta   -> usado automaticamente
       media  -> usado, mas o painel mostra "provável, confirmar"
       baixa/ambíguo/não achou -> NÃO é usado; candidatos ficam listados

Os sites agregadores só sugerem candidatos. Nenhum dado deles é confiado sem
passar pela BrasilAPI. Homônimos (mesmo nome, outra pessoa) são pegos pelo
município e pelo QSA; se dois candidatos empatam, o status é "ambiguo".
=====================================================================
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from urllib.parse import quote

from classificacao.guardrails import _norm
from enriquecimento import casadosdados, web
from enriquecimento.brasilapi import consultar_brasilapi
from extracao.validacao import cnpj_valido, limpar_cnpj

log = logging.getLogger("cnpj_resolver")

RAIZ = Path(__file__).resolve().parent.parent
CACHE = RAIZ / "data" / "cache_cnpj.json"

RE_FMT = re.compile(r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b")
RE_RAW = re.compile(r"(?<!\d)\d{14}(?!\d)")
STOP = {"de", "da", "do", "das", "dos", "e", "du"}

# status possíveis em lead["cnpj_status"]
INFORMADO, ENCONTRADO, PROVAVEL, AMBIGUO, NAO_ENCONTRADO = (
    "informado", "encontrado", "provavel", "ambiguo", "nao_encontrado")


def tokens(nome: str) -> list[str]:
    return [t for t in _norm(nome).split() if len(t) > 1 and t not in STOP]


def candidatos_do_texto(texto: str) -> list[str]:
    achados = []
    for m in RE_FMT.findall(texto or "") + RE_RAW.findall(texto or ""):
        c = limpar_cnpj(m)
        if cnpj_valido(c) and c not in achados:
            achados.append(c)
    return achados


def consultas_busca(lead: dict) -> list[str]:
    prop, mun = lead.get("proponente", ""), lead.get("municipio", "")
    titulo = lead.get("titulo_projeto", "")
    q = []
    if prop:
        q += [f'site:cnpj.biz "{prop}"',
              f'site:casadosdados.com.br "{prop}"',
              f'"{prop}" CNPJ {mun}'.strip()]
    if titulo:
        q.append(f'"{titulo}" CNPJ {mun}'.strip())
    return q


def termos_do_titulo(titulo: str) -> list[str]:
    """Nomes que a startup pode ter usado: o trecho antes de ':' ou ' - ' e o título curto."""
    t = (titulo or "").strip()
    out = []
    for sep in (":", " - ", " – ", " | "):
        if sep in t:
            out.append(t.split(sep)[0].strip())
    if 0 < len(t.split()) <= 4:
        out.append(t)
    return [x for x in dict.fromkeys(out) if len(x) >= 4]


def estagios(lead: dict, max_consultas: int, buscar, baixar, cdd):
    """Gera (nome_da_etapa, função_sem_argumentos -> {cnpj: [fontes]}), na ordem de tentativa."""
    prop, mun, titulo = lead.get("proponente", ""), lead.get("municipio", ""), lead.get("titulo_projeto", "")

    def de_lista(itens, fonte):
        return {i["cnpj"]: [fonte] for i in itens}

    if prop:
        yield "casadosdados: nome do proponente (sócio)", lambda: de_lista(
            cdd(prop, socio=True, razao=True, fantasia=False, tipo="exata"), "casadosdados:sócio")
        yield "casadosdados: nome do proponente (MEI)", lambda: de_lista(
            cdd(prop, socio=False, razao=True, fantasia=False, tipo="radical"), "casadosdados:razão")
    for termo in termos_do_titulo(titulo):
        yield f"casadosdados: título do projeto ({termo[:30]})", lambda t=termo: de_lista(
            cdd(t, socio=False, razao=True, fantasia=True, tipo="radical"), "casadosdados:título")

    def cnpj_biz():
        html, motivo = baixar(f"https://cnpj.biz/procura/{quote(prop)}")
        if not html:
            log.info("cnpj.biz/procura indisponível (%s)", motivo)
        return {c: ["cnpj.biz/procura"] for c in candidatos_do_texto(html or "")}

    if prop:
        yield "cnpj.biz (busca por nome)", cnpj_biz

    def web_busca():
        achados: dict[str, list[str]] = {}
        for consulta in consultas_busca(lead)[:max_consultas]:
            for r in buscar(consulta):
                blob = " ".join((r.get("href", ""), r.get("title", ""), r.get("body", "")))
                for c in candidatos_do_texto(blob):
                    achados.setdefault(c, []).append(web.dominio(r.get("href", "")) or "busca")
        return achados

    yield "busca web", web_busca


def verificar(cnpj: str, lead: dict, consultar=consultar_brasilapi) -> dict | None:
    """Confere um candidato na BrasilAPI. Devolve {'cnpj','pontos','confianca','evidencias'}."""
    d = consultar(cnpj)
    if not d:
        return None
    toks = tokens(lead.get("proponente", ""))
    razao = set(tokens(d.get("razao_social") or ""))
    fantasia = set(tokens(d.get("nome_fantasia") or ""))
    socios = [set(tokens(s.get("nome_socio") or "")) for s in (d.get("qsa") or [])]

    evid, pontos = [], 0
    nome_na_razao = bool(toks) and len(toks) >= 2 and all(t in razao for t in toks)
    nome_no_qsa = bool(toks) and len(toks) >= 2 and any(all(t in s for t in toks) for s in socios)
    if nome_na_razao:
        pontos += 3
        evid.append("nome completo do proponente na razão social (padrão MEI)")
    elif nome_no_qsa:
        pontos += 3
        evid.append("proponente consta no quadro societário")
    elif toks and len(toks) >= 2 and (sum(t in razao for t in toks) >= len(toks) - 1
                                      or any(sum(t in s for t in toks) >= len(toks) - 1 for s in socios)):
        pontos += 1
        evid.append("nome do proponente bate parcialmente")

    titulo_toks = {t for t in tokens(lead.get("titulo_projeto", "")) if len(t) >= 5}
    if titulo_toks & (razao | fantasia):
        pontos += 1
        evid.append("nome da empresa lembra o título do projeto")

    mun_lead, mun_cnpj = _norm(lead.get("municipio", "")), _norm(d.get("municipio") or "")
    mesmo_mun = bool(mun_lead) and mun_lead == mun_cnpj
    if mesmo_mun:
        pontos += 1
        evid.append(f"mesmo município ({d.get('municipio')})")

    ativa = "ativa" in _norm(d.get("descricao_situacao_cadastral") or "")
    if ativa:
        evid.append("situação cadastral ativa")
    else:
        pontos -= 3
        evid.append(f"situação cadastral não ativa ({d.get('descricao_situacao_cadastral')})")

    forte = nome_na_razao or nome_no_qsa
    if forte and ativa and (mesmo_mun or nome_no_qsa):
        conf = "alta"
    elif forte and ativa:
        conf = "media"
    else:
        conf = "baixa"
    return {"cnpj": cnpj, "pontos": pontos, "confianca": conf, "evidencias": evid,
            "razao_social": d.get("razao_social")}


def _carregar_cache() -> dict:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _salvar_cache(c: dict) -> None:
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def resolver_cnpj_lead(lead: dict, max_consultas: int = 3, max_verificar: int = 8,
                       buscar=web.buscar_web, baixar=web.baixar,
                       consultar=consultar_brasilapi, cdd=casadosdados.pesquisar) -> dict:
    """Devolve só os campos a acrescentar ao lead."""
    if lead.get("cnpj") and cnpj_valido(lead["cnpj"]):
        return {"cnpj": limpar_cnpj(lead["cnpj"]), "cnpj_status": INFORMADO, "cnpj_confianca": "alta",
                "cnpj_origem": "lista oficial/manual", "cnpj_evidencias": []}

    fontes: dict[str, list[str]] = {}
    verificados: dict[str, dict] = {}
    etapas_usadas: list[str] = []

    def melhor_unico():
        altas = [v for v in verificados.values() if v["confianca"] == "alta"]
        altas.sort(key=lambda v: -v["pontos"])
        if altas and (len(altas) == 1 or altas[0]["pontos"] > altas[1]["pontos"]):
            return altas[0]
        return None

    for nome_etapa, coletar in estagios(lead, max_consultas, buscar, baixar, cdd):
        try:
            novos = coletar()
        except Exception as e:  # noqa: BLE001 - uma fonte quebrada não derruba as outras
            log.warning("Etapa '%s' falhou: %s", nome_etapa, e)
            continue
        etapas_usadas.append(f"{nome_etapa}: {len(novos)} candidato(s)")
        for c, fs in novos.items():
            fontes.setdefault(c, [])
            fontes[c] += [f for f in fs if f not in fontes[c]]
        for c in list(fontes):
            if c not in verificados and len(verificados) < max_verificar:
                v = verificar(c, lead, consultar)
                if v:
                    verificados[c] = v
        if melhor_unico():
            break  # achou com alta confiança: não precisa gastar as etapas seguintes

    for c, v in verificados.items():
        v["fontes"] = fontes.get(c, [])
    ordenados = sorted(verificados.values(), key=lambda v: -v["pontos"])
    listados = [{"cnpj": v["cnpj"], "razao_social": v["razao_social"], "confianca": v["confianca"],
                 "fontes": v["fontes"]} for v in ordenados]
    base = {"cnpj_etapas": etapas_usadas, "cnpj_candidatos": listados}

    boas = [v for v in ordenados if v["confianca"] in ("alta", "media")]
    if not boas:
        return {**base, "cnpj": None, "cnpj_status": NAO_ENCONTRADO, "cnpj_confianca": None,
                "cnpj_origem": None, "cnpj_evidencias": []}
    topo = boas[0]
    empatados = [v for v in boas if v["pontos"] == topo["pontos"] and v["cnpj"] != topo["cnpj"]]
    if empatados:
        return {**base, "cnpj": None, "cnpj_status": AMBIGUO, "cnpj_confianca": None, "cnpj_origem": None,
                "cnpj_evidencias": ["mais de um CNPJ com a mesma pontuação: confirmar à mão"]}
    return {**base, "cnpj": topo["cnpj"],
            "cnpj_status": ENCONTRADO if topo["confianca"] == "alta" else PROVAVEL,
            "cnpj_confianca": topo["confianca"],
            "cnpj_origem": "Receita (BrasilAPI) confirmou candidato de: " + ", ".join(topo["fontes"]),
            "cnpj_evidencias": topo["evidencias"]}


def resolver_cnpjs(leads: list[dict], max_busca: int | None = None, **kw) -> list[dict]:
    """Roda o resolvedor nos leads sem CNPJ (com cache em data/cache_cnpj.json)."""
    cache = _carregar_cache()
    saida, buscas = [], 0
    for lead in leads:
        lead = dict(lead)
        chave = f"{lead.get('proponente', '')}|{lead.get('titulo_projeto', '')}"
        if lead.get("cnpj") and cnpj_valido(lead["cnpj"]):
            lead.update(resolver_cnpj_lead(lead, **kw))
        elif chave in cache and cache[chave].get("cnpj_status") in (ENCONTRADO, PROVAVEL):
            lead.update(cache[chave])
        elif max_busca is not None and buscas >= max_busca:
            lead.update({"cnpj_status": NAO_ENCONTRADO, "cnpj_evidencias": ["busca não executada (--max-busca)"]})
        else:
            res = resolver_cnpj_lead(lead, **kw)
            buscas += 1
            lead.update(res)
            if res["cnpj_status"] in (ENCONTRADO, PROVAVEL):
                cache[chave] = res
                _salvar_cache(cache)
        saida.append(lead)
    cont: dict[str, int] = {}
    for l in saida:
        cont[l.get("cnpj_status", "?")] = cont.get(l.get("cnpj_status", "?"), 0) + 1
    log.info("Resolução de CNPJ: %s", cont)
    if casadosdados.ULTIMO_ERRO and not any(l.get("cnpj_status") in (ENCONTRADO, PROVAVEL) for l in saida):
        log.warning("Casa dos Dados não colaborou (%s). Defina CASADOSDADOS_API_KEY no .env, se tiver, "
                    "ou confira a conexão; as etapas seguintes (cnpj.biz e busca web) foram usadas.",
                    casadosdados.ULTIMO_ERRO)
    return saida
