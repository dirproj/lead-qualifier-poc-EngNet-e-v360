"""
ETAPA 2: ENRIQUECIMENTO (BrasilAPI)
=====================================================================
Entrada: lista de dicts da etapa 1.
Saída:   mesma lista + razão social, CNAE, porte, situação cadastral
         + `status_enriquecimento`.

A lista oficial do edital não traz CNPJ, e a BrasilAPI só consulta POR
CNPJ. Para o MVP, o CNPJ vem de data/cnpjs_manuais.csv, preenchido pela
equipe para os leads que vão aparecer no pitch:

    proponente,cnpj
    Nome Completo Como Na Lista,12.345.678/0001-95

Teste (a partir da RAIZ do projeto):
    python -m enriquecimento.brasilapi
=====================================================================
"""

from __future__ import annotations

import csv
import logging
import time
import unicodedata
from pathlib import Path

import requests

from extracao.validacao import cnpj_valido, limpar_cnpj

log = logging.getLogger("enriquecimento")

RAIZ = Path(__file__).resolve().parent.parent
CSV_PADRAO = RAIZ / "data" / "cnpjs_manuais.csv"
URL_BRASILAPI = "https://brasilapi.com.br/api/cnpj/v1/{}"

# Valores possíveis de `status_enriquecimento` (o painel usa para filtrar)
OK = "ok"
SEM_CNPJ = "sem_cnpj"
CNPJ_INVALIDO = "cnpj_invalido"
NAO_ENCONTRADO = "nao_encontrado_ou_api_fora"


def _chave(nome: str) -> str:
    """Normaliza nome (sem acento, minúsculo, espaços únicos) para casar."""
    sem_acento = unicodedata.normalize("NFKD", nome or "").encode("ascii", "ignore").decode()
    return " ".join(sem_acento.lower().split())


def carregar_mapa_cnpj(caminho: Path | str = CSV_PADRAO) -> dict[str, str]:
    mapa: dict[str, str] = {}
    try:
        with open(caminho, newline="", encoding="utf-8") as f:
            for linha in csv.DictReader(f):
                if linha.get("proponente") and linha.get("cnpj"):
                    mapa[_chave(linha["proponente"])] = limpar_cnpj(linha["cnpj"])
        log.info("%d CNPJs carregados de %s.", len(mapa), Path(caminho).name)
    except FileNotFoundError:
        log.warning("Arquivo %s não encontrado; leads ficarão sem CNPJ.", caminho)
    except Exception as e:
        log.error("Erro lendo %s: %s", caminho, e)
    return mapa


def consultar_brasilapi(cnpj: str, tentativas: int = 3, timeout: int = 10) -> dict | None:
    """Consulta com retry para 429 e erros de rede. None se não achar."""
    for i in range(tentativas):
        try:
            resp = requests.get(URL_BRASILAPI.format(cnpj), timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                log.warning("CNPJ %s não encontrado na BrasilAPI.", cnpj)
                return None
            if resp.status_code == 429:  # limite de requisições: espera e tenta de novo
                time.sleep(2 * (i + 1))
                continue
            log.warning("BrasilAPI respondeu HTTP %s para %s.", resp.status_code, cnpj)
        except requests.RequestException as e:  # fora do ar, timeout, DNS
            log.warning("Erro de rede (%s). Tentativa %d/%d.", e, i + 1, tentativas)
            time.sleep(1)
    return None


def normalizar_resposta(d: dict) -> dict:
    """Só os campos úteis ao SDR. Usa .get porque o schema pode variar."""
    return {
        "razao_social": d.get("razao_social"),
        "nome_fantasia": d.get("nome_fantasia"),
        "cnae_codigo": d.get("cnae_fiscal"),
        "cnae_descricao": d.get("cnae_fiscal_descricao"),
        "porte": d.get("descricao_porte") or d.get("porte"),
        "situacao_cadastral": d.get("descricao_situacao_cadastral"),
        "data_abertura": d.get("data_inicio_atividade"),
        "uf": d.get("uf"),
        "municipio_cnpj": d.get("municipio"),
    }


def enriquecer_leads(leads: list[dict], mapa_cnpj: dict[str, str] | None = None,
                     pausa: float = 0.3) -> list[dict]:
    mapa_cnpj = carregar_mapa_cnpj() if mapa_cnpj is None else mapa_cnpj
    saida: list[dict] = []

    for lead in leads:
        lead = dict(lead)  # não altera a lista original
        cnpj = lead.get("cnpj") or mapa_cnpj.get(_chave(lead.get("proponente", "")))

        if not cnpj:
            lead["status_enriquecimento"] = SEM_CNPJ
        elif not cnpj_valido(cnpj):
            log.warning("CNPJ com dígito verificador inválido: %s", cnpj)
            lead["status_enriquecimento"] = CNPJ_INVALIDO
        else:
            lead["cnpj"] = limpar_cnpj(cnpj)
            dados = consultar_brasilapi(lead["cnpj"])
            if dados:
                lead.update(normalizar_resposta(dados))
                lead["status_enriquecimento"] = OK
            else:
                lead["status_enriquecimento"] = NAO_ENCONTRADO
            time.sleep(pausa)  # respeita o rate limit

        saida.append(lead)

    ok = sum(1 for l in saida if l["status_enriquecimento"] == OK)
    log.info("Enriquecidos: %d de %d leads.", ok, len(saida))
    return saida


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    exemplo = [{"titulo_projeto": "Teste", "proponente": "Fulano de Tal Demo",
                "tematica": "TI", "municipio": "Brasília", "cnpj": None}]
    for l in enriquecer_leads(exemplo):
        print(l["proponente"], "|", l["status_enriquecimento"])
