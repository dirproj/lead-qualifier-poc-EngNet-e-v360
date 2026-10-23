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
  media = pessoa exata (ou parcial + startup), ou nome da startup + empresa
          aberta a partir de 2025 (compatível com o edital) -> atribui o CNPJ
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
MIN_NOMES_PARA_BATE_PARCIAL_SEGURO = 3
# Empresas de edital (ex.: Centelha DF 2025/26) costumam ser abertas depois da aprovação.
# O nome da startup + abertura a partir desta data já é um vínculo razoável (ex.: "X INOVA SIMPLES").
ABERTURA_RECENTE_A_PARTIR_DE = "2025-01-01"


def normalizar_nome(texto: str | None) -> str:
    """Maiúsculo, sem acento, sem pontuação e sem dígitos."""
    s = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().upper()
    s = re.sub(r"[^A-Z\s]", " ", s)
    return " ".join(s.split())


def _tokens(texto: str | None) -> list[str]:
    return [t for t in normalizar_nome(texto).split() if t not in STOPWORDS]

def comparar_pessoa(nome_a: str | None, nome_b: str | None) -> str | None:
    """'exato' (mesmos tokens, qualquer ordem), 'parcial' (todos os tokens do
    menor nome estão no maior) ou None."""
    a, b = set(_tokens(nome_a)), set(_tokens(nome_b))
    if not a or not b:
        return None
    
    # Se o nome for idêntico após remover acentos e pontuação
    if a == b and len(a) >= 2:
        return "exato"
    
    # Se o conjunto menor estiver contido no maior (ignorando ordem)
    menor, maior = (a, b) if len(a) <= len(b) else (b, a)
    if len(menor) >= 2 and menor.issubset(maior):
        return "parcial"
        
    return None

def nome_da_startup(titulo: str | None) -> str | None:
    """Extrai o nome da startup do título do projeto.
    
    Exemplos:
    - "GoalSet: Rastreabilidade e Monetização de Serviços Ambientais" -> "GoalSet"
    - "Sintática AI Studio" -> "Sintática AI Studio"
    - "Taiuva Vitae" -> "Taiuva Vitae"
    - "ECOBUEIROS SUSTENTÁVEIS NO DF" -> "ECOBUEIROS"
    """
    if not titulo:
        return None
    
    titulo_limpo = normalizar_nome(titulo)
    
    # 1. Tenta separar por ':' ou '-' (o que vier primeiro)
    #    Ex: "GOALSET: RASTREABILIDADE..." -> "GOALSET"
    for sep in [":", " - ", " – ", " | "]:
        if sep in titulo_limpo:
            nome = titulo_limpo.split(sep)[0].strip()
            toks = _tokens(nome)
            if toks:
                return " ".join(toks)  # Retorna o nome antes do separador
            break
    
    # 2. Se não tem separador, pega as primeiras palavras significativas
    toks = _tokens(titulo_limpo)
    if not toks:
        return None
    
    # Se a primeira palavra for genérica, pega as duas primeiras
    if toks[0] in GENERICOS and len(toks) > 1:
        return " ".join(toks[:2])
    
    # Caso padrão: retorna apenas a primeira palavra (nome da startup)
    return toks[0]


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
    nomes_em_comum = 0
    for socio in registro.get("socios") or []:
        r = comparar_pessoa(proponente, socio)
        if r == "exato" or (r == "parcial" and melhor is None):
            melhor = r
            nomes_em_comum = len(set(_tokens(proponente)) & set(_tokens(socio)))
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
    elif startup and (registro.get("data_abertura") or "") >= ABERTURA_RECENTE_A_PARTIR_DE:
        conf = "media"
        motivos.append(f"empresa aberta em {registro.get('data_abertura')} (compatível com o edital)")
    elif startup:
        conf = "baixa"
    else:
        return {"confianca": None, "pessoa": pessoa, "startup": startup, "motivos": motivos,
                "rejeicao": "sem vínculo com o proponente nem com o nome da startup"}
    return {"confianca": conf, "pessoa": pessoa, "startup": startup, "motivos": motivos, "rejeicao": None,
            "alerta": _alerta_de_vinculo_fraco(pessoa, nomes_em_comum)}


def _alerta_de_vinculo_fraco(pessoa: str | None, nomes_em_comum: int) -> str | None:
    if pessoa == "parcial" and nomes_em_comum < MIN_NOMES_PARA_BATE_PARCIAL_SEGURO:
        return (f"o sócio só bate em {nomes_em_comum} nomes com o proponente; "
                "pode ser homônimo, confirme antes de contatar")
    if pessoa is None:
        return ("vínculo só pelo nome da startup e pela data de abertura, sem o proponente no "
                "quadro societário; confirme antes de contatar")
    return None