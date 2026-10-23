"""
ETAPA 2: ENRIQUECIMENTO (CNPJ + cadastro + contatos), automatizado
=====================================================================
Para cada lead:
  1. CNPJ informado pela equipe em data/cnpjs_manuais.csv (se houver); senão
  2. busca automática por nome (proponente e startup) em descoberta.py;
  3. cada candidato é conferido no cadastro oficial (identidade.py):
     só é atribuído com vínculo a pessoa/startup, ativo e na UF do edital;
  4. com o CNPJ vinculado, aplica a regra de contato (contato.py).

Teste de uma busca isolada (mostra cada fonte e cada candidato):
    python -m enriquecimento.orquestrador --diagnostico "Nome do Proponente" \\
           --titulo "Nome da Startup: subtítulo do projeto"
=====================================================================
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import time
import unicodedata
from pathlib import Path

from extracao.validacao import cnpj_valido, limpar_cnpj
from enriquecimento import brasilapi, contatos, identidade
from enriquecimento.descoberta import Buscador

log = logging.getLogger("enriquecimento")

RAIZ = Path(__file__).resolve().parent.parent
CSV_PADRAO = RAIZ / "data" / "cnpjs_manuais.csv"
CACHE_PATH = RAIZ / "data" / "cache_cnpj.json"
MAX_VERIFICAR = 10  # candidatos conferidos por lead (poupa as APIs gratuitas)

# Valores possíveis de `status_enriquecimento` (o painel usa para filtrar)
OK = "ok"
SEM_CNPJ = "sem_cnpj"
CNPJ_INVALIDO = "cnpj_invalido"
NAO_ENCONTRADO = "nao_encontrado_ou_api_fora"
AMBIGUO = "ambiguo"
NAO_VERIFICADO = "nao_verificado"

_CAMPOS_CADASTRO = (
    "razao_social", "nome_fantasia", "cnae_codigo", "cnae_descricao", "porte",
    "natureza_juridica", "situacao_cadastral", "data_abertura", "uf", "municipio_cnpj",
)


def _chave(nome: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", nome or "").encode("ascii", "ignore").decode()
    return " ".join(sem_acento.lower().split())


def carregar_mapa_cnpj(caminho: Path | str = CSV_PADRAO) -> dict[str, str]:
    """CNPJs levantados pela equipe (csv: proponente,cnpj). Tem prioridade sobre a busca."""
    mapa: dict[str, str] = {}
    try:
        with open(caminho, newline="", encoding="utf-8") as f:
            for linha in csv.DictReader(f):
                if linha.get("proponente") and linha.get("cnpj"):
                    mapa[_chave(linha["proponente"])] = limpar_cnpj(linha["cnpj"])
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log.error("Erro lendo %s: %s", caminho, e)
    return mapa


# ---------------------------------------------------------------
# Cache (poupa as APIs/sites e deixa a demo estável)
# ---------------------------------------------------------------
class Cache:
    def __init__(self, ativo: bool = True, caminho: Path = CACHE_PATH):
        self.ativo, self.caminho = ativo, caminho
        self.dados: dict = {"busca": {}, "registro": {}}
        if ativo:
            try:
                self.dados = json.loads(caminho.read_text(encoding="utf-8"))
                self.dados.setdefault("busca", {})
                self.dados.setdefault("registro", {})
            except (FileNotFoundError, json.JSONDecodeError):
                pass

    def salvar(self) -> None:
        if not self.ativo:
            return
        try:
            self.caminho.parent.mkdir(parents=True, exist_ok=True)
            self.caminho.write_text(json.dumps(self.dados, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            log.warning("Não consegui salvar o cache de CNPJ: %s", e)


def _registro(cnpj: str, consulta, cache: Cache) -> tuple[dict | None, str]:
    if cnpj in cache.dados["registro"]:
        return cache.dados["registro"][cnpj], "ok"
    registro, status = consulta(cnpj)
    if registro:
        cache.dados["registro"][cnpj] = registro
    return registro, status


def _buscar(termo: str, tipo: str, buscador, cache: Cache,
            titulo: str | None = None) -> tuple[list[str], list[str]]:
    chave = f"{tipo}:{_chave(termo)}"
    if chave in cache.dados["busca"]:
        return cache.dados["busca"][chave], ["(cache)"]
    cnpjs, diag = buscador.buscar(termo, tipo, titulo)
    # só guarda no cache se alguma fonte respondeu de fato
    if cnpjs or any("HTTP 200" in d for d in diag):
        cache.dados["busca"][chave] = cnpjs
    return cnpjs, diag


# ---------------------------------------------------------------
# Resolução do CNPJ de um lead
# ---------------------------------------------------------------
def _resumo(cnpj: str, registro: dict, ident: dict) -> dict:
    return {"cnpj": cnpj, "razao_social": registro.get("razao_social"),
            "motivo": "; ".join(ident["motivos"]) or ident.get("rejeicao") or ""}


def _resolver(lead: dict, mapa: dict, buscador, consulta, cache: Cache, uf_alvo: str):
    """Devolve (campos_para_o_lead, registro|None, identidade|None)."""
    proponente = lead.get("proponente", "")
    titulo = lead.get("titulo_projeto", "")
    print(f"DEBUG RESOLVER: proponente='{proponente}', titulo='{titulo}")
    cnpj = lead.get("cnpj") or mapa.get(_chave(proponente))

    # ---- caminho 1: CNPJ informado pela equipe ----
    if cnpj:
        
        cnpj = limpar_cnpj(cnpj)
        if not cnpj_valido(cnpj):
            log.warning("CNPJ com dígito verificador inválido: %s", cnpj)
            return {"status_enriquecimento": CNPJ_INVALIDO}, None, None
        registro, _ = _registro(cnpj, consulta, cache)
        if not registro:
            return {"status_enriquecimento": NAO_ENCONTRADO, "cnpj": cnpj}, None, None
        ident = identidade.avaliar(lead, registro, uf_alvo)
        return ({"cnpj": cnpj, "cnpj_origem": "manual", "cnpj_confianca": "manual",
                 "cnpj_evidencia": "informado pela equipe (data/cnpjs_manuais.csv)",
                 "status_enriquecimento": OK}, registro, ident)

    # ---- caminho 2: busca automática ----
    if buscador is None:
        return {"status_enriquecimento": SEM_CNPJ}, None, None

    startup = identidade.nome_da_startup(lead.get("titulo_projeto"))
    candidatos: list[str] = []
    # 1) pelo NOME DO PROPONENTE (sócio/MEI); 2) pelo nome da startup tirado do TÍTULO
    for termo, tipo in ((startup, "startup"), (proponente, "pessoa")):
        if not termo:
            continue
        achados, _ = _buscar(termo, tipo, buscador, cache, lead.get("titulo_projeto"))
        candidatos += [c for c in achados if c not in candidatos]

    avaliados = []
    for c in candidatos[:MAX_VERIFICAR]:
        registro, _ = _registro(c, consulta, cache)
        if registro:
            avaliados.append((c, registro, identidade.avaliar(lead, registro, uf_alvo)))

    aceitos = [a for a in avaliados if a[2]["confianca"] in ("alta", "media")]
    aceitos.sort(key=lambda a: (identidade.RANK[a[2]["confianca"]], a[2]["startup"],
                                a[1].get("data_abertura") or ""), reverse=True)
    if aceitos:
        topo = aceitos[0]
        empate = len(aceitos) > 1 and (
            identidade.RANK[aceitos[1][2]["confianca"]], aceitos[1][2]["startup"]) == (
            identidade.RANK[topo[2]["confianca"]], topo[2]["startup"])
        if empate:
            return ({"status_enriquecimento": AMBIGUO,
                     "cnpj_evidencia": f"{len(aceitos)} CNPJs igualmente prováveis; confirme manualmente",
                     "cnpj_candidatos": [_resumo(c, r, i) for c, r, i in aceitos[:3]]}, None, None)
        c, registro, ident = topo
        return ({"cnpj": c, "cnpj_origem": "automatico", "cnpj_confianca": ident["confianca"],
                 "cnpj_evidencia": "; ".join(ident["motivos"]),
                 "status_enriquecimento": OK}, registro, ident)

    fracos = [a for a in avaliados if a[2]["confianca"] == "baixa"]
    if fracos:
        return ({"status_enriquecimento": NAO_VERIFICADO,
                 "cnpj_evidencia": "só o nome da startup bate; sem vínculo com o proponente",
                 "cnpj_candidatos": [_resumo(c, r, i) for c, r, i in fracos[:3]]}, None, None)
    return ({"status_enriquecimento": SEM_CNPJ,
             "cnpj_evidencia": (f"{len(candidatos)} candidatos nas buscas, nenhum confirmado"
                                if candidatos else "buscas sem candidatos")}, None, None)


def _completar_contato_pelo_site(registro: dict, buscador) -> None:
    """Se o cadastro oficial (API) veio sem telefone/e-mail, tenta ler o que a página da
    empresa na Casa dos Dados exibe. Valores ocultos (****) seguem para a regra de contato,
    que os marca como 'mascarado'."""
    if registro.get("telefones") and registro.get("email"):
        return
    ler = getattr(buscador, "contatos_da_pagina", None)
    if not ler:
        return
    dados = ler(registro["cnpj"]) or {}
    if not registro.get("telefones") and dados.get("telefones"):
        registro["telefones"] = dados["telefones"]
        registro["fonte_contato"] = "página da empresa (Casa dos Dados)"
    if not registro.get("email") and dados.get("emails"):
        registro["email"] = dados["emails"][0]
        registro["fonte_contato"] = "página da empresa (Casa dos Dados)"


# ---------------------------------------------------------------
# Lote (chamado pelo pipeline.py)
# ---------------------------------------------------------------
def enriquecer_leads(leads: list[dict], mapa_cnpj: dict[str, str] | None = None,
                     automatico: bool = True, uf_alvo: str = "DF", usar_cache: bool = True,
                     buscador=None, consulta=None, pausa: float = 0.3) -> list[dict]:
    mapa = carregar_mapa_cnpj() if mapa_cnpj is None else mapa_cnpj
    consulta = consulta or brasilapi.consultar_cnpj
    cache = Cache(usar_cache)

    proprio = None
    if automatico and buscador is None:
        proprio = buscador = Buscador(uf=uf_alvo)
    if not automatico:
        buscador = None

    resultados: list[tuple[dict, dict, dict | None, dict | None]] = []
    try:
        for lead in leads:
            lead = dict(lead)
            campos, registro, ident = _resolver(lead, mapa, buscador, consulta, cache, uf_alvo)
            if registro:
                if buscador is not None:
                    _completar_contato_pelo_site(registro, buscador)
                lead.update({k: registro.get(k) for k in _CAMPOS_CADASTRO})
            lead.update(campos)
            lead.setdefault("status_enriquecimento", SEM_CNPJ)
            resultados.append((lead, registro, ident))
            time.sleep(pausa if registro or lead["status_enriquecimento"] != SEM_CNPJ else 0)
    finally:
        if proprio:
            proprio.fechar()
        cache.salvar()

    # ---- 2ª passada: contatos (precisa ver todos os CNPJs para achar números compartilhados) ----
    # Considera TODOS os cadastros consultados (inclusive candidatos rejeitados): um contador
    # costuma repetir o mesmo telefone/e-mail em vários CNPJs.
    vistos = {r["cnpj"]: r for r in cache.dados["registro"].values() if r.get("cnpj")}
    vistos.update({r["cnpj"]: r for _, r, _ in resultados if r})
    tel_comp, mail_comp = contatos.contatos_compartilhados(list(vistos.values()))
    saida: list[dict] = []
    for lead, registro, ident in resultados:
        if registro:
            vinculo_ok = lead.get("cnpj_confianca") in ("alta", "media", "manual")
            lead.update(contatos.avaliar_contato(registro, ident or {}, lead, tel_comp, mail_comp, vinculo_ok))
            lead["contato_fonte"] = registro.get("fonte_contato") or f"cadastro da Receita ({registro.get('fonte_cadastro')})"
        saida.append(lead)

    cont: dict[str, int] = {}
    for l in saida:
        cont[l["status_enriquecimento"]] = cont.get(l["status_enriquecimento"], 0) + 1
    com_tel = sum(1 for l in saida if l.get("telefone"))
    log.info("CNPJ: %s | telefones confirmados: %d | e-mails confirmados: %d",
             cont, com_tel, sum(1 for l in saida if l.get("email")))
    return saida


# ---------------------------------------------------------------
# Relatório: data/cnpjs_encontrados.csv (gerado automaticamente a cada rodada)
# ---------------------------------------------------------------
CSV_ENCONTRADOS = RAIZ / "data" / "cnpjs_encontrados.csv"
_COLUNAS_RELATORIO = (
    "proponente", "titulo_projeto", "status_enriquecimento", "cnpj", "razao_social",
    "cnpj_origem", "cnpj_confianca", "cnpj_evidencia", "telefone", "telefone_status",
    "telefone_obs", "email", "email_status", "email_obs",
)


def exportar_cnpjs_encontrados(leads: list[dict], caminho: Path | str = CSV_ENCONTRADOS) -> Path | None:
    """Grava, para auditoria, o que a busca automática achou para cada lead.
    (Separado de cnpjs_manuais.csv, que é só para CORREÇÕES manuais opcionais.)"""
    try:
        caminho = Path(caminho)
        caminho.parent.mkdir(parents=True, exist_ok=True)
        with open(caminho, "w", newline="", encoding="utf-8-sig") as f:   # utf-8-sig: abre bem no Excel
            w = csv.DictWriter(f, fieldnames=_COLUNAS_RELATORIO, delimiter=";", extrasaction="ignore")
            w.writeheader()
            for l in leads:
                w.writerow({c: ("" if l.get(c) is None else l.get(c)) for c in _COLUNAS_RELATORIO})
        return caminho
    except OSError as e:
        log.warning("Não consegui gravar %s: %s", caminho, e)
        return None


# ---------------------------------------------------------------
# Diagnóstico de uma busca
# ---------------------------------------------------------------
def diagnosticar(proponente: str, titulo: str, uf: str) -> None:
    lead = {"proponente": proponente, "titulo_projeto": titulo}
    cache = Cache(False)
    startup = identidade.nome_da_startup(titulo)
    print(f"Proponente: {proponente}\nStartup (do título): {startup or '(título descritivo, não usado)'}\nUF alvo: {uf}\n")
    with Buscador(uf=uf) as b:
        candidatos: list[str] = []
        for termo, tipo in ((proponente, "pessoa"), (startup, "startup")):
            if not termo:
                continue
            achados, diag = b.buscar(termo, tipo, titulo)
            print(f"Busca '{termo}' ({tipo}):")
            for d in diag:
                print(f"  - {d}")
            candidatos += [c for c in achados if c not in candidatos]
        print(f"\n{len(candidatos)} candidatos distintos. Conferindo os {min(len(candidatos), MAX_VERIFICAR)} primeiros no cadastro oficial:\n")
        avaliados = []
        for c in candidatos[:MAX_VERIFICAR]:
            registro, _ = _registro(c, consulta, cache)
            print(f"DEBUG REGISTRO: CNPJ={c}, registro={'SIM' if registro else 'NAO'}, status={_}")
            if registro:
                ident = identidade.avaliar(lead, registro, uf_alvo)
                print(f"DEBUG IDENTIDADE: CNPJ={c}, razao={registro.get('razao_social')}, confianca={ident['confianca']}, motivos={ident['motivos']}, rejeicao={ident.get('rejeicao')}")
                avaliados.append((c, registro, ident))
            veredito = ident["confianca"] or "REJEITADO"
            print(f"  {c}  {registro.get('razao_social')}  [{veredito}]")
            print(f"      {'; '.join(ident['motivos']) or '-'}  {('| ' + ident['rejeicao']) if ident['rejeicao'] else ''}")
            if ident["confianca"] in ("alta", "media") and c in b.slugs:
                pagina = b.contatos_da_pagina(c)
                print(f"      página da empresa: {pagina if pagina is not None else 'indisponível/bloqueada'}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    ap = argparse.ArgumentParser(description="Etapa 2: diagnóstico da busca de CNPJ")
    ap.add_argument("--diagnostico", metavar="PROPONENTE", help="nome do proponente a procurar")
    ap.add_argument("--titulo", default="", help="título do projeto (para extrair o nome da startup)")
    ap.add_argument("--uf", default="DF")
    a = ap.parse_args()
    if not a.diagnostico:
        ap.print_help()
    else:
        diagnosticar(a.diagnostico, a.titulo, a.uf)