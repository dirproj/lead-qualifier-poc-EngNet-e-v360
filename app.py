"""
ETAPA 4: PAINEL (mini-CRM em Streamlit)
=====================================================================
Mostra os leads extraídos, enriquecidos e classificados pela IA numa
tabela interativa, com filtros, detalhe de cada lead e um funil
comercial simples (etapa + anotações do SDR).

COMO RODAR (a partir da RAIZ do projeto):
    pip install -r requirements.txt
    python pipeline.py --pagina "https://programacentelha.com.br/df/"   # gera os dados
    streamlit run app.py             # no Windows, se não achar o comando:
    python -m streamlit run app.py
    # abre em http://localhost:8501 (Ctrl+C no terminal encerra o servidor)

O painel só LÊ data/leads_processados.json. Para atualizar os dados, rode
o pipeline de novo no terminal e clique em "Recarregar dados" na barra lateral.
O estado comercial (etapa e anotações) fica em data/crm_estado.json.
=====================================================================
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from classificacao.perfil import CATALOGO_SERVICOS

RAIZ = Path(__file__).resolve().parent
ARQ_LEADS = RAIZ / "data" / "leads_processados.json"
ARQ_CRM = RAIZ / "data" / "crm_estado.json"

ETAPAS_CRM = ["Novo", "Contatado", "Em conversa", "Reunião marcada", "Descartado"]
FAIXAS = ["alto", "medio", "baixo", "sem nota"]
ICONE_FAIXA = {"alto": "🟢", "medio": "🟡", "baixo": "🔴", "sem nota": "⚪"}
ROTULO_STATUS_IA = {
    "ok": " ok", "revisar": " revisar", "falha": " falha",
    "sem_chave": " sem chave", "nao_processado": "⏸ não processado",
}
ROTULO_ENRIQ = {
    "ok": " CNPJ vinculado", "sem_cnpj": "— sem CNPJ", "cnpj_invalido": " CNPJ inválido",
    "nao_encontrado_ou_api_fora": " cadastro indisponível", "ambiguo": " ambíguo",
    "nao_verificado": " não verificado",
}
ROTULO_CONTATO = {
    "confirmado": " confirmado", "nao_confirmado": " não confirmado",
    "compartilhado": " compartilhado", "contabilidade": " contabilidade",
    "coworking": " coworking/recepção", "mascarado": " dígitos ocultos",
    "incompleto": " incompleto", "invalido": " inválido", "sem_dado": "— sem dado",
}

st.set_page_config(page_title="Pré-SDR | Mini-CRM", page_icon=" ", layout="wide")


# ---------------------------------------------------------------
# Dados
# ---------------------------------------------------------------
def lead_id(lead: dict) -> str:
    base = f"{lead.get('titulo_projeto', '')}|{lead.get('proponente', '')}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]


@st.cache_data(show_spinner=False)
def _ler_leads(caminho: str, mtime: float) -> list[dict]:
    """`mtime` só serve para invalidar o cache quando o arquivo muda."""
    return json.loads(Path(caminho).read_text(encoding="utf-8"))


def carregar_leads() -> list[dict]:
    if not ARQ_LEADS.exists():
        st.error(
            "Ainda não há dados. Rode no terminal:\n\n"
            '`python pipeline.py --pagina "https://programacentelha.com.br/df/"`'
        )
        st.stop()
    try:
        return _ler_leads(str(ARQ_LEADS), ARQ_LEADS.stat().st_mtime)
    except (json.JSONDecodeError, OSError) as e:
        st.error(f"Não consegui ler {ARQ_LEADS.name}: {e}. Rode o pipeline novamente.")
        st.stop()


def carregar_crm() -> dict:
    try:
        return json.loads(ARQ_CRM.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def salvar_crm(estado: dict) -> bool:
    try:
        ARQ_CRM.parent.mkdir(parents=True, exist_ok=True)
        ARQ_CRM.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except OSError as e:
        st.error(f"Não consegui salvar o estado comercial: {e}")
        return False


def faixa_de(lead: dict) -> str:
    return lead.get("faixa_fit") or "sem nota"


def formatar_cnpj(cnpj: str | None) -> str:
    c = cnpj or ""
    return f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}" if len(c) == 14 else (c or "—")


def nomes_servicos(chaves: list | None) -> str:
    return ", ".join(CATALOGO_SERVICOS.get(c, c) for c in (chaves or []))


# ---------------------------------------------------------------
# Filtros (barra lateral)
# ---------------------------------------------------------------
def barra_lateral(leads: list[dict]) -> dict:
    sb = st.sidebar
    sb.header("Filtros")

    filtros = {
        "faixas": sb.multiselect("Faixa de fit", FAIXAS, default=FAIXAS),
        "nota_min": sb.slider("Nota mínima", 0, 10, 0),
        "confianca": sb.multiselect(
            "Confiança da IA", ["alta", "media", "baixa"], default=["alta", "media", "baixa"],
            help="Leads sem nota (sem IA) só aparecem se a nota mínima for 0.",
        ),
        "status_ia": sb.multiselect(
            "Status da IA", list(ROTULO_STATUS_IA),
            format_func=ROTULO_STATUS_IA.get, default=list(ROTULO_STATUS_IA),
            help="'revisar' = a IA respondeu, mas um guardrail apontou algo para conferir.",
        ),
        "tematicas": sb.multiselect(
            "Temática", sorted({l.get("tematica") for l in leads if l.get("tematica")}),
        ),
        "so_cnpj": sb.checkbox("Só com CNPJ vinculado"),
        "so_telefone": sb.checkbox("Só com telefone confirmado"),
        "busca": sb.text_input("Buscar no projeto", placeholder="ex.: saúde, IoT, agro"),
    }

    sb.divider()
    filtros["mostrar_proponente"] = sb.checkbox(
        "Mostrar nome do proponente", value=False,
        help="Pessoas físicas. Deixe desligado em apresentações.",
    )
    if sb.button(" Recarregar dados"):
        st.cache_data.clear()
        st.rerun()

    modelo = next((l.get("modelo_ia") for l in leads if l.get("modelo_ia")), None)
    gerado = datetime.fromtimestamp(ARQ_LEADS.stat().st_mtime).strftime("%d/%m/%Y %H:%M")
    sb.caption(f"Dados gerados em {gerado}" + (f" · modelo: {modelo}" if modelo else ""))
    return filtros


def aplicar_filtros(leads: list[dict], f: dict) -> list[dict]:
    busca = f["busca"].strip().lower()
    saida = []
    for l in leads:
        nota = l.get("nota_fit")
        if faixa_de(l) not in f["faixas"]:
            continue
        if f["nota_min"] > 0 and (nota is None or nota < f["nota_min"]):
            continue
        if nota is not None and l.get("confianca") not in f["confianca"]:
            continue
        if (l.get("status_ia") or "nao_processado") not in f["status_ia"]:
            continue
        if f["tematicas"] and l.get("tematica") not in f["tematicas"]:
            continue
        if f["so_cnpj"] and l.get("status_enriquecimento") != "ok":
            continue
        if f["so_telefone"] and not l.get("telefone"):
            continue
        if busca:
            texto = " ".join(str(l.get(c, "")) for c in (
                "titulo_projeto", "tematica", "municipio", "justificativa", "razao_social")).lower()
            if busca not in texto:
                continue
        saida.append(l)
    return sorted(saida, key=lambda l: (l.get("nota_fit") is None, -(l.get("nota_fit") or 0)))


# ---------------------------------------------------------------
# Visão geral
# ---------------------------------------------------------------
def mostrar_kpis(todos: list[dict], filtrados: list[dict]) -> None:
    notas = [l["nota_fit"] for l in todos if l.get("nota_fit") is not None]
    c = st.columns(6)
    c[0].metric("Leads extraídos", len(todos))
    c[1].metric("Exibindo", len(filtrados))
    c[2].metric("Alto fit (7+)", sum(1 for l in todos if faixa_de(l) == "alto"))
    c[3].metric("Para revisar", sum(1 for l in todos if l.get("status_ia") == "revisar"),
                help="Respostas da IA que um guardrail pediu para conferir.")
    c[4].metric("Com CNPJ", sum(1 for l in todos if l.get("status_enriquecimento") == "ok"),
                help="CNPJ encontrado automaticamente (ou informado) e conferido no cadastro oficial.")
    c[5].metric("Telefone confirmado", sum(1 for l in todos if l.get("telefone")),
                help="Só conta número completo e claramente da pessoa/startup (não contador, coworking etc.).")
    if notas:
        st.caption(f"Nota média de fit: {sum(notas) / len(notas):.1f} · {len(notas)} leads classificados")


def montar_tabela(leads: list[dict], crm: dict, mostrar_proponente: bool) -> pd.DataFrame:
    linhas = []
    for l in leads:
        faixa = faixa_de(l)
        linha = {
            "Fit": f"{ICONE_FAIXA[faixa]} {faixa}",
            "Nota": l.get("nota_fit"),
            "Confiança": l.get("confianca") or "—",
            "Projeto": l.get("titulo_projeto"),
        }
        if mostrar_proponente:
            linha["Proponente"] = l.get("proponente")
        linha.update({
            "Temática": l.get("tematica"),
            "Município": l.get("municipio"),
            "Serviços sugeridos": nomes_servicos(l.get("servicos_sugeridos")),
            "IA": ROTULO_STATUS_IA.get(l.get("status_ia"), "—"),
            "CNPJ": ROTULO_ENRIQ.get(l.get("status_enriquecimento"), "—"),
            "CNPJ nº": formatar_cnpj(l.get("cnpj")) if l.get("cnpj") else "—",
            "Telefone": l.get("telefone") or "—",
            "E-mail": l.get("email") or "—",
            "Contato": ROTULO_CONTATO.get(l.get("telefone_status"), "—"),
            "Etapa": crm.get(lead_id(l), {}).get("etapa", "Novo"),
        })
        linhas.append(linha)
    return pd.DataFrame(linhas)


def exportar_csv(leads: list[dict], crm: dict, mostrar_proponente: bool) -> bytes:
    linhas = []
    for l in leads:
        linha = {
            "nota_fit": l.get("nota_fit"), "faixa": faixa_de(l), "confianca": l.get("confianca"),
            "projeto": l.get("titulo_projeto"),
        }
        if mostrar_proponente:
            linha["proponente"] = l.get("proponente")
        linha.update({
            "tematica": l.get("tematica"), "municipio": l.get("municipio"),
            "servicos_sugeridos": nomes_servicos(l.get("servicos_sugeridos")),
            "justificativa": l.get("justificativa"), "status_ia": l.get("status_ia"),
            "cnpj": l.get("cnpj"), "razao_social": l.get("razao_social"),
            "cnpj_origem": l.get("cnpj_origem"), "cnpj_confianca": l.get("cnpj_confianca"),
            "cnae": l.get("cnae_descricao"), "porte": l.get("porte"),
            "telefone": l.get("telefone"), "telefone_status": l.get("telefone_status"),
            "telefone_obs": l.get("telefone_obs"),
            "email": l.get("email"), "email_status": l.get("email_status"),
            "etapa": crm.get(lead_id(l), {}).get("etapa", "Novo"),
            "anotacoes": crm.get(lead_id(l), {}).get("notas", ""),
        })
        linhas.append(linha)
    # utf-8-sig + ';' para abrir direto no Excel em português
    return pd.DataFrame(linhas).to_csv(index=False, sep=";").encode("utf-8-sig")


# ---------------------------------------------------------------
# Detalhe do lead + funil comercial
# ---------------------------------------------------------------
def painel_detalhe(lead: dict, crm: dict, mostrar_proponente: bool) -> None:
    lid = lead_id(lead)
    faixa = faixa_de(lead)
    nota = lead.get("nota_fit")

    st.subheader(lead.get("titulo_projeto") or "(sem título)")
    st.caption(
        f"{lead.get('tematica', '—')} · {lead.get('municipio', '—')}"
        + (f" · Proponente: {lead.get('proponente')}" if mostrar_proponente else "")
    )

    esq, dir_ = st.columns([3, 2])
    with esq:
        m1, m2, m3 = st.columns(3)
        m1.metric("Nota de fit", "—" if nota is None else f"{nota}/10")
        m2.metric("Faixa", f"{ICONE_FAIXA[faixa]} {faixa}")
        m3.metric("Confiança", lead.get("confianca") or "—")

        if lead.get("justificativa"):
            st.markdown("**Por que essa nota**")
            st.write(lead["justificativa"])
        elif lead.get("status_ia") in ("falha", "sem_chave", "nao_processado"):
            st.info(f"Sem avaliação da IA: {'; '.join(lead.get('avisos_ia') or ['não processado'])}")

        if lead.get("evidencias"):
            st.markdown("**Trechos dos dados que sustentam a nota**")
            for e in lead["evidencias"]:
                st.markdown(f"> “{e}”")
        if lead.get("servicos_sugeridos"):
            st.markdown("**Serviços sugeridos:** " + nomes_servicos(lead["servicos_sugeridos"]))
        for aviso in lead.get("avisos_ia") or []:
            if lead.get("status_ia") in ("ok", "revisar"):
                st.warning(f"Guardrail: {aviso}")

    with dir_:
        st.markdown("**Empresa (cadastro da Receita)**")
        if lead.get("status_enriquecimento") == "ok":
            st.write(f"**Razão social:** {lead.get('razao_social') or '—'}")
            st.write(f"**CNPJ:** {formatar_cnpj(lead.get('cnpj'))}")
            st.write(f"**CNAE:** {lead.get('cnae_descricao') or '—'}")
            st.write(f"**Porte:** {lead.get('porte') or '—'} · **Abertura:** {lead.get('data_abertura') or '—'}")
            origem = {"automatico": "busca automática", "manual": "informado pela equipe"}.get(lead.get("cnpj_origem"), "—")
            st.caption(f"Como foi encontrado: {origem} · confiança {lead.get('cnpj_confianca') or '—'}. "
                       f"{lead.get('cnpj_evidencia') or ''}")
        else:
            st.info(f"CNPJ não vinculado ({ROTULO_ENRIQ.get(lead.get('status_enriquecimento'), '—')}). "
                    f"{lead.get('cnpj_evidencia') or ''}")
            for cand in lead.get("cnpj_candidatos") or []:
                st.caption(f"Candidato (NÃO confirmado): {formatar_cnpj(cand.get('cnpj'))} · "
                           f"{cand.get('razao_social')} · {cand.get('motivo')}")

        st.markdown("**Contato**")
        if lead.get("status_enriquecimento") == "ok":
            if lead.get("telefone"):
                st.success(f"📞 {lead['telefone']} ({lead.get('telefone_tipo')}) · confiança {lead.get('telefone_confianca')}")
            else:
                st.warning(f"Telefone não usado: {ROTULO_CONTATO.get(lead.get('telefone_status'), '—')}")
            st.caption(lead.get("telefone_obs") or "")
            if lead.get("telefone_candidato"):
                st.caption(f"Número no cadastro, NÃO confirmado: {lead['telefone_candidato']}")
            if lead.get("email"):
                st.success(f"✉️ {lead['email']} · confiança {lead.get('email_confianca')}")
            else:
                st.warning(f"E-mail não usado: {ROTULO_CONTATO.get(lead.get('email_status'), '—')}")
            st.caption(lead.get("email_obs") or "")
            if lead.get("email_candidato"):
                st.caption(f"E-mail no cadastro, NÃO confirmado: {lead['email_candidato']}")
        else:
            st.caption("Sem CNPJ vinculado, não há contato a confirmar.")

        st.markdown("**Funil comercial**")
        atual = crm.get(lid, {})
        with st.form(f"form_{lid}"):
            etapa = st.selectbox(
                "Etapa", ETAPAS_CRM,
                index=ETAPAS_CRM.index(atual["etapa"]) if atual.get("etapa") in ETAPAS_CRM else 0,
            )
            notas = st.text_area("Anotações do SDR", value=atual.get("notas", ""), height=110)
            if st.form_submit_button("Salvar"):
                crm[lid] = {"etapa": etapa, "notas": notas, "atualizado": datetime.now().isoformat(timespec="seconds")}
                if salvar_crm(crm):
                    st.session_state["_salvo"] = True
                    st.rerun()


# ---------------------------------------------------------------
# Página
# ---------------------------------------------------------------
def main() -> None:
    st.title("🎯 Pré-SDR · Mini-CRM de Leads")
    st.caption("Projetos aprovados em editais de inovação, qualificados por IA com travas anti-alucinação.")
    if st.session_state.pop("_salvo", False):
        st.toast("Estado comercial salvo")

    todos = carregar_leads()
    filtros = barra_lateral(todos)
    filtrados = aplicar_filtros(todos, filtros)
    crm = carregar_crm()

    mostrar_kpis(todos, filtrados)

    contagem = pd.Series([faixa_de(l) for l in filtrados]).value_counts().reindex(FAIXAS, fill_value=0)
    with st.expander("Distribuição por faixa de fit", expanded=False):
        st.bar_chart(contagem)

    if not filtrados:
        st.info("Nenhum lead com esses filtros.")
        return

    df = montar_tabela(filtrados, crm, filtros["mostrar_proponente"])
    st.markdown("##### Leads (clique em uma linha para abrir o detalhe)")
    evento = st.dataframe(
        df, hide_index=True, key="tabela_leads",
        on_select="rerun", selection_mode="single-row",
        column_config={
            "Nota": st.column_config.ProgressColumn("Nota", min_value=0, max_value=10, format="%d"),
            "Projeto": st.column_config.TextColumn(width="large"),
            "Serviços sugeridos": st.column_config.TextColumn(width="medium"),
        },
    )
    st.download_button(
        "⬇ Baixar leads filtrados (CSV)",
        data=exportar_csv(filtrados, crm, filtros["mostrar_proponente"]),
        file_name="leads_filtrados.csv", mime="text/csv",
    )

    linhas = evento.selection.rows
    st.divider()
    if linhas and linhas[0] < len(filtrados):
        painel_detalhe(filtrados[linhas[0]], crm, filtros["mostrar_proponente"])
    else:
        st.caption("Selecione um lead na tabela para ver a justificativa da IA, os dados cadastrais e registrar o contato.")


main()