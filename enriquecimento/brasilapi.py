"""
CONSULTA DE CNPJ (camada de dados oficiais abertos)
=====================================================================
Dado um CNPJ, devolve o cadastro da Receita Federal já normalizado:
razão social, CNAE, porte, situação, sócios (QSA), telefones e e-mail
cadastrados. Tenta duas fontes gratuitas, nesta ordem:
    1) minhareceita.org   (espelho open-source da base aberta da Receita)
    2) BrasilAPI          (reserva)

Os sites cnpj.biz e Casa dos Dados mostram ESTES MESMOS dados da Receita,
muitas vezes com telefone/e-mail parcialmente ocultos. Por isso o contato
vem daqui (completo e sem raspar ninguém); os sites só são usados para
DESCOBRIR o CNPJ (veja descoberta.py).
=====================================================================
"""

from __future__ import annotations

import logging
import time

import requests

from extracao.validacao import limpar_cnpj

log = logging.getLogger("enriquecimento")

FONTES_CNPJ = (
    ("minhareceita", "https://minhareceita.org/{}"),
    ("brasilapi", "https://brasilapi.com.br/api/cnpj/v1/{}"),
)
USER_AGENT = "lead-qualifier-poc/1.0 (hackathon; consulta de CNPJ publico)"


def _get_json(url: str, tentativas: int = 3, timeout: int = 15) -> tuple[dict | None, str]:
    """Devolve (json, status) com status em 'ok', 'nao_encontrado' ou 'erro'."""
    for i in range(tentativas):
        try:
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
            if resp.status_code == 200:
                return resp.json(), "ok"
            if resp.status_code in (404, 400):
                return None, "nao_encontrado"
            if resp.status_code == 429:  # limite de requisições: espera e tenta de novo
                time.sleep(2 * (i + 1))
                continue
            log.warning("HTTP %s em %s", resp.status_code, url)
        except (requests.RequestException, ValueError) as e:  # rede, timeout, JSON inválido
            log.warning("Erro (%s) em %s. Tentativa %d/%d.", e, url, i + 1, tentativas)
            time.sleep(1)
    return None, "erro"


def _primeiro(d: dict, *chaves):
    for c in chaves:
        v = d.get(c)
        if v not in (None, "", []):
            return v
    return None


def normalizar_registro(d: dict, cnpj: str, fonte: str) -> dict:
    """Unifica o formato das duas fontes. Usa .get porque o schema pode variar."""
    natureza = _primeiro(d, "natureza_juridica", "descricao_natureza_juridica") or ""
    if isinstance(natureza, dict):
        natureza = natureza.get("descricao", "")
    qsa = d.get("qsa") or d.get("quadro_societario") or []
    socios = [
        (q.get("nome_socio") or q.get("nome") or "").strip()
        for q in qsa if isinstance(q, dict)
    ]
    telefones = [str(t) for t in (
        _primeiro(d, "ddd_telefone_1", "telefone_1", "telefone"),
        _primeiro(d, "ddd_telefone_2", "telefone_2"),
    ) if t]
    individual = bool(d.get("opcao_pelo_mei")) or "individual" in str(natureza).lower()

    return {
        "cnpj": limpar_cnpj(cnpj),
        "razao_social": d.get("razao_social"),
        "nome_fantasia": d.get("nome_fantasia"),
        "cnae_codigo": d.get("cnae_fiscal"),
        "cnae_descricao": d.get("cnae_fiscal_descricao"),
        "porte": _primeiro(d, "descricao_porte", "porte"),
        "natureza_juridica": natureza or None,
        "situacao_cadastral": d.get("descricao_situacao_cadastral"),
        "data_abertura": d.get("data_inicio_atividade"),
        "uf": d.get("uf"),
        "municipio_cnpj": d.get("municipio"),
        "titular_unico": individual,   # MEI / empresário individual / EIRELI
        "socios": [s for s in socios if s],
        "telefones": telefones,
        "email": _primeiro(d, "email", "correio_eletronico"),
        "fonte_cadastro": fonte,
    }


def consultar_cnpj(cnpj: str) -> tuple[dict | None, str]:
    """Devolve (registro_normalizado, status). status: 'ok', 'nao_encontrado' ou 'erro'."""
    cnpj = limpar_cnpj(cnpj)
    pior = "nao_encontrado"
    for nome, modelo in FONTES_CNPJ:
        dados, status = _get_json(modelo.format(cnpj))
        if dados:
            return normalizar_registro(dados, cnpj, nome), "ok"
        if status == "erro":
            pior = "erro"
    return None, pior