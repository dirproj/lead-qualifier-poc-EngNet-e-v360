"""
NOTA POR REGRAS (reserva quando a IA não está disponível)
=====================================================================
Se a IA falhar (limite do plano gratuito, rede, chave), o lead NÃO fica
sem nota: esta função calcula uma nota conservadora a partir de palavras-
chave do título/temática/CNAE e marca claramente `origem_nota = "regras"`.

Regras de honestidade:
  - só usa os mesmos campos que iriam para a IA (nunca o nome do proponente);
  - a nota máxima é 6 (7 só se o CNAE confirmar atividade de tecnologia);
  - a confiança nunca passa de "media" e o painel mostra que NÃO veio da IA.
=====================================================================
"""

from __future__ import annotations

import re

from classificacao.guardrails import _norm, faixa_da_nota
from classificacao.perfil import CATALOGO_SERVICOS

# prefixos aceitos (casam o começo da palavra): "automatiz" pega "automatizar"
GRUPOS: dict[str, tuple[str, ...]] = {
    "desenvolvimento_software": ("software", "aplicativ", "app", "plataforma", "sistema", "saas",
                                 "mvp", "web", "mobile", "digital", "api"),
    "dados_ia": ("inteligencia artificial", "machine learning", "aprendizado de maquina",
                 "dados", "analytics", "big data", "visao computacional", "algoritmo", "preditiv"),
    "cloud_infra": ("nuvem", "cloud", "infraestrutura", "servidor", "escalab"),
    "seguranca_lgpd": ("seguranca da informacao", "ciberseguranca", "lgpd", "privacidade",
                       "criptografia", "protecao de dados", "blockchain"),
    "automacao": ("automacao", "automatiz", "rpa", "workflow", "processos", "gestao"),
    "consultoria_produto": ("inovacao", "startup", "modelo de negocio", "produto",
                            "estrategia", "marketplace"),
    "hardware_iot": ("iot", "internet das coisas", "sensor", "hardware", "embarcado",
                     "dispositivo", "drone", "robo"),
}

CAMPOS = ("titulo_projeto", "tematica", "cnae_descricao")


def _casa(texto_norm: str, prefixo: str) -> bool:
    # termos curtos ("api", "app", "iot") só valem como palavra inteira:
    # senão "api" casaria com "apicultura"
    fim = r"\b" if len(prefixo) <= 4 else ""
    return re.search(r"\b" + re.escape(prefixo) + fim, texto_norm) is not None


def _cnae_tecnologia(lead: dict) -> bool:
    cod = str(lead.get("cnae_codigo") or "")
    desc = _norm(lead.get("cnae_descricao") or "")
    return (cod.startswith(("62", "63"))
            or "tecnologia da informacao" in desc
            or ("desenvolvimento de" in desc and "software" in desc))


def pontuar(lead: dict, motivo_ia: str = "IA indisponível") -> dict:
    """Devolve o mesmo formato de `validar_resposta`, com status_ia='regras'."""
    texto = " ".join(str(lead.get(c) or "") for c in CAMPOS)
    texto_norm = _norm(texto)
    achados: dict[str, list[str]] = {}
    for servico, prefixos in GRUPOS.items():
        achou = [p for p in prefixos if _casa(texto_norm, p)]
        if achou and servico in CATALOGO_SERVICOS:
            achados[servico] = achou

    n = len(achados)
    nota = {0: 2, 1: 4, 2: 5}.get(n, 6)
    tecnologia = _cnae_tecnologia(lead)
    if tecnologia and n >= 2:
        nota = 7
    insuficiente = n == 0

    ordem = sorted(achados, key=lambda s: -len(achados[s]))
    evidencias = []
    for s in ordem:
        for termo in achados[s]:
            if termo not in evidencias:
                evidencias.append(termo)
    evidencias = evidencias[:3]

    if achados:
        nomes = ", ".join(CATALOGO_SERVICOS[s] for s in ordem[:3])
        just = f"Nota por regras: o projeto cita termos ligados a {nomes}."
        if tecnologia:
            just += " O CNAE indica atividade de tecnologia."
    else:
        just = "Nota por regras: o título e a temática não trazem termos ligados aos serviços."

    return {
        "nota_fit": nota,
        "faixa_fit": faixa_da_nota(nota),
        "confianca": "media" if nota >= 7 else "baixa",
        "dados_insuficientes": insuficiente,
        "justificativa": just,
        "servicos_sugeridos": ordem[:3],
        "evidencias": evidencias,
        "status_ia": "regras",
        "origem_nota": "regras",
        "avisos_ia": [f"{motivo_ia}; nota calculada por regras, não pela IA"],
    }
