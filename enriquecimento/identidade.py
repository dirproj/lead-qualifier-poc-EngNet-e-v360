"""
IDENTIDADE: este CNPJ é mesmo da pessoa/startup do projeto?
=====================================================================
Candidatos a CNPJ vêm de buscas por nome (descoberta.py) e podem ser de
homônimos. Aqui cada candidato é CONFERIDO contra o cadastro oficial:

  pessoa  = o nome do proponente bate com um sócio do QSA, ou com a razão
            social de MEI/empresário individual (que é "NOME + CPF").
  startup = o nome da startup (extraído do título do projeto) aparece na
            razão social ou no nome fantasia.
  Além disso, a empresa precisa estar ATIVA e na UF do edital.

  alta  = pessoa exata + startup      -> atribui o CNPJ
  media = pessoa exata (ou parcial + startup) -> atribui o CNPJ
  baixa = só o nome da startup        -> NÃO atribui; fica como candidato
=====================================================================
"""

from __future__ import annotations

import re
import unicodedata

STOPWORDS = {"DE", "DA", "DO", "DAS", "DOS", "E"}
GENERICOS = {
    "AI", "IA", "STUDIO", "TECH", "TECNOLOGIA", "TECNOLOGIAS", "SOLUCOES", "SOLUCAO",
    "DIGITAL", "SISTEMAS", "SOFTWARE", "LTDA", "ME", "EPP", "MEI", "SERVICOS", "LAB",
    "LABS", "GROUP", "GRUPO", "BRASIL", "PLATAFORMA", "APP",
}
RANK = {"alta": 3, "media": 2, "baixa": 1, None: 0}


def normalizar_nome(texto: str | None) -> str:
    """Maiúsculo, sem acento, sem pontuação e sem dígitos."""
    s = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().upper()
    s = re.sub(r"[^A-Z\s]", " ", s)
    return " ".join(s.split())


def _tokens(texto: str | None) -> list[str]:
    return [t for t in normalizar_nome(texto).split() if t not in STOPWORDS]


def comparar_pessoa(nome_a: str | None, nome_b: str | None) -> str | None:
    """'exato' (mesmos tokens, qualquer ordem), 'parcial' (todos os tokens do
    menor nome, com 3+ tokens, estão no maior) ou None."""
    a, b = set(_tokens(nome_a)), set(_tokens(nome_b))
    if not a or not b:
        return None
    if a == b and len(a) >= 2:
        return "exato"
    menor, maior = (a, b) if len(a) <= len(b) else (b, a)
    if len(menor) >= 3 and menor <= maior:
        return "parcial"
    return None


def nome_da_startup(titulo: str | None) -> str | None:
    """'GoalSet: Rastreabilidade...' -> 'GoalSet'. Títulos longos e genéricos
    (frases descritivas) não são nome de startup e devolvem None."""
    if not titulo:
        return None
    nome = re.split(r"\s[:\-–|]\s|:\s", titulo, maxsplit=1)[0].strip()
    toks = _tokens(nome)
    if not toks or len(toks) > 4 or all(t in GENERICOS for t in toks):
        return None
    return nome


def _contem_startup(nome_startup: str | None, *campos: str | None) -> bool:
    sig = [t for t in _tokens(nome_startup) if t not in GENERICOS]
    if not sig or sum(len(t) for t in sig) < 4:
        return False
    alvo = " ".join(normalizar_nome(c) for c in campos if c)
    return all(t in alvo for t in sig)


def avaliar(lead: dict, registro: dict, uf_alvo: str = "DF") -> dict:
    """Devolve {'confianca','pessoa','startup','motivos','rejeicao'}."""
    proponente = lead.get("proponente")
    motivos: list[str] = []

    melhor = None
    for socio in registro.get("socios") or []:
        r = comparar_pessoa(proponente, socio)
        if r == "exato" or (r == "parcial" and melhor is None):
            melhor = r
    pessoa = melhor
    if pessoa:
        motivos.append(f"nome do proponente bate ({pessoa}) com sócio do QSA")
    if registro.get("titular_unico") or not registro.get("socios"):
        r = comparar_pessoa(proponente, registro.get("razao_social"))
        if r == "exato":
            pessoa = "exato"
            motivos.append("razão social é o nome do proponente (MEI/empresário individual)")

    startup = _contem_startup(
        nome_da_startup(lead.get("titulo_projeto")),
        registro.get("razao_social"), registro.get("nome_fantasia"))
    if startup:
        motivos.append("nome da startup aparece na razão social/fantasia")

    situacao = (registro.get("situacao_cadastral") or "").upper()
    if "ATIVA" not in situacao:
        return {"confianca": None, "pessoa": pessoa, "startup": startup, "motivos": motivos,
                "rejeicao": f"situação cadastral: {registro.get('situacao_cadastral') or 'desconhecida'}"}
    if (registro.get("uf") or "").upper() != uf_alvo.upper():
        return {"confianca": None, "pessoa": pessoa, "startup": startup, "motivos": motivos,
                "rejeicao": f"UF {registro.get('uf') or '?'} diferente de {uf_alvo}"}
    motivos.append(f"ativa, UF {uf_alvo}")

    if pessoa == "exato" and startup:
        conf = "alta"
    elif pessoa == "exato" or (pessoa == "parcial" and startup):
        conf = "media"
    elif startup:
        conf = "baixa"
    else:
        return {"confianca": None, "pessoa": pessoa, "startup": startup, "motivos": motivos,
                "rejeicao": "sem vínculo com o proponente nem com o nome da startup"}
    return {"confianca": conf, "pessoa": pessoa, "startup": startup, "motivos": motivos, "rejeicao": None}