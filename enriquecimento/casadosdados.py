"""
BUSCA NO CASA DOS DADOS (por nome do proponente e por título do projeto)
=====================================================================
Usa a pesquisa do Casa dos Dados, que aceita busca textual em três campos:
razão social, nome fantasia e NOME DO SÓCIO. Dois endpoints:

  - público (sem chave, padrão):  POST /v5/public/cnpj/pesquisa
    devolve poucos campos (CNPJ, razão social, nome fantasia, situação) e
    cerca de 20 empresas por consulta. Basta para descobrir o CNPJ.
  - oficial (opcional, com chave): POST /v5/cnpj/pesquisa + header api-key.
    Se existir CASADOSDADOS_API_KEY no .env, ele é usado.

IMPORTANTE (honestidade): os nomes dos campos abaixo seguem a documentação
oficial do Casa dos Dados (docs.casadosdados.com.br) e relatos públicos, mas
este código foi escrito SEM acesso ao site para teste. Por isso:
  - a resposta é lida de forma tolerante (qualquer objeto com 'cnpj' vale);
  - o motivo de qualquer falha (401/403/429/400/rede) vai para o log;
  - TODO candidato é conferido na Receita (BrasilAPI) antes de ser usado.
Termos de uso: o endpoint público é o mesmo que o site usa. Use com moderação
(há pausa entre consultas e cache em disco) e confira os termos do serviço.
=====================================================================
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import unicodedata
from pathlib import Path

import requests

from extracao.validacao import cnpj_valido, limpar_cnpj

log = logging.getLogger("casadosdados")

RAIZ = Path(__file__).resolve().parent.parent
DIR_CACHE = RAIZ / "data" / "cache_web"

URL_PUBLICA = "https://api.casadosdados.com.br/v5/public/cnpj/pesquisa"
URL_OFICIAL = "https://api.casadosdados.com.br/v5/cnpj/pesquisa"
PAUSA = 3.0

_ultimo = 0.0
ULTIMO_ERRO: str | None = None   # lido pelo pipeline para explicar por que a fonte falhou


def _sem_acento_maiusculo(txt: str) -> str:
    return unicodedata.normalize("NFKD", txt or "").encode("ascii", "ignore").decode().upper().strip()


def montar_payload(termo: str, socio: bool, razao: bool, fantasia: bool,
                   tipo: str = "exata", uf: str | None = None, municipio: str | None = None) -> dict:
    p = {
        "busca_textual": [{
            "texto": [termo],
            "tipo_busca": tipo,
            "razao_social": razao,
            "nome_fantasia": fantasia,
            "nome_socio": socio,
        }],
        "situacao_cadastral": ["ATIVA"],
        "limite": 20,
        "pagina": 1,
    }
    if uf:
        p["uf"] = [uf.upper()]
    if municipio:
        p["municipio"] = [_sem_acento_maiusculo(municipio)]
    return p


def extrair_empresas(obj) -> list[dict]:
    """Varre qualquer JSON e junta todo objeto que tenha um 'cnpj' válido."""
    achados: dict[str, dict] = {}

    def varrer(o):
        if isinstance(o, dict):
            c = limpar_cnpj(str(o.get("cnpj", ""))) if "cnpj" in o else ""
            if c and cnpj_valido(c) and c not in achados:
                achados[c] = {"cnpj": c, "razao_social": o.get("razao_social"),
                              "nome_fantasia": o.get("nome_fantasia"), "fonte": "casadosdados"}
            for v in o.values():
                varrer(v)
        elif isinstance(o, list):
            for v in o:
                varrer(v)

    varrer(obj)
    return list(achados.values())


def _cache_path(chave: str) -> Path:
    return DIR_CACHE / ("cdd_" + hashlib.sha1(chave.encode("utf-8")).hexdigest() + ".json")


def pesquisar(termo: str, socio: bool = False, razao: bool = True, fantasia: bool = False,
              tipo: str = "exata", uf: str | None = None, municipio: str | None = None,
              post=requests.post) -> list[dict]:
    """Devolve [{'cnpj','razao_social','nome_fantasia','fonte'}]. Nunca levanta exceção."""
    global _ultimo, ULTIMO_ERRO
    termo = (termo or "").strip()
    if len(termo) < 4:
        return []
    chave_api = os.getenv("CASADOSDADOS_API_KEY")
    url = URL_OFICIAL if chave_api else URL_PUBLICA
    headers = {"Content-Type": "application/json", "User-Agent": "LeadQualifierPoC/1.0 (projeto academico)"}
    if chave_api:
        headers["api-key"] = chave_api

    tipos = [tipo] + (["exata"] if tipo != "exata" else [])   # se "radical" der 400, tenta "exata"
    for t in tipos:
        payload = montar_payload(termo, socio, razao, fantasia, t, uf, municipio)
        chave = url + json.dumps(payload, sort_keys=True)
        cache = _cache_path(chave)
        if cache.exists():
            try:
                return extrair_empresas(json.loads(cache.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                pass

        espera = _ultimo + PAUSA - time.monotonic()
        if espera > 0:
            time.sleep(espera)
        _ultimo = time.monotonic()
        try:
            r = post(url, json=payload, headers=headers, timeout=20)
        except requests.RequestException as e:
            ULTIMO_ERRO = f"rede: {type(e).__name__}"
            log.warning("Casa dos Dados indisponível (%s)", ULTIMO_ERRO)
            return []

        if r.status_code == 200:
            try:
                dados = r.json()
            except ValueError:
                ULTIMO_ERRO = "resposta não é JSON"
                log.warning("Casa dos Dados: %s", ULTIMO_ERRO)
                return []
            try:
                DIR_CACHE.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
            ULTIMO_ERRO = None
            return extrair_empresas(dados)

        corpo = (getattr(r, "text", "") or "")[:200].replace("\n", " ")
        ULTIMO_ERRO = f"HTTP {r.status_code}: {corpo}"
        if r.status_code == 400 and t != "exata":
            log.info("Casa dos Dados recusou tipo_busca=%s; tentando 'exata'. %s", t, corpo)
            continue
        if r.status_code in (401, 403):
            log.warning("Casa dos Dados negou acesso (%s). O endpoint público pode exigir chave: "
                        "defina CASADOSDADOS_API_KEY ou use a busca web como alternativa.", ULTIMO_ERRO)
        else:
            log.warning("Casa dos Dados respondeu %s", ULTIMO_ERRO)
        return []
    return []
