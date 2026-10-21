"""
PIPELINE: etapas 1 (extração), 2 (BrasilAPI) e 3 (IA)
=====================================================================
Roda a partir da RAIZ do projeto:
    python pipeline.py --pagina "https://programacentelha.com.br/df/" --max-ia 3
    python pipeline.py --pagina "https://programacentelha.com.br/df/"
    python pipeline.py --pagina "..." --sem-ia      # só extração + CNPJ

Gera data/leads_processados.json (ordenado pela nota de fit), que o
painel Streamlit lê:
    streamlit run app.py     # etapa 4: abre em http://localhost:8501
=====================================================================
"""

import argparse
import json
import logging
from pathlib import Path

from extracao.scraper import coletar_leads, adicionar_args_cli, aplicar_args_cli
from enriquecimento.brasilapi import enriquecer_leads
from classificacao.llm import classificar_leads

SAIDA = Path(__file__).resolve().parent / "data" / "leads_processados.json"


def _ordem(lead: dict):
    nota = lead.get("nota_fit")
    return (nota is None, -(nota or 0))  # maiores notas primeiro, sem nota por último


def rodar(pdf=None, pagina=None, palavras=None, limite=1, demo=False,
          sem_ia=False, max_ia=None) -> list[dict]:
    leads = coletar_leads(pdf=pdf, pagina=pagina, palavras_chave=palavras,
                          limite_pdfs=limite, demo=demo)        # Etapa 1
    leads = enriquecer_leads(leads)                             # Etapa 2
    if not sem_ia:
        leads = classificar_leads(leads, max_chamadas=max_ia)   # Etapa 3
        leads.sort(key=_ordem)

    SAIDA.parent.mkdir(parents=True, exist_ok=True)
    SAIDA.write_text(json.dumps(leads, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.getLogger("pipeline").info("%d leads salvos em %s", len(leads), SAIDA)
    return leads


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    ap = argparse.ArgumentParser()
    adicionar_args_cli(ap)
    ap.add_argument("--sem-ia", action="store_true", help="pula a etapa 3")
    ap.add_argument("--max-ia", type=int, help="máximo de chamadas à OpenAI (para testar barato)")
    a = ap.parse_args()
    rodar(pdf=a.pdf, pagina=a.pagina, palavras=aplicar_args_cli(a), limite=a.limite,
          demo=a.demo, sem_ia=a.sem_ia, max_ia=a.max_ia)