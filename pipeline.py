"""
PIPELINE: etapas 1 (extração), 2 (CNPJ + contatos) e 3 (IA com Groq)
=====================================================================
Roda a partir da RAIZ do projeto:
    python pipeline.py --pagina "https://programacentelha.com.br/df/" --max-ia 3
    python pipeline.py --pagina "https://programacentelha.com.br/df/"
    python pipeline.py --pagina "..." --sem-ia            # sem LLM
    python pipeline.py --pagina "..." --sem-busca-cnpj    # só CNPJs do CSV manual

Gera data/leads_processados.json (ordenado pela nota de fit), que o
painel Streamlit lê:
    streamlit run app.py     # etapa 4: abre em http://localhost:8501

Diagnósticos úteis:
    python -m classificacao.llm --testar                       # a IA está funcionando?
    python -m enriquecimento.orquestrador --diagnostico "Nome" # a busca de CNPJ encontra?
=====================================================================
"""

import argparse
import json
import logging
from pathlib import Path

from extracao.scraper import coletar_leads, adicionar_args_cli, aplicar_args_cli
from enriquecimento.orquestrador import enriquecer_leads, exportar_cnpjs_encontrados
from classificacao.llm import classificar_leads

SAIDA = Path(__file__).resolve().parent / "data" / "leads_processados.json"

def _ordem(lead: dict):
    nota = lead.get("nota_fit")
    return (nota is None, -(nota or 0))  # maiores notas primeiro, sem nota por último


def rodar(pdf=None, pagina=None, palavras=None, limite=1, demo=False,
          sem_ia=False, max_ia=None, busca_cnpj=True, uf="DF", cache_cnpj=True) -> list[dict]:
    leads = coletar_leads(pdf=pdf, pagina=pagina, palavras_chave=palavras,
                          limite_pdfs=limite, demo=demo)                     # Etapa 1
    leads = [{**lead, "uf_edital": uf.upper()} for lead in leads]
    leads = enriquecer_leads(leads, automatico=busca_cnpj, uf_alvo=uf,
                             usar_cache=cache_cnpj)                          # Etapa 2
    exportar_cnpjs_encontrados(leads)    # data/cnpjs_encontrados.csv (para auditar a busca)
    if not sem_ia:
        leads = classificar_leads(leads, max_chamadas=max_ia)                # Etapa 3
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
    ap.add_argument("--max-ia", type=int, help="máximo de chamadas ao LLM (para testar barato)")
    ap.add_argument("--sem-busca-cnpj", action="store_true", help="não procura CNPJ na web (usa só o CSV manual)")
    ap.add_argument("--sem-cache-cnpj", action="store_true", help="ignora o cache de buscas/cadastros")
    ap.add_argument("--uf", default="DF", help="UF do edital, usada para conferir o CNPJ (padrão DF)")
    a = ap.parse_args()
    rodar(pdf=a.pdf, pagina=a.pagina, palavras=aplicar_args_cli(a), limite=a.limite,
          demo=a.demo, sem_ia=a.sem_ia, max_ia=a.max_ia, busca_cnpj=not a.sem_busca_cnpj,
          uf=a.uf, cache_cnpj=not a.sem_cache_cnpj)