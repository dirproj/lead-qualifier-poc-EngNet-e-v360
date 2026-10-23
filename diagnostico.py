"""
DIAGNÓSTICO DA INSTALAÇÃO: rode quando "o sistema não está rodando direito"
=====================================================================
    python diagnostico.py            # confere Python, pacotes, .env, arquivos e versões
    python diagnostico.py --rede     # também testa as APIs de CNPJ e a IA (faz chamadas reais)

O que ele pega:
  - pacote não instalado ou navegador do Playwright ausente;
  - chave de IA ausente (Gemini/Groq) ou provedor errado;
  - ARQUIVOS DESENCONTRADOS: um módulo de uma versão chamando outro de versão diferente
    (ex.: "Buscador.__init__() got an unexpected keyword argument 'uf'");
  - módulos do pipeline importando streamlit (só o app.py deveria).
=====================================================================
"""

from __future__ import annotations

import importlib
import inspect
import os
import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
OK, FALHA, AVISO = "✅", "❌", "⚠️ "
problemas = 0


def linha(simbolo: str, texto: str) -> None:
    global problemas
    if simbolo == FALHA:
        problemas += 1
    print(f"{simbolo} {texto}")


def checar_python() -> None:
    v = sys.version_info
    linha(OK if v >= (3, 10) else FALHA, f"Python {v.major}.{v.minor}.{v.micro}" + ("" if v >= (3, 10) else " (precisa 3.10+)"))


def checar_pacotes() -> None:
    pacotes = {"playwright": "playwright", "pdfplumber": "pdfplumber", "requests": "requests",
               "openai": "openai", "dotenv": "python-dotenv", "streamlit": "streamlit",
               "pandas": "pandas", "bs4": "beautifulsoup4", "ddgs": "ddgs"}
    faltando = []
    for mod, pip_nome in pacotes.items():
        try:
            importlib.import_module(mod)
        except ImportError:
            faltando.append(pip_nome)
    if faltando:
        linha(FALHA, f"Pacotes ausentes: {', '.join(faltando)}  ->  pip install -r requirements.txt")
    else:
        linha(OK, "Todos os pacotes do requirements.txt estão instalados")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
        linha(OK, "Chromium do Playwright disponível")
    except Exception as e:  # noqa: BLE001
        linha(FALHA, f"Chromium do Playwright indisponível ({type(e).__name__}) ->  python -m playwright install chromium")


def checar_env() -> None:
    sys.path.insert(0, str(RAIZ))
    try:
        from classificacao import llm
    except Exception as e:  # noqa: BLE001
        linha(FALHA, f"classificacao/llm.py não importa: {type(e).__name__}: {e}")
        return
    if not hasattr(llm, "_provedor"):
        linha(FALHA, "classificacao/llm.py é uma versão ANTIGA (sem Gemini/Groq). Substitua pelo arquivo novo.")
        return
    env = RAIZ / ".env"
    linha(OK if env.exists() else AVISO, f".env {'encontrado' if env.exists() else 'NÃO encontrado na raiz (copie .env.example)'}")
    prov = llm._provedor()
    tem_chave = bool(llm._chave_api())
    linha(OK if tem_chave else FALHA,
          f"Provedor de IA: {prov} | modelo: {llm._modelo()} | chave: {'definida' if tem_chave else 'AUSENTE (GEMINI_API_KEY ou GROQ_API_KEY no .env)'}")


def checar_versoes() -> None:
    """Confere se os módulos do enriquecimento são da mesma versão (assinaturas esperadas)."""
    sys.path.insert(0, str(RAIZ))
    esperado = {
        "enriquecimento.descoberta": [("Buscador.__init__", ["uf", "visivel"]), ("Buscador.buscar", ["tipo", "titulo"]),
                                      ("Buscador.contatos_da_pagina", [])],
        "enriquecimento.orquestrador": [("enriquecer_leads", ["automatico", "uf_alvo", "usar_cache"]),
                                        ("exportar_cnpjs_encontrados", [])],
        "enriquecimento.identidade": [("avaliar", ["uf_alvo"])],
        "enriquecimento.contatos": [("avaliar_contato", ["vinculo_ok"])],
        "enriquecimento.brasilapi": [("consultar_cnpj", [])],
        "classificacao.llm": [("classificar_leads", ["max_chamadas", "pausa"])],
        "extracao.scraper": [("coletar_leads", ["limite_pdfs"]), ("adicionar_args_cli", [])],
    }
    for modulo, itens in esperado.items():
        try:
            m = importlib.import_module(modulo)
        except Exception as e:  # noqa: BLE001
            linha(FALHA, f"{modulo} não importa: {type(e).__name__}: {e}")
            continue
        for caminho, params in itens:
            obj = m
            try:
                for parte in caminho.split("."):
                    obj = getattr(obj, parte)
            except AttributeError:
                linha(FALHA, f"{modulo}: falta '{caminho}'  ->  arquivo de versão antiga, substitua")
                continue
            faltam = [p for p in params if p not in inspect.signature(obj).parameters]
            if faltam:
                linha(FALHA, f"{modulo}.{caminho} sem os parâmetros {faltam}  ->  arquivo de versão antiga, substitua")
    if not problemas:
        linha(OK, "Módulos do pipeline consistentes entre si")


def checar_streamlit_fora_do_app() -> None:
    suspeitos = []
    for arq in RAIZ.rglob("*.py"):
        if arq.name in ("app.py", "diagnostico.py") or ".venv" in arq.parts or "site-packages" in arq.parts:
            continue
        if re.search(r"^\s*(import streamlit|from streamlit)", arq.read_text(encoding="utf-8", errors="ignore"), re.M):
            suspeitos.append(str(arq.relative_to(RAIZ)))
    if suspeitos:
        linha(AVISO, f"Só o app.py deveria usar Streamlit. Importam streamlit: {', '.join(suspeitos)} "
                     "(causa os avisos 'missing ScriptRunContext' ao rodar o pipeline)")
    else:
        linha(OK, "Nenhum módulo do pipeline importa streamlit")


def checar_arquivos() -> None:
    for rel in ("data/leads_processados.json", "data/cnpjs_encontrados.csv"):
        existe = (RAIZ / rel).exists()
        linha(OK if existe else AVISO, f"{rel} {'existe' if existe else 'ainda não existe (rode o pipeline)'}")


def checar_rede() -> None:
    from enriquecimento.brasilapi import consultar_cnpj
    registro, status = consultar_cnpj("00000000000191")   # Banco do Brasil
    linha(OK if registro else FALHA, f"APIs de CNPJ (minhareceita/BrasilAPI): {status}" + (f" via {registro['fonte_cadastro']}" if registro else ""))
    from classificacao import llm
    codigo = llm.diagnosticar(False)
    linha(OK if codigo == 0 else FALHA, "IA respondeu ao teste" if codigo == 0 else "IA falhou (veja a mensagem acima)")


if __name__ == "__main__":
    print(f"Diagnóstico em {RAIZ}\n")
    checar_python()
    checar_pacotes()
    checar_env()
    checar_versoes()
    checar_streamlit_fora_do_app()
    checar_arquivos()
    if "--rede" in sys.argv:
        print()
        checar_rede()
    print(f"\n{'Tudo certo.' if not problemas else f'{problemas} problema(s) encontrado(s): corrija os itens ❌ acima.'}")
    sys.exit(1 if problemas else 0)