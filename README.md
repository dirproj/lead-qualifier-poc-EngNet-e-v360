# 🎯 Lead Qualifier: Pré-SDR com IA para prospecção outbound

Prova de conceito (PoC) para o hackathon **IA Operacional e Produtividade no Trabalho**, trilha **Tecnologia & TI**.

O sistema lê a lista real de projetos aprovados num edital de inovação (ex.: **Programa Centelha**), **descobre sozinho o CNPJ e o contato** do proponente ou da startup, usa o **Gemini** como **Pré-SDR** para dar uma **nota de fit** a cada projeto e entrega tudo num **mini-CRM em Streamlit**.

> **Custo: R$ 0.** Gemini (camada gratuita da API), minhareceita.org e BrasilAPI (dados abertos), Streamlit e Playwright (open source).

---

## Sumário

1. [Como funciona](#como-funciona)
2. [Resultado de uma rodada real](#resultado-de-uma-rodada-real)
3. [Estrutura do projeto](#estrutura-do-projeto)
4. [Instalação](#instalação)
5. [Configuração](#configuração)
6. [Como rodar](#como-rodar)
7. [Diagnóstico: quando algo falha](#diagnóstico-quando-algo-falha)
8. [Regra de contato](#regra-de-contato)
9. [Guardrails contra alucinação](#guardrails-contra-alucinação)
10. [Privacidade, LGPD e termos de uso](#privacidade-lgpd-e-termos-de-uso)
11. [Limitações conhecidas](#limitações-conhecidas)
12. [Próximos passos](#próximos-passos)
13. [Roteiro da demonstração](#roteiro-da-demonstração)

---

## Como funciona

```mermaid
flowchart LR
    A["Página do edital<br/>(Centelha DF)"] -->|"Playwright:<br/>acha o PDF"| B["1. Extração<br/>nome, projeto, temática"]
    B --> C["2. Busca automática<br/>do CNPJ"]
    C -->|"nome do sócio,<br/>nome da startup"| D["Casa dos Dados<br/>cnpj.biz · buscador"]
    D -->|"candidatos"| E["Conferência no cadastro<br/>oficial (QSA, UF, ativa)"]
    E --> F["Regra de contato<br/>telefone / e-mail"]
    F --> G["3. Pré-SDR<br/>Gemini + guardrails"]
    G --> H[("leads_processados.json")]
    H --> I["4. Streamlit<br/>mini-CRM"]
```

| # | Etapa | O que faz |
|---|-------|-----------|
| 1 | **Extração** | Abre a página real do edital, encontra o PDF de resultado (inclusive em links do Google Drive) e lê a tabela. |
| 2 | **CNPJ + contato (automático)** | Procura o CNPJ pelo **nome do proponente** (como sócio ou MEI) e pelo **nome da startup** tirado do título do projeto. **Cada candidato é conferido no cadastro oficial**; só é atribuído se o proponente for sócio/titular (ou o nome da startup bater), a empresa estiver ativa e for da UF do edital. Com o CNPJ vinculado, aplica a [regra de contato](#regra-de-contato). |
| 3 | **Pré-SDR (IA)** | O Gemini devolve JSON estruturado com nota (0 a 10), confiança, justificativa, serviços sugeridos e evidências literais, protegido por guardrails. |
| 4 | **Painel** | Tabela interativa, filtros, detalhe do lead (com a origem do CNPJ e o status de cada contato) e funil comercial. |

**Por que conferir cada candidato?** A busca por nome pode devolver homônimos. Os sites só *geram candidatos*; quem decide é o cadastro oficial (quadro societário, razão social de MEI, UF e situação). Se houver dois candidatos igualmente prováveis, o lead fica **ambíguo** e nenhum CNPJ é atribuído.

---

## Resultado de uma rodada real

Rodada de 23/10/2026 sobre a página do Centelha DF, que publicou a lista de projetos contratados em 21/09/2026.

| Etapa | Resultado |
|-------|-----------|
| Extração | **46 projetos** lidos do PDF de contratados |
| CNPJ vinculado | **23 (50%)**: 20 com confiança alta, 3 com confiança média; 2 deles com `cnpj_alerta` de vínculo fraco |
| CNPJ ambíguo | 9: dois ou mais candidatos igualmente prováveis, nenhum atribuído |
| Só o nome da startup bate | 4: ficam como candidatos `nao_verificado` |
| Sem CNPJ | 10: provavelmente pessoa física que ainda não abriu a empresa |
| Telefone | 22 dos 23 cadastros vinculados têm telefone completo (18 com o nono dígito acrescentado); todos ficam **não confirmados**, porque o cadastro público não traz e-mail para corroborar ([ver regra](#regra-de-contato)) |
| E-mail | 0: a minhareceita e a BrasilAPI devolvem `email: null` |
| Tempo | **mais de 1 hora** na primeira rodada, quase toda na busca de CNPJ (pausas de 2,5 s por site e espera do navegador); **27 s** nas rodadas seguintes, com cache |

Para comparar com a prospecção manual: se uma pessoa levar 30 minutos por lead para achar o CNPJ, conferir o quadro societário e o contato (estimativa a validar com o SDR da EJ), os 46 projetos tomariam cerca de **23 horas** de trabalho.

---

## Estrutura do projeto

```
lead-qualifier-poc/
├── app.py                       # Etapa 4: painel Streamlit
├── pipeline.py                  # Orquestra as etapas 1, 2 e 3
├── requirements.txt
├── .env.example                 # copie para .env e coloque a chave do Gemini
├── planilha.py                  # protege os CSVs exportados contra fórmulas no Excel
├── diagnostico.py               # confere pacotes, .env e versões dos módulos
│
├── extracao/                    # Etapa 1
│   ├── scraper.py               # Playwright + pdfplumber, links do Google Drive
│   └── validacao.py             # validação de CNPJ (inclui o formato alfanumérico)
│
├── enriquecimento/              # Etapa 2
│   ├── descoberta.py            # nome -> CNPJs candidatos (Casa dos Dados, cnpj.biz, web)
│   ├── brasilapi.py             # CNPJ -> cadastro oficial (minhareceita.org + BrasilAPI)
│   ├── identidade.py            # este CNPJ é mesmo da pessoa/startup?
│   ├── contatos.py              # regra de telefone e e-mail
│   └── orquestrador.py          # junta tudo; gera data/cnpjs_encontrados.csv
│
├── classificacao/               # Etapa 3
│   ├── perfil.py                # ✏️ o que a SUA empresa vende (editar)
│   ├── llm.py                   # Gemini (SDK google-genai), cache, diagnóstico
│   ├── guardrails.py            # schema estrito + validações anti-alucinação
│   └── regras.py                # nota por palavras-chave (reserva)
│
├── tests/test_poc.py            # testes offline, sem rede nem chave
│
└── data/
    ├── brutos/                  # PDFs baixados
    ├── cnpjs_encontrados.csv    # AUTOMÁTICO: o que a busca achou, com evidência
    ├── cnpjs_manuais.csv        # OPCIONAL: correções da equipe (proponente,cnpj)
    ├── nao_contatar.csv         # OPCIONAL: opt-out (proponente,cnpj), ignorado pelo git
    ├── leads_processados.json   # saída lida pelo painel
    └── cache_*.json, crm_estado.json   # caches e funil (ignorados pelo git)
```

> `cnpjs_manuais.csv` pode ficar vazio: a busca é automática. Ele serve só para **corrigir** um lead que a busca errou ou não achou, e tem prioridade sobre a busca.

---

## Instalação

**Requisitos:** Python **3.10+**.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1              # Windows (PowerShell)
# source .venv/bin/activate             # Linux / macOS

pip install -r requirements.txt
python -m playwright install chromium   # uma vez só
```

---

## Configuração

### Chave do Gemini

1. Crie uma chave em <https://aistudio.google.com/apikey>.
2. Copie `.env.example` para `.env` e preencha:

```env
GEMINI_API_KEY=...
GEMINI_MODEL=              # opcional; padrão: gemini-3.8-flash
LLM_PAUSA=6                # segundos entre chamadas (a camada gratuita tem limite por minuto)
CASADOSDADOS_API_KEY=      # opcional; chave da API oficial da Casa dos Dados
NAVEGADOR_VISIVEL=0        # 1 abre o Chromium com janela
```

Confira se o modelo responde com `python -m classificacao.llm --testar`.

O `.env` está no `.gitignore`. **Nunca** suba a chave para o repositório.

### Perfil da empresa

Edite `classificacao/perfil.py` com o que **vocês** vendem. A nota de fit depende disso.

---

## Como rodar

```powershell
# Teste barato: só 3 chamadas ao LLM
python pipeline.py --pagina https://programacentelha.com.br/df/ --max-ia 3

# Rodada completa (extração + CNPJ + contatos + IA)
python pipeline.py --pagina https://programacentelha.com.br/df/

# Painel
streamlit run app.py        # ou: python -m streamlit run app.py   -> http://localhost:8501

# Testes (offline, sem chave)
python -m unittest discover -s tests -v

# Apagar leads, caches, CRM e PDFs baixados
python pipeline.py --apagar-dados
```

Opções do pipeline: `--sem-ia`, `--sem-busca-cnpj` (usa só o CSV manual), `--sem-cache-cnpj`, `--uf DF`, `--max-ia N`.

Depois de cada rodada, abra `data/cnpjs_encontrados.csv` para **auditar** o que foi achado e com que evidência.

---

## Diagnóstico: quando algo falha

| Sintoma | Comando | O que mostra |
|---------|---------|--------------|
| Nota não aparece / IA falhando | `python -m classificacao.llm --testar` | Faz 1 chamada real e mostra o erro exato (chave, modelo, limite) |
| Pacotes, `.env` ou módulos desencontrados | `python diagnostico.py` | O que falta instalar ou configurar |
| CNPJ não é encontrado | `python -m enriquecimento.orquestrador --diagnostico "Nome do Proponente" --titulo "Título do projeto"` | Resultado de cada fonte (HTTP, bloqueio, nº de candidatos) e o veredito da conferência de cada candidato |
| Colunas do PDF erradas | `python -m extracao.scraper --pagina URL --inspecionar` | Linhas do PDF com índices, para ajustar `--colunas` |

No painel, o campo **Status da IA** e os avisos do detalhe do lead dizem por que uma nota está ausente (`sem_chave`, `falha`, `nao_processado`).

---

## Regra de contato

Um telefone ou e-mail **só entra na coluna principal** quando **todas** as condições valem:

1. está **completo** (sem dígitos ocultos; DDD válido; 8 ou 9 dígitos). Celular de 8 dígitos começando com 6, 7, 8 ou 9, formato antigo que o cadastro da Receita ainda guarda, ganha o 9 na frente;
2. **não** é de contabilidade, coworking, recepção ou condomínio;
3. **não** se repete em CNPJs diferentes (sinal de contador/escritório);
4. o CNPJ foi **vinculado** ao proponente/startup;
5. há **corroboração**: e-mail no domínio ou com o nome da própria startup/pessoa, **ou** o CNPJ é de MEI/empresário individual no nome do proponente.

Em qualquer outro caso o número **não vai para a coluna principal**, e o motivo aparece de forma explícita: `dígitos ocultos`, `incompleto`, `contabilidade`, `coworking/recepção`, `compartilhado` ou `não confirmado`. O número do cadastro, quando existe, aparece só no detalhe do lead, rotulado como **não confirmado**.

Mesmo um contato "confirmado" vem do cadastro do CNPJ e pode ter sido preenchido pelo contador; por isso o painel mostra a **confiança** (alta/média) e a observação.

**O cadastro público não traz e-mail.** A minhareceita e a BrasilAPI devolvem `email: null` (confirmado até no CNPJ do Banco do Brasil). Sem e-mail, a corroboração da condição 5 só acontece quando o CNPJ é de MEI no nome do proponente. Por isso, na prática, quase todo telefone fica como **não confirmado** e aparece só no detalhe do lead. A decisão foi manter a regra conservadora no MVP: o SDR vê o número com o rótulo e confirma na primeira ligação.

---

## Guardrails contra alucinação

| Camada | O que garante |
|--------|---------------|
| **Dados enviados ao LLM** | Só título, temática e município (mais CNAE/porte). O prompt trata o texto do projeto como **dado**, nunca como instrução. |
| **Schema JSON estrito** | O Gemini recebe o schema em `response_schema` e responde em JSON, só com os campos e os serviços do catálogo. |
| **Validação em Python** | Nota inteira de 0 a 10, serviços do catálogo; resposta inválida é rejeitada e o modelo corrige (até 3 tentativas). |
| **Ancoragem** | Cada evidência precisa ser trecho **literal** dos dados; senão é descartada e o lead vai para `revisar`. |
| **Regras determinísticas** | Dados insuficientes limitam a nota a 4; nota alta sem evidência cai para 6; termos como *faturamento* ou *investidores* sem base geram aviso. |
| **Nota por regras (reserva)** | Sem chave, com falha da IA ou fora do limite do `--max-ia`, o lead recebe uma nota conservadora por palavras-chave (no máximo 6, ou 7 com CNAE de tecnologia), marcada como `regras (sem IA)` no painel. |
| **Conferência de CNPJ** | Dígito verificador validado e candidato conferido no cadastro (sócio/MEI, UF, situação ativa). Ambíguo ou fraco **não é atribuído**. |

Falhas de rede, bloqueios e limites nunca derrubam o pipeline: viram status por lead. Respostas do LLM, buscas e cadastros ficam em **cache**, o que poupa a cota gratuita e deixa a demo estável.

---

## Privacidade, LGPD e termos de uso

- O **nome do proponente não é enviado** ao LLM. No painel, ele fica **oculto por padrão**.
- Os dados usados (lista de aprovados e cadastro de CNPJ) são **públicos**, mas telefone e e-mail de MEI/sócio são dados pessoais ligados a uma pessoa física. Para contato comercial, trabalhe com **interesse legítimo**, identifique-se, ofereça opt-out e não use os dados para outra finalidade.
- **Termos de uso dos sites.** A busca por nome usa o endpoint público de pesquisa da Casa dos Dados, páginas do cnpj.biz e um buscador web. **Esses sites podem restringir acesso automatizado.** O código reduz o risco: respeita o `robots.txt`, espera ~2,5 s entre requisições, usa cache, para no primeiro site que responde e, se houver bloqueio ou captcha, **registra e segue em frente, sem tentar contornar**. Confira os termos de cada site antes de uso além do PoC. A Casa dos Dados também oferece uma API oficial com chave (`https://docs.casadosdados.com.br`) e os dados-fonte são os arquivos abertos da Receita Federal.
- **Compartilhamento com terceiros.** Para achar o CNPJ, o nome completo do proponente é enviado à Casa dos Dados, ao cnpj.biz e ao pacote `ddgs`, que repassa a busca a vários buscadores (numa rodada real do Centelha DF: Startpage, Mojeek, Google, Wikipédia, DuckDuckGo, Grokipedia, Brave e Yahoo). Para o Gemini vão só dados do projeto e da empresa.
- **Mínimo de dados guardados.** O cache guarda telefone e e-mail só dos CNPJs vinculados a um lead. Dos demais candidatos (homônimos, empresas rejeitadas) fica só uma impressão SHA-256 do contato, usada para detectar número de contador repetido. É pseudonimização, não anonimização.
- **Opt-out.** Quem pedir para não ser contatado entra em `data/nao_contatar.csv` (colunas `proponente,cnpj`) e sai da coleta antes da busca. `python pipeline.py --apagar-dados` apaga leads, caches, CRM e PDFs.
- **Vínculo fraco.** Quando o CNPJ foi ligado ao lead por um bate parcial de só 2 nomes, ou só pelo nome da startup e pela data de abertura, o painel e os CSVs mostram `cnpj_alerta` para o SDR confirmar antes de contatar.
- **Painel só local.** O `.streamlit/config.toml` prende o painel em `localhost` e desliga a telemetria do Streamlit. Não publique o painel sem senha: ele mostra telefones e e-mails e dispara coletas.
- Chaves ficam só no `.env`. Os CSVs exportados neutralizam células que começariam com `=`, `+`, `-` ou `@`.

---

## Limitações conhecidas

- **A busca de CNPJ depende de sites externos.** O endpoint público da Casa dos Dados e as páginas do cnpj.biz **não têm contrato de estabilidade** e podem mudar ou bloquear. Na rodada real de 23/10/2026, a página de empresa da Casa dos Dados devolveu 403, e muitas buscas passaram pelo buscador web. Use `--diagnostico` na primeira rodada. Se todas as fontes falharem, o lead fica `sem_cnpj` e o CSV manual cobre os casos importantes.
- **Nem todo proponente tem CNPJ ainda.** Muitos projetos são de pessoa física que abre a empresa só após a contratação; nesses casos `sem_cnpj` é o resultado correto.
- **Homônimos.** A conferência reduz o risco, mas não o elimina; revise os leads `confiança média` antes de abordar.
- **Contato do cadastro é incerto por natureza** (pode ser do contador). A regra acima é conservadora de propósito: poucos números na planilha, mas com motivo claro.
- **A IA decide com pouco texto** (título e temática): a nota é uma priorização, não um veredito.
- **Camada gratuita do Gemini** tem limite de requisições por minuto/dia; o código espera e usa cache, mas uma rodada de muitos leads pode precisar ser feita em partes.
- Os testes automatizados (`tests/test_poc.py`) rodam offline, com cliente de IA e cadastros simulados. Eles **não** testam os sites reais nem o Gemini.
- **Telefone quase sempre não confirmado**, porque o cadastro público não tem e-mail (ver [Regra de contato](#regra-de-contato)).
- **Bate parcial de nomes.** A conferência aceita um sócio que contém só 2 nomes do proponente ("Maria Silva" casa com "Maria Silva Santos Oliveira"). Esses casos são marcados com `cnpj_alerta`, mas a regra não foi endurecida para não mudar os resultados perto da entrega.

---

## Próximos passos

- **Desempatar os ambíguos (20% na rodada real).** Duas causas apareceram: o nome da startup é uma palavra comum ("Primeiro", "Green", "Farol") e casa com empresas sem relação; ou o proponente é sócio de duas empresas. O desempate pode usar CNAE de tecnologia, abertura a partir de 2025 e a semelhança do nome com o título. Fica para depois do MVP porque muda os CNPJs encontrados.
- **Endurecer o bate parcial** para exigir 3 nomes em comum, hoje só sinalizado com `cnpj_alerta`.
- **E-mail de outra fonte**, como a API oficial da Casa dos Dados (`CASADOSDADOS_API_KEY`), para voltar a ter corroboração de telefone.
- **Restringir o `ddgs` a menos buscadores**, para reduzir o envio do nome do proponente a terceiros.

---

## Roteiro da demonstração

1. **Terminal:** rode o pipeline (PDF real, busca de CNPJ, contatos, IA) e mostre `data/cnpjs_encontrados.csv`.
2. **Painel:** filtre por **alto fit** e marque **Só com CNPJ vinculado**.
3. **Detalhe:** mostre a justificativa com trechos literais e **como o CNPJ foi encontrado** (evidência e confiança).
4. **Contato claro:** abra um lead com contato **bloqueado** (contabilidade, compartilhado ou dígitos ocultos) e mostre o motivo.
5. **Guardrail:** abra um lead em `revisar` ou `ambíguo`.
6. **CRM:** mova um lead para *Contatado*, anote e exporte o CSV.

---

**Stack:** Python 3.10+ · Playwright · pdfplumber · requests · Gemini (SDK `google-genai`) · minhareceita.org / BrasilAPI · Streamlit · pandas

**Equipe:** _(preencha os nomes)_