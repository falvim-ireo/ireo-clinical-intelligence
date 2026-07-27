# IREO Clinical Intelligence

> **Fonte única de verdade do projeto.**
>
> Última atualização: 2026-07-27
>
> Estado documental: v1, criado por auditoria do repositório, do histórico Git e dos testes.
> Referência de código: `main` em `b4d014a62aa2cd4838f23842e83ddfeb9ab7d161`.

## Protocolo obrigatório para novas sessões

Copiar e enviar no início de toda nova conversa:

```text
Leia integralmente:

docs/ENGINEERING_MASTER.md

Ele é a única fonte de verdade.

Não reconstrua arquitetura.

Não faça inferências.

Atualize esse documento ao final da tarefa.
```

Regras de governança:

1. Toda Sprint aprovada atualiza este arquivo na mesma mudança de código.
2. Uma Sprint não está concluída enquanto este documento não registrar objetivo, resultado, arquivos, testes, riscos, decisões e próxima tarefa.
3. Fatos não confirmados devem ser escritos como `DESCONHECIDO`, nunca preenchidos por inferência.
4. Bugs resolvidos nunca são apagados: movem-se de “Bugs abertos” para “Bugs resolvidos”.
5. Deve existir exatamente uma “Próxima tarefa única”.
6. Este documento prevalece sobre `README.md`, `ROADMAP.md`, `CHANGELOG.md` e documentos em `docs/architecture/` quando houver divergência. A divergência deve ser corrigida, não perpetuada.
7. Credenciais, tokens, nomes de pacientes, IDs clínicos puros, URLs assinadas e caminhos clínicos reais nunca entram neste documento.
8. Alterações locais não commitadas são identificadas como tal e não são apresentadas como Sprint aprovada.

## 1. Objetivo do Projeto

### 1.1 Objetivo

O IREO Clinical Intelligence é uma plataforma operacional para adquirir, identificar, normalizar, publicar, indexar e consultar ativos clínicos/radiológicos do IREO com rastreabilidade, idempotência, proteção de dados e revisão humana nas situações ambíguas.

O sistema integra atualmente:

- Clinicorp como fonte mestre de identidade do paciente;
- Gmail em modo somente leitura para descoberta de notificações;
- TransferNow e Cfaz como provedores de aquisição;
- quarentena e staging local para validação;
- leitura estrutural DICOM sem interpretação clínica;
- OneDrive, por pasta local sincronizada e por Microsoft Graph;
- SQLite como histórico operacional e índice derivado;
- CLI como interface operacional e de consulta.

“Future AI” é uma direção futura, não um componente ativo. Nenhum dado clínico é enviado atualmente a modelos de IA.

### 1.2 Escopo atual

Incluído:

- descoberta e aquisição multiprovedor;
- validação de links, downloads e arquivos compactados;
- proteção contra path traversal, sobrescrita e associação ambígua;
- resolução de paciente contra Clinicorp;
- inventário estrutural DICOM, sem carregar pixels;
- normalização de radiografias, fotografias, tomografias, relatórios e modelos digitais;
- criação de `ClinicalPackage`, manifesto e resumos;
- publicação idempotente no OneDrive;
- histórico sanitizado e prevenção de reimportação;
- indexação SQLite reconstruível;
- consultas clínicas estruturais e dashboard operacional;
- automação local fail-closed e fila de revisão.

Fora do escopo atual:

- diagnóstico, laudo, segmentação ou interpretação clínica por IA;
- alteração de e-mails no Gmail;
- resolução automática de identidades ambíguas;
- exclusão automática de origem ou quarentena;
- dashboard web;
- operação distribuída/multiusuário;
- banco servidor como fonte primária;
- garantia de homologação dos modelos digitais Cfaz no fluxo produtivo.

### 1.3 Arquitetura de referência

Visão conceitual solicitada:

```text
Clinicorp
    ↓
Acquisition
    ↓
Normalizer
    ↓
ClinicalPackage
    ↓
Publisher
    ↓
OneDrive
    ↓
SQLite
    ↓
CLI
    ↓
Future AI
```

Visão executável real:

```text
Gmail readonly ─┬─> TransferNowProvider ─┐
                └─> CfazProvider ────────┤
Clinicorp ────────> PatientResolver      │
                                        v
                          quarentena + validação
                                        ↓
                           ClinicalAssetNormalizer
                                        ↓
                                ClinicalPackage
                                        ↓
                 DicomReader + manifesto + resumos
                                        ↓
                         publicador idempotente
                                        ↓
             OneDrive local / Microsoft Graph
                                        ↓
              manifestos (fonte clínica publicada)
                                        ↓
               SQLite derivado + históricos locais
                                        ↓
                    CLI operacional e consultas
                                        ↓
                       Future AI (não implementado)
```

Clinicorp não é a origem dos arquivos e, portanto, não precede tecnicamente toda aquisição. Ele é a fonte mestre de identificação usada antes da publicação. OneDrive é a fonte dos artefatos publicados; o índice SQLite é uma projeção reconstruível dos manifestos e não deve substituir os arquivos publicados.

### 1.4 Princípios

- **Fail closed:** ambiguidade, erro de histórico ou validação incompleta bloqueia publicação automática.
- **Humano no circuito:** decisões de identidade e destino não inequívocas exigem revisão.
- **Idempotência:** SHA-256, IDs dos provedores, estados de publicação e histórico evitam duplicação.
- **Mínimo privilégio:** Gmail é readonly; tokens e URLs assinadas permanecem fora de logs e manifestos.
- **Dados clínicos locais:** nenhuma integração com IA está ativa.
- **Sem sobrescrita silenciosa:** conteúdo remoto existente só é reutilizado quando sua identidade é verificável.
- **Quarentena primeiro:** downloads e extrações são validados antes de publicação.
- **Fonte publicada + projeção reconstruível:** manifestos/OneDrive são duráveis; SQLite é consultável e reconstruível.
- **Compatibilidade aditiva:** evolução do manifesto e do banco preserva campos e dados existentes.
- **Observabilidade sanitizada:** logs e históricos evitam PHI, segredos, URLs e caminhos completos.
- **Uma tarefa por vez:** a seção final deste documento contém uma única prioridade.

### 1.5 Princípio obrigatório de MVP de alto valor clínico

Enquanto o IREO Clinical Intelligence permanecer uma solução de uso exclusivo
do IREO, cada função deverá ser construída como um **MVP de alto valor
clínico**: o menor esforço e a menor complexidade capazes de produzir benefício
real, transformador, perceptível e verificável para a clínica, preservando as
garantias mínimas exigidas por segurança, integridade dos dados, LGPD e
prevenção de associação incorreta de pacientes.

Essa decisão reduz escopo e complexidade antecipada; ela não reduz segurança
essencial. Permanece válida até que uma decisão formal transforme o sistema em
produto para uso externo.

#### Significado de MVP

- MVP não significa protótipo descartável.
- MVP não autoriza sistema inseguro, associação ambígua de pacientes ou
  exposição de dados.
- MVP é a menor entrega vertical utilizável que gere benefício clínico ou
  operacional mensurável no IREO.
- A arquitetura deve ser proporcional ao risco e ao uso real presente, não a
  cenários hipotéticos de comercialização ou escala.

#### Garantias mínimas inegociáveis

- LGPD e minimização de dados;
- proteção de credenciais e segredos;
- associação inequívoca entre paciente, solicitação e exame;
- prevenção de sobrescrita ou perda silenciosa;
- idempotência suficiente para o fluxo efetivamente utilizado;
- rastreabilidade clínica e operacional necessária;
- falha segura nos pontos que possam gerar associação errada, perda ou
  duplicidade relevante;
- revisão humana antes de decisões ou ações com consequência clínica;
- testes sintéticos dos caminhos reais e dos riscos essenciais.

#### Justificativa obrigatória para complexidade adicional

Qualquer aumento de complexidade deve responder a pelo menos um fator atual e
demonstrável:

- risco concreto de dano clínico ou associação incorreta;
- obrigação legal, de privacidade ou segurança;
- prevenção de perda ou sobrescrita de informação relevante;
- redução relevante e mensurável de trabalho manual;
- necessidade operacional observada no IREO;
- falha real ou provável do fluxo utilizado;
- benefício clínico, assistencial ou financeiro mensurável.

Sem essa justificativa, ficam adiados mecanismos voltados apenas a escala
hipotética, workers inexistentes, distribuição comercial futura, generalização
prematura, integrações não utilizadas, abstrações para provedores alternativos,
alta disponibilidade não requerida, automação integral onde revisão humana
simples é suficiente ou cobertura extensa de cenários fora do fluxo real.

#### Priorização de testes

Os testes devem priorizar, nesta ordem:

1. caminho clínico e operacional realmente utilizado;
2. associação correta do paciente;
3. integridade dos dados essenciais;
4. ausência de sobrescrita, duplicidade ou perda;
5. falhas prováveis e recuperáveis;
6. evidência de entrega do benefício pretendido.

Testes adicionais de infraestrutura devem ser proporcionais ao risco concreto
e não substituem validação de valor.

#### Regra para novas tarefas

Toda nova tarefa, Sprint ou instrução de implementação deve declarar:

- problema real do IREO que será resolvido;
- usuário interno beneficiado;
- benefício clínico ou operacional esperado;
- fluxo mínimo utilizável;
- riscos essenciais que precisam ser controlados agora;
- critério objetivo de sucesso;
- forma de medir o valor entregue;
- itens explicitamente adiados;
- condição que justificaria retomá-los.

#### Gate pragmático

Toda proposta deve responder objetivamente:

1. Qual problema real e atual do IREO esta mudança resolve?
2. Quem perceberá diretamente o benefício?
3. Qual é a menor implementação utilizável?
4. Qual risco concreto exige robustez adicional agora?
5. Como o valor clínico ou operacional será medido?
6. O que será explicitamente adiado?

Se as respostas não forem verificáveis e ligadas ao uso atual do IREO, a
mudança não entra no escopo vigente.

#### Sequência vigente de entregas

1. concluir a aquisição automatizada de exames de imagem;
2. validar seu benefício operacional e clínico no IREO;
3. iniciar o **Patient Recall Engine**;
4. ampliar robustez ou complexidade somente quando sustentada por risco
   concreto, benefício demonstrado ou necessidade operacional atual.

A aquisição automatizada é a entrega vertical atual. O trabalho já construído
deve ser preservado e concluído de forma pragmática, segura e utilizável.
Novos requisitos de robustez só entram quando necessários às garantias mínimas
ou ao fluxo real. Depois da conclusão técnica, a clínica deve validar benefício
perceptível. O Patient Recall Engine é a próxima entrega vertical e não começa
antes dessa conclusão e da avaliação operacional inicial.

## 2. Arquitetura Atual

### 2.1 Camadas e responsabilidades

| Camada | Implementação principal | Responsabilidade | Não deve fazer |
|---|---|---|---|
| Configuração | `src/core/config.py` | Ler flags, limites e caminhos do ambiente | Persistir segredos |
| Integrações | `src/integrations/` | Gmail, TransferNow, Microsoft Graph e OneDrive | Decidir identidade clínica |
| Clinicorp | `src/api/clinicorp_connector.py`, `src/repositories/clinicorp_patient_repository.py` | Fonte mestre de pacientes | Publicar arquivos |
| Acquisition | `src/acquisition/` | Descobrir, baixar, classificar e finalizar pacotes por provider | Conhecer regras da CLI ou inferir paciente |
| Normalizer | `clinical_normalizer.py` | Detectar formato, categoria, nome e pasta normalizados | Interpretar clinicamente o conteúdo |
| ClinicalPackage | `src/acquisition/models/clinical_package.py` | Contrato serializável dos ativos normalizados | Acessar rede |
| DICOM | `src/radiology/dicom_reader.py` | Inventário estrutural sem pixels | Diagnosticar |
| Orquestração | `supervised_import.py`, `auto_run.py`, `acquisition/service.py` | Coordenar validação, revisão, publicação e estados | Contornar bloqueios |
| Publisher/Storage | `src/storage/`, `onedrive_graph.py` | Organizar e publicar sem sobrescrita indevida | Resolver paciente |
| Histórico | `intake_history.py`, `cfaz_operations.py` | Estados de intake e importação Cfaz | Ser fonte clínica |
| Índice | `exam_index_service.py` | Projetar manifestos em SQLite para consulta | Substituir OneDrive |
| CLI | `src/main.py` | Interface operacional, manutenção e consulta | Conter regra clínica nova isolada |
| Observabilidade | `src/observability/audit_logger.py` | Eventos sanitizados e correlacionáveis | Registrar PHI/segredos |

### 2.2 Componentes externos

| Sistema | Uso | Permissão/contrato | Falha esperada |
|---|---|---|---|
| Clinicorp | Busca e validação de paciente | Leitura via API | revisão/bloqueio |
| Gmail | Descoberta de mensagens | `gmail.readonly` | revisão/bloqueio |
| TransferNow | Download de pacotes | HTTPS/Chromium temporário | fallback supervisionado |
| Cfaz | Pedidos, ativos e modelos | API oficial; sessão autenticada quando necessária | revisão, retomada ou homologação |
| OneDrive | Publicação e inventário | pasta local ou Graph | publicação falha/retomável |
| SQLite | histórico e projeção | arquivo local, transações curtas | pipeline falha fechado quando histórico é obrigatório |

## 3. Estrutura do Projeto

### 3.1 Árvore importante

```text
ireo-clinical-intelligence/
├── docs/
│   ├── ENGINEERING_MASTER.md       fonte oficial de contexto
│   ├── api/API.md                  documentação histórica da API
│   └── architecture/               arquitetura, deployment, observabilidade e operação
├── scripts/                        diagnósticos e automação operacional
├── src/
│   ├── acquisition/                aquisição multiprovedor e normalização
│   │   └── models/                 contratos serializáveis do pacote clínico
│   ├── api/                        cliente Clinicorp
│   ├── core/                       configuração
│   ├── integrations/               Gmail, TransferNow e Microsoft Graph
│   ├── intelligence/               classificação de acompanhamento
│   ├── models/                     entidades de domínio
│   ├── observability/              auditoria sanitizada
│   ├── radiology/                  intake, DICOM, histórico e índice
│   ├── repositories/               portas e adaptadores de pacientes
│   ├── services/                   serviços de aplicação
│   ├── storage/                    staging e organização OneDrive
│   ├── utils/                      formatação e logging legado
│   ├── workflows/                  orquestrações
│   └── main.py                     composição e CLI
├── tests/
│   └── fixtures/clinicorp/         contratos sanitizados do Clinicorp
├── .env.example                    catálogo de configuração, sem segredos
├── CHANGELOG.md                    changelog histórico anterior ao documento mestre
├── ROADMAP.md                      roadmap resumido legado
├── README.md                       entrada para operadores/desenvolvedores
├── pyproject.toml                  pacote, dependências, entry point e pytest
└── requirements.txt                dependências legadas
```

`ireo-cfaz-source/` existe localmente como cópia não versionada da árvore em um
estado anterior. Não é módulo de produção nem fonte oficial; sua origem e
política de remoção estão **PENDENTES DE DOCUMENTAÇÃO**.

### 3.2 Diretórios e módulos

- `acquisition/base.py`: enums e dataclasses de request/asset/pacote adquirido e contrato abstrato `AcquisitionProvider`.
- `acquisition/service.py`: executa o ciclo do provider sem acoplar provider ao publicador.
- `acquisition/transfernow_provider.py`: adapta o fluxo TransferNow legado ao contrato comum.
- `acquisition/cfaz_provider.py`: autenticação, descoberta, inventário, payloads, downloads e modelos Cfaz.
- `acquisition/cfaz_operations.py`: catálogo Gmail e histórico operacional Cfaz.
- `acquisition/cfaz_repair.py`: reparo idempotente de importações Cfaz concluídas.
- `acquisition/clinical_normalizer.py`: classificação, detecção, nomeação e `ClinicalPackage`.
- `acquisition/models/clinical_package.py`: representação serializável usada pela indexação.
- `acquisition/cfaz_*models*.py`, `cfaz_digital_*`, `cfaz_browser_session.py`, `cfaz_inventory.py`: linha local ainda não aprovada de modelos digitais.
- `api/clinicorp_connector.py`: transporte da API Clinicorp.
- `core/config.py`: variáveis de ambiente, flags, limites e caminhos.
- `integrations/gmail_connector.py`: OAuth readonly, consulta e parsing MIME.
- `integrations/transfernow_connector.py`: validação/parsing de mensagens e links.
- `integrations/microsoft_graph_auth.py`: device flow/cache MSAL.
- `integrations/onedrive_graph.py`: árvore, download JSON e uploads Graph.
- `models/`: `Patient`, `Appointment`, `EmailMessage`, `ImagingExam`, plano e resolução de paciente.
- `observability/audit_logger.py`: eventos com correlação e allowlist de campos.
- `radiology/archive_extractor.py` e `zip_extractor.py`: extração segura.
- `radiology/dicom_reader.py`: inventário DICOM sem pixels.
- `radiology/supervised_import.py`: pipeline supervisionado e publicador.
- `radiology/auto_run.py`: automação agendável fail-closed.
- `radiology/intake_history.py`: histórico SQLite sanitizado.
- `radiology/exam_index_service.py`: índice, consultas e rebuild.
- `radiology/inbox_processor.py`: processamento do inbox no workflow Graph.
- `repositories/`: porta de pacientes, implementações vazia, memória e Clinicorp.
- `storage/radiology_storage.py`: staging/estado local.
- `storage/onedrive_radiology_organizer.py`: hierarquia radiológica no Graph.
- `workflows/imaging_workflow.py`: dry-run provider-independent.
- `workflows/radiology_workflow.py`: workflow de download, ZIP, DICOM e matching.
- `main.py`: entry point `ireo-clinical-intelligence`.

### 3.3 Estado dos módulos

Percentuais são estimativas de prontidão operacional, não cobertura de código.

### Clinicorp — 90%

- **Status:** funcional como fonte mestre de identificação e repositório de candidatos.
- **Entregue:** contrato, normalização, matching determinístico, tratamento de duplicidade e registros inválidos.
- **Problemas:** disponibilidade e qualidade do cadastro externo continuam sendo dependências; casos ambíguos exigem revisão.
- **Última entrega confirmada:** Issues 31–35 / base do MVP.

### Acquisition — 90%

- **Status:** interface multiprovedor implementada; TransferNow maduro; Cfaz em evolução.
- **Entregue:** contrato `AcquisitionProvider`, descoberta, download, classificação, retomada, pacote adquirido e identidades estáveis.
- **Problemas:** modelos digitais Cfaz ainda estão em trabalho local/homologação; há mudanças não commitadas.
- **Última Sprint confirmada:** 16.1; trabalho local posterior associado à linha 17.x.

### Normalizer — 90%

- **Status:** normalização multiformato implementada.
- **Entregue:** categorias clínicas, MIME/magic bytes, nomes determinísticos, pastas e deduplicação.
- **Problemas:** necessita validação com corpus real amplo e formatos proprietários adicionais.
- **Última Sprint confirmada:** 16.1.

### ClinicalPackage — 90%

- **Status:** contrato serializável implementado.
- **Entregue:** ativos, metadados do provedor, checksums, categorias e reconstrução por dicionário.
- **Problemas:** campos `digital_model_id` e `stl_file_id` existem apenas nas alterações locais até esta atualização.
- **Última Sprint confirmada:** 16.3; extensão local posterior.

### DICOM Intelligence — 90%

- **Status:** inventário estrutural implementado e integrado.
- **Entregue:** estudos, séries, geometria estimada, consistência de paciente e datas; `dicom_summary.json`.
- **Problemas:** formatos proprietários podem ficar como não reconhecidos; não há interpretação clínica.
- **Última Sprint confirmada:** 13.

### Publisher / OneDrive — 85%

- **Status:** publicação local e Graph implementadas; publicação idempotente com estados.
- **Entregue:** navegação, criação controlada, small upload, organização radiológica, manifestos e retomada.
- **Problemas:** rede, permissões e conflitos remotos exigem operação fail-closed; upload grande e cenários reais múltiplos ainda precisam de validação contínua.
- **Última Sprint confirmada:** 16.1 (integração no pipeline).

### SQLite / Index — 90%

- **Status:** histórico sanitizado e índice derivado implementados.
- **Entregue:** schemas versionados, rebuild, consultas, timeline, dashboard e ativos clínicos.
- **Problemas:** `INDEX_VERSION` permanece 1 apesar de migrações aditivas; não há ferramenta única formal de migração/backup.
- **Última Sprint confirmada:** 17; ajustes locais posteriores.

### CLI — 85%

- **Status:** ampla interface operacional e de consulta em `src/main.py`.
- **Entregue:** dry-run, importação supervisionada/automática, histórico, rebuild, consultas e operações Cfaz.
- **Problemas:** arquivo monolítico; alguns comandos de modelos digitais pertencem a mudanças locais ainda não aprovadas; documentação de comandos está fragmentada.
- **Última Sprint confirmada:** 17; trabalho local posterior.

### Automação — 80%

- **Status:** runner local agendável, opt-in e fail-closed.
- **Entregue:** lock, data inicial, fila `REVIEW_REQUIRED`, resumo sanitizado e kill switch.
- **Problemas:** depende de máquina ligada, sessão/token válidos e revisão externa das pendências.
- **Última Sprint confirmada:** fluxo autônomo pós-v1.0.0.

### Future AI — 0%

- **Status:** não implementado por decisão de segurança e escopo.
- **Pré-condições:** governança de dados, finalidade clínica explícita, anonimização, avaliação, auditoria, aprovação regulatória e critérios de não dano.

### 3.4 Bancos, manifestos e caches

Os bancos e tabelas estão detalhados na seção 6. O `manifest.json` é o contrato
durável da publicação; `dicom_summary.json` e `resumo_do_exame.txt` são
artefatos derivados publicados junto ao exame. Os caches/estados conhecidos
são:

- cache MSAL configurado por `MS_GRAPH_TOKEN_CACHE_FILE`, local e ignorado;
- token OAuth Gmail configurado localmente e ignorado;
- estado de retomada Cfaz dentro da quarentena, sem URLs assinadas;
- staging `supervised-staging`, que preserva manifestos locais;
- resumo atômico da última execução automática;
- lock do agendador Windows;
- SQLite e seus auxiliares WAL/SHM;
- cache em memória por `message_id` do dry-run legado;
- caches em memória de inventário durante rebuild.

Formato, retenção e expiração unificados desses caches: **PENDENTE DE DOCUMENTAÇÃO**.

### 3.5 Especificação física dos bancos

#### 3.5.1 Topologia e propriedade

Por configuração padrão, histórico de intake, histórico Cfaz e índice radiológico podem compartilhar `data/ireo_intake.db`/caminho configurado. São tabelas independentes no mesmo SQLite.

| Conjunto | Fonte | Escritor | Leitor | Natureza |
|---|---|---|---|---|
| Intake | execução de importação | `IntakeHistoryRepository`, auto-run e importer | CLI de histórico/revisão | histórico operacional |
| Cfaz | operações de pedidos | `CfazHistoryRepository` | catálogo e CLI Cfaz | histórico operacional |
| Índice clínico | manifestos publicados | `ExamIndexService`, rebuild | CLI de consulta/dashboard | projeção reconstruível |

Pragmas relevantes: `foreign_keys=ON`; histórico de intake usa `busy_timeout=10000` e WAL. Conexões são curtas e operações de escrita crítica usam transação explícita.

#### 3.5.2 `schema_info`

| Campo | Tipo | Regra |
|---|---|---|
| `key` | TEXT | PK |
| `value` | INTEGER | obrigatório |

Escrito/lido por `IntakeHistoryRepository`. Registra `schema_version=2`.

#### 3.5.3 `radiology_imports`

| Campo | Tipo | Observação |
|---|---|---|
| `id` | INTEGER | PK autoincremento |
| `correlation_id` | TEXT | correlação sanitizada, obrigatório |
| `run_id` | TEXT | execução automática |
| `gmail_message_id_hash` | TEXT | fingerprint SHA-256 |
| `transfer_fingerprint` | TEXT | fingerprint da transferência |
| `archive_filename_masked` | TEXT | nome mascarado |
| `archive_size` | INTEGER | >= 0 |
| `archive_sha256` | TEXT | identidade forte do arquivo |
| `patient_id_hash` | TEXT | fingerprint do paciente |
| `destination_fingerprint` | TEXT | fingerprint do destino |
| `file_count` | INTEGER | >= 0 ou nulo |
| `total_size` | INTEGER | >= 0 ou nulo |
| `manifest_path_fingerprint` | TEXT | caminho mascarado por hash |
| `file_checksums_fingerprint` | TEXT | fingerprint do conjunto |
| `reimport_confirmed` | INTEGER | booleano, default 0 |
| `previous_record_reference` | TEXT | vínculo lógico com importação anterior |
| `status` | TEXT | estado obrigatório |
| `reason_code` | TEXT | código sanitizado |
| `stage` | TEXT | etapa |
| `exception_type` | TEXT | classe sanitizada |
| `created_at_utc` | TEXT | obrigatório |
| `updated_at_utc` | TEXT | atualização |
| `completed_at_utc` | TEXT | conclusão |

Índices: `(archive_sha256,status)`, `(gmail_message_id_hash,status)`, `(transfer_fingerprint,status)`.

Estados permitidos: `DOWNLOADED`, `EXTRACTED`, `READY_FOR_CONFIRMATION`, `COMPLETED`, `FAILED`, `CANCELLED`, `DUPLICATE_DETECTED`, `REIMPORT_CONFIRMED`, `REVIEW_REQUIRED`, `RESET`.

#### 3.5.4 `cfaz_import_history`

| Campo | Tipo | Observação |
|---|---|---|
| `provider` | TEXT | parte da PK |
| `request_id` | TEXT | parte da PK |
| `provider_request_id` | TEXT | chave interna da API |
| `sequential_id` | TEXT | número visível |
| `clinic_number` | TEXT | número operacional |
| `repaired_at` | TEXT | reparo |
| `repair_version` | INTEGER | versão do reparo |
| `provider_exam_id` | TEXT | exame do provedor |
| `acquisition_sha` | TEXT | identidade da aquisição |
| `import_timestamp` | TEXT | instante |
| `message_id_hash` | TEXT | Gmail mascarado |
| `patient_name` | TEXT | atenção: dado identificador local |
| `status` | TEXT | estado |
| `started_at` / `completed_at` | TEXT | tempos |
| `duration_seconds` | REAL | duração |
| `onedrive_destination` | TEXT | destino |
| `error_code` | TEXT | código sanitizado |
| `updated_at` | TEXT | atualização obrigatória |

PK: `(provider,request_id)`. Índices por status, `provider_request_id` e `sequential_id`.

#### 3.5.5 Índice clínico

#### `index_metadata`

`key TEXT PRIMARY KEY`, `value TEXT NOT NULL`. Mantido por `ExamIndexService`; `schema_version=1`.

#### `patients`

`patient_key` PK; `normalized_name`; `display_name`; `masked_patient_id`; `first_exam_date`; `last_exam_date`; `updated_at`.

É pai de `exams` e `clinical_assets`. A chave é SHA-256 do nome normalizado no contrato atual.

#### `exams`

`exam_id` PK; FK `patient_key -> patients`; data/hora/fonte; status; modalidade; fabricante; modelo; software; instituição; voxel/FOV em JSON; contagens; SHA do arquivo; destino OneDrive; tempos de importação/indexação; `index_version`.

#### `studies`

PK `(exam_id,study_instance_uid)`; FK para `exams` com cascade; data/hora; modalidade; descrição; fabricante/modelo/software/instituição; contagem de séries.

#### `series`

PK `(exam_id,series_instance_uid)`; FK para `exams` com cascade; `study_instance_uid`; modalidade; descrição; imagens; voxel/FOV JSON; linhas/colunas.

#### `import_history`

`id` PK; `exam_id`; `state`; `source`; `index_version`; `indexed_at`; `detail_code`. Histórico do índice, distinto do intake.

#### `exam_assets`

PK `(exam_id,asset_index)`; FK para `exams` com cascade; coleção; nome/pasta; MIME/extensão; tipo/categoria; dimensões/tamanho/SHA; flags de thumbnail/duplicidade; referência da duplicata; instante de normalização.

#### `clinical_assets`

`asset_id` PK; FK `exam_id -> exams`; provider/request/sequential; FK `patient_id -> patients.patient_key`; categoria, coleção, seção e nome do provedor; `digital_model_id`; `stl_file_id`; nome/caminho; MIME/extensão/tamanho/SHA/dimensões; thumbnail; tempos.

Unicidade: `(exam_id,sha256,relative_path)`.

#### `consistency_issues`

`id` PK; FK para `exams` com cascade; `code`; `severity`; `detail`. Unicidade `(exam_id,code,detail)`.

#### `rebuild_state`

`remote_path` PK; `exam_id`; `index_version`; `status`; `updated_at`; `error_code`. Checkpoint do rebuild.

#### 3.5.6 Relacionamentos

```text
patients 1 ─── N exams
patients 1 ─── N clinical_assets
exams    1 ─── N studies
exams    1 ─── N series
exams    1 ─── N exam_assets
exams    1 ─── N clinical_assets
exams    1 ─── N consistency_issues

radiology_imports       (independente; vínculo por fingerprints)
cfaz_import_history     (independente; vínculo por IDs do provider)
import_history          (histórico interno do índice)
rebuild_state           (checkpoint por caminho remoto)
```

### 2.3 Pipeline completo

#### 2.3.1 Descoberta

1. Operador/automação escolhe provider e modo.
2. Gmail readonly lista notificações dentro dos limites configurados, ou a CLI recebe `request-id`/arquivo explícito.
3. TransferNow valida domínio/link público; Cfaz resolve IDs documentados na API.
4. Histórico é consultado antes da aquisição para impedir importação já concluída.

#### 2.3.2 Aquisição

5. Provider autentica com segredos somente em memória/headers.
6. Pedido é descoberto sem inferir rotas não documentadas.
7. Ativos são enumerados e classificados.
8. Cada arquivo é transferido por streaming para quarentena com limite de tamanho.
9. MIME, magic bytes, tamanho e SHA-256 são validados.
10. ZIP/RAR é extraído com proteção contra traversal e limites de expansão.
11. Estado local permite retomada de itens completos.
12. Pacote multiparte pode ser consolidado deterministicamente.

#### 2.3.3 Identificação e normalização

13. Clinicorp fornece candidatos; o nome é normalizado deterministicamente.
14. Candidato único e forte pode ser selecionado somente conforme as flags e allowlist; ambiguidade exige revisão.
15. `DicomReader` inventaria estudos/séries sem pixels e verifica consistência com o paciente confirmado.
16. `ClinicalAssetNormalizer` determina categoria, extensão, nome e pasta.
17. Thumbnails genéricos são excluídos da publicação; previews clínicos autorizados são preservados.
18. Ativos duplicados são marcados por SHA-256.
19. O resultado vira `ClinicalPackage`.

#### 2.3.4 Publicação

20. A data clínica é resolvida uma vez com fonte explícita.
21. O destino do paciente é localizado sem criar associação ambígua.
22. Manifesto, resumo DICOM e resumo legível são gerados no staging.
23. A publicação entra em `IN_PROGRESS`.
24. Arquivos são enviados/copiados com nome determinístico e sem sobrescrita não verificada.
25. Checksums e existência remota são conferidos.
26. O manifesto passa a `COMPLETE`; falhas ficam `FAILED` e são retomáveis.

#### 2.3.5 Persistência e consulta

27. Histórico de intake/Cfaz é atualizado.
28. `ExamIndexService` projeta o manifesto em SQLite.
29. Falha do índice não invalida uma publicação remota já `COMPLETE`; rebuild posterior recupera a projeção.
30. CLI consulta exclusivamente SQLite para buscas, timeline, comparação e dashboard.
31. Future AI permanece desligado até decisão arquitetural, clínica e regulatória formal.

## 4. Histórico Cronológico do Projeto

As datas e commits abaixo vêm do Git. Rótulos de Sprint ausentes no histórico não foram inventados.

### Sprint 1 — Integração Clinicorp e arquitetura inicial

- **Data/commit:** 2026-07-12, `3578823`.
- **Objetivo:** fundação do projeto, Clinicorp, serviços de paciente/agenda e maintenance engine.
- **Resultado:** arquitetura Python inicial e CLI básica.
- **Arquivos-chave:** `src/api/clinicorp_connector.py`, `src/core/config.py`, `src/main.py`, `src/services/`.
- **Testes:** não há suíte registrada no commit.
- **Status:** concluída.

### Sprint 2 — Estrutura inicial do Radiology Intake

- **Data/commit:** 2026-07-13, `47eef3f`.
- **Objetivo:** criar domínio e fluxo inicial de radiologia/TransferNow.
- **Resultado:** `ImagingExam`, connector e workflow inicial.
- **Arquivos-chave:** `src/integrations/transfernow_connector.py`, `src/models/imaging_exam.py`, `src/workflows/imaging_workflow.py`.
- **Testes:** não há testes adicionados no commit.
- **Status:** concluída.

### Fase 2 / Sprint não numerada — Dry-run

- **Data/commit:** 2026-07-13, `bd5960c`.
- **Objetivo:** planejar intake sem efeitos externos.
- **Resultado:** `RadiologyIntakePlan`, matcher e serviço de importação em memória.
- **Arquivos-chave:** domínio, matcher, serviço, workflow e arquitetura.
- **Testes:** `test_radiology_dry_run.py`.
- **Status:** concluída.

### Issues 31–32 — Normalização e resolução de pacientes

- **Data/commit:** 2026-07-13, `04d0d9c`.
- **Objetivo:** separar normalização, resolução e Gmail readonly.
- **Resultado:** resolver determinístico, modelos e dry-run Gmail.
- **Testes:** normalizador, resolver e Gmail dry-run.
- **Status:** concluída.

### Issues 31–33 — Repositório Clinicorp

- **Data/commit:** 2026-07-13, `413241d`.
- **Objetivo:** conectar resolução ao Clinicorp por abstração de repositório.
- **Resultado:** `ClinicorpPatientRepository`.
- **Testes:** contrato amplo do repositório.
- **Status:** concluída.

### Issues 31–35 — Importação supervisionada

- **Data/commit:** 2026-07-13, `8c2f167`.
- **Objetivo:** quarentena, extração, auditoria, seleção e publicação supervisionada.
- **Resultado:** fluxo supervisionado e observabilidade sanitizada.
- **Testes:** contratos Clinicorp, audit logger, importação supervisionada.
- **Status:** concluída.

### Issues 34–36 — Fechamento do MVP

- **Data/commit:** 2026-07-14, `b327369`.
- **Objetivo:** consolidar importação radiológica supervisionada.
- **Resultado:** suporte e validação WinRAR e endurecimento do fluxo.
- **Testes:** suíte supervisionada e integração local WinRAR.
- **Status:** concluída.

### MVP v1.0.0

- **Data/commit/tag:** 2026-07-15, `bbfd22c`, tag `v1.0.0`.
- **Objetivo:** congelar o piloto supervisionado.
- **Resultado:** Gmail → TransferNow → quarentena → Clinicorp → OneDrive local → manifesto.
- **Testes:** download HTTP/browser e seleção de link, além da suíte existente.
- **Status:** concluída e validada em piloto supervisionado conforme README.

### Evolução pós-MVP — Auto-seleção segura

- **Data/commit:** 2026-07-15, `b8a6f34`.
- **Objetivo:** reduzir escolhas intermediárias apenas em casos inequívocos.
- **Resultado:** feature flag, limiar próprio, allowlist e override manual.
- **Testes:** ampliação da suíte supervisionada.
- **Status:** concluída, opt-in.

### Evolução pós-MVP — Automação fail-closed

- **Data/commit:** 2026-07-15, `0eb172c`.
- **Objetivo:** execução agendada segura e fila de revisão.
- **Resultado:** auto-run, SQLite, lock, resumo e revisão pendente.
- **Testes:** auto-run, histórico e download browser.
- **Status:** concluída.

### Microsoft Graph / OneDrive

- **Datas/commits:** 2026-07-20 a 2026-07-21, `53d1a93`, `ad9c6b5`, `9e9ede1`.
- **Objetivo:** autenticação Graph, navegação compartilhada e small upload.
- **Resultado:** cliente Graph, cache protegido e scripts diagnósticos.
- **Testes:** autenticação e Graph.
- **Status:** concluída.

### Sprint não numerada — Workflow radiológico completo

- **Data/commit/tag:** 2026-07-21, `deac51b`, tag também aponta `v0.15.0`.
- **Objetivo:** fluxo completo de intake e arquivamento via Graph.
- **Resultado:** inbox processor, DICOM básico, storage, organizer e workflow.
- **Testes:** DICOM, inbox, storage, organizer, workflow e integrações.
- **Status:** concluída. O histórico disponível não informa um número de Sprint; nenhum número foi inferido.

### Sprint 13 — Inteligência estrutural DICOM

- **Evidência:** registrada em `docs/architecture/ARCHITECTURE.md`; código consolidado em `da3a195`.
- **Objetivo:** inventário estrutural seguro, datas clínicas, resumos e consistência.
- **Resultado:** análise de estudos/séries e artefatos explicativos.
- **Testes:** `test_dicom_intelligence.py` e `test_dicom_reader.py`.
- **Status:** concluída.

### Sprint 14 / 14.1 — Índice radiológico e rebuild

- **Evidência:** arquitetura; código consolidado em `da3a195`.
- **Objetivo:** SQLite derivado dos manifestos e rebuild eficiente/observável.
- **Resultado:** projeções, consultas, checkpoints, retries e inventário remoto.
- **Testes:** `test_exam_index_service.py`, `test_onedrive_graph.py`.
- **Status:** concluída.

### Sprint 15 / 15.1 — Aquisição multiprovedor e Cfaz

- **Evidência:** arquitetura; código consolidado em `da3a195`.
- **Objetivo:** providers independentes, API Cfaz, catálogo e histórico próprio.
- **Resultado:** `AcquisitionProvider`, Cfaz/TransferNow providers, operações e reparo.
- **Testes:** acquisition providers, operações e reparo Cfaz.
- **Status:** concluída para ativos suportados.

### Sprint 16.1 — Normalização clínica

- **Data/commit/tag:** 2026-07-24, `da3a195`, tag `v0.16.1`.
- **Objetivo:** normalização clínica multiformato e integração completa.
- **Resultado:** normalizer, índice, aquisição e publicação ampliados.
- **Arquivos:** 39 arquivos; principais em `src/acquisition/`, `radiology/` e `integrations/`.
- **Testes:** grande ampliação, incluindo normalização, DICOM, índice, Cfaz e Graph.
- **Status:** concluída.

### Sprint 16.2 — ClinicalPackage e preservação da estrutura clínica

- **Fonte histórica:** livro de engenharia exportado do Pages; não houve commit/tag individual.
- **Objetivo:** tornar `ClinicalPackage` o contrato oficial entre providers e pipeline.
- **Resultado:** `ClinicalAsset` preserva coleção, seção, nome, categoria e metadados do provider; TransferNow e Cfaz compartilham o contrato; nenhuma indexação foi antecipada.
- **Validação registrada:** 492 testes aprovados e 2 ignorados.
- **Decisão:** nenhum módulo posterior interpreta diretamente o payload Cfaz.
- **Status:** concluída; checkpoint Git individual não foi criado.
- **Documento especializado:** `docs/history/sprint-16.2.md`.

### Sprint 16.3 — Indexação de ativos clínicos

- **Data/commit/tag:** 2026-07-24, `4c2860f`, tag `v0.16.3`.
- **Objetivo:** materializar ativos clínicos consultáveis.
- **Resultado:** `ClinicalPackage` formal e tabela/consultas `clinical_assets`.
- **Testes:** `test_clinical_package.py` e suítes correlatas.
- **Status:** concluída.

### Sprint 17 — Camada de consulta clínica

- **Data/commit:** 2026-07-24, `d065a6f`.
- **Objetivo:** expor consultas estruturadas sobre o índice.
- **Resultado:** CLI de assets, timeline, resumo e dashboard.
- **Arquivos:** `src/main.py`, `src/radiology/exam_index_service.py`.
- **Testes:** nenhuma alteração de testes nesse commit; cobertura herdada.
- **Status:** concluída no Git.

### Sprint 17.1

- **Objetivo:** Clinical Timeline por paciente.
- **Implementação:** `patient-timeline`, exclusivamente sobre SQLite.
- **Resultado:** ordenação por data/tipo/provider e categorias persistidas, sem reclassificação.
- **Validação histórica:** 492 testes aprovados, 2 ignorados.
- **Documento:** `docs/history/sprint-17.1.md`.

### Sprint 17.2

- **Objetivo:** Clinical Search.
- **Implementação:** `find-assets` com filtros de paciente, categoria, provider e datas.
- **Resultado:** busca agregada somente SQLite, sem serviços externos.
- **Validação histórica:** 492 testes aprovados, 2 ignorados.
- **Documento:** `docs/history/sprint-17.2.md`.

### Sprint 17.3

- **Objetivo:** Clinical Summary.
- **Implementação:** `patient-summary` com contagens e intervalo do acervo.
- **Resultado:** resumo reflete apenas dados persistidos; não inventa categorias históricas.
- **Documento:** `docs/history/sprint-17.3.md`.

### Sprint 17.4

- **Objetivo:** Dashboard operacional textual.
- **Implementação:** `dashboard`, agregações globais somente leitura.
- **Resultado:** pacientes, pedidos, assets, categorias, qualidade e datas consultáveis.
- **Documento:** `docs/history/sprint-17.4.md`.

### Sprint 17.5

- **Objetivo:** Thumbnail Filtering & Clinical Publication.
- **Descoberta:** 30 JPEG de um pedido sanitizado eram 15 originais + 15 thumbnails, com 30 SHA únicos; não havia duplicação real.
- **Implementação:** filtro por asset, `thumbnail-report`, rebuild/reprocessamento sem exclusão remota.
- **Resultado:** 58 registros reduziram para 29 ativos clínicos indexáveis.
- **Documento:** `docs/history/sprint-17.5.md`.

### Sprint 17.6

- **Objetivo:** Cfaz Network Discovery.
- **Problema:** modelos digitais apareciam no portal, mas não no payload persistido.
- **Descoberta:** frontend Vue usa objeto `digitalModel`, `data.stl_files` e campos `id`, `download_url`, `document_file_name`; requisições de arquivo chegam ao Google Storage.
- **Resultado:** evidência técnica para aquisição somente leitura, sem usar `PUT /digital_models/{id}.js`.
- **Documento:** `docs/history/sprint-17.6.md`.

### Sprint 17.7

- **Objetivo:** Cfaz Digital Model Acquisition.
- **Implementação:** parsing `digital_models[].stl_files[]`, IDs de modelo/STL, categoria `DIGITAL_MODEL`, migração e diagnóstico dry-run.
- **Segurança:** URL assinada somente em memória; nenhuma rota Cfaz de escrita.
- **Validação histórica:** 492 testes aprovados, 2 ignorados.
- **Status:** implementada; homologação real de download/publicação continuou pendente.
- **Documento:** `docs/history/sprint-17.7.md`.

### Sprint 17.8

- **Objetivo:** Local Reset & Reimport.
- **Implementação:** `cfaz-reset --dry-run|--apply`, inventário e limpeza local restrita ao pedido, preservando auditoria/OneDrive.
- **Homologação:** reset real removeu 1 manifesto, 1 pacote, 15 assets, 1 exame e preservou outros pacientes.
- **Bug descoberto:** intake `COMPLETED`/destination fingerprint ainda bloqueava reimportação após o primeiro reset.
- **Status:** implementação consolidada em `18540ea`; homologação ponta a ponta dos STL permanece aberta.
- **Documento:** `docs/history/sprint-17.8.md`.

## 5. Histórico de Comandos

O executável é definido em `pyproject.toml` como
`ireo-clinical-intelligence = "main:main"`. Foram catalogados 31 comandos
nomeados e um modo interativo sem subcomando. “Dry-run” abaixo significa
ausência garantida de publicação; “Apply” significa que o comando pode escrever.

### 5.1 Consulta Clinicorp interativa

- **Objetivo:** consultar paciente, agendamentos e classificação de acompanhamento.
- **Sintaxe/exemplo:** `ireo-clinical-intelligence`.
- **Fluxo interno:** `ClinicorpAPI` → `PatientService` → `AppointmentService` → `MaintenanceEngine`.
- **Dry-run/Apply:** somente leitura externa; não altera Clinicorp.
- **Riscos:** imprime dados identificáveis no terminal; uso deve ocorrer em estação autorizada.

### 5.2 Diagnóstico e inbox

| Comando | Objetivo e sintaxe | Fluxo interno | Dry-run / Apply / riscos |
|---|---|---|---|
| `browser-self-test` | validar Playwright; `ireo-clinical-intelligence browser-self-test` | inicia teste controlado do Chromium | diagnóstico; pode abrir browser |
| `transfernow-link-diagnosis` | diagnosticar link da mensagem; sem flags | Gmail readonly → seleção/diagnóstico | não publica; acessa Gmail |
| `process-radiology-inbox` | processar até 20 mensagens; `... --max-messages 5` | Gmail → workflow → storage → organizer Graph | apply; pode publicar; não tem dry-run explícito |
| `radiology-gmail-dry-run` | planejar mensagens Gmail; `... [--patient-source clinicorp|offline]` | Gmail → `ImagingWorkflow` → plano | dry-run real; Clinicorp opcional |
| `radiology-import-supervised` | importar mensagem ou arquivo; `... (--email-message-id ID|--archive-path PATH) [--patient-source ...] [--force-manual-selection] [--allow-reimport]` | aquisição/extrator → paciente → manifesto → OneDrive → histórico/índice | apply supervisionado; `CONFIRMAR`; reimport exige confirmação adicional |
| `radiology-import-from-gmail` | selecionar/baixar/importar Gmail; `... [--patient-source ...] [--force-manual-selection] [--allow-reimport]` | Gmail → HTTP/browser → importer supervisionado | apply; rede e publicação; preserva download se importer não iniciar |
| `radiology-auto-run` | executar automação agendada; sem flags CLI | flags de ambiente → Gmail → importer estrito → resumo/fila | apply somente se flags autorizarem; fail-closed |
| `radiology-auto-status` | ler último resumo; sem flags | JSON local → terminal | somente leitura |

### 5.3 Histórico e revisão

| Comando | Objetivo e sintaxe | Fluxo interno | Dry-run / Apply / riscos |
|---|---|---|---|
| `intake-history` | listar histórico; `... [--limit N] [--status STATUS] [--correlation-id ID]` | SQLite → linhas sanitizadas | leitura |
| `intake-review-list` | listar `REVIEW_REQUIRED`; sem flags | SQLite → linhas sanitizadas | leitura |
| `intake-review-resume` | retomar por correlação; `... --correlation-id ID` | SQLite → localizar SHA na quarentena → import supervisionado | apply após interação; hashing de arquivos locais |

### 5.4 Índice e inteligência clínica

| Comando | Objetivo e sintaxe | Fluxo interno | Dry-run / Apply / riscos |
|---|---|---|---|
| `find-assets` | busca agregada; `... [--patient P] [--category C] [--provider V] [--after D] [--before D]` | SQLite → agregação | leitura |
| `clinical-assets` | listar ativos JSON; `... [--patient P] [--provider V] [--category C]` | `clinical_assets` → JSON lines | leitura; saída pode conter metadados locais |
| `clinical-assets-rebuild` | reconstruir ativos de manifestos locais; sem flags | staging → manifestos COMPLETE → SQLite | apply somente no índice local; não acessa rede |
| `patient-timeline` | timeline; `... --patient P` | patients/exams/assets → eventos | leitura |
| `patient-summary` | resumo quantitativo; `... --patient P` | SQLite → contagens/datas | leitura |
| `dashboard` | dashboard agregado; sem flags | SQLite → métricas | leitura |
| `thumbnail-report` | inspecionar thumbnails em manifests locais; sem flags | staging → manifestos → relatório | leitura |
| `rebuild-radiology-index` | rebuild remoto; `... [--full] [--patient P]` | Graph inventário → manifests → SQLite/checkpoints | apply local; `--full` limpa projeções somente depois do inventário remoto bem-sucedido |

### 5.5 Cfaz

| Comando | Objetivo e sintaxe | Fluxo interno | Dry-run / Apply / riscos |
|---|---|---|---|
| `cfaz-list-notifications` | listar Gmail Cfaz; `... [--debug]` | Gmail → catálogo → histórico | leitura; debug sanitizado |
| `cfaz-history` | listar histórico; sem flags | SQLite Cfaz → terminal | leitura |
| `radiology-import-from-cfaz` | importar pedido; `... [--request-id ID|--select] [--debug-auth] [--debug-payload]` | catálogo/API → provider → pacote → importer → OneDrive/SQLite | apply; sem dry-run explícito; debug deve permanecer sanitizado |
| `cfaz-repair-files` | reparar importações; `... (--request-id ID...|--all) [--dry-run|--apply]` | histórico/staging → plano → Graph → manifesto | dry-run padrão; apply move/renomeia/atualiza |
| `cfaz-reprocess` | remover thumbnails e reindexar localmente; `... --request-id ID [--include-digital-models] [--dry-run|--apply]` | staging → manifesto → índice | dry-run padrão; apply altera manifesto local/SQLite com rollback |
| `cfaz-browser-login` | criar/reutilizar sessão browser; sem flags | Playwright persistente → login | altera perfil local; risco de sessão/credencial |
| `cfaz-download-models` | baixar e validar dois STL; `... --request-id ID --browser-session` | browser → downloads temporários → validação | não publica; rede e arquivos temporários |
| `cfaz-digital-models-identify` | comparar descriptors/filenames; `... --request-id ID [--browser-session] [--manual-model-trigger]` | API → descriptors → validação | diagnóstico; atualmente relata zero incorporações |
| `cfaz-digital-models-collection-identify` | validar coleção; `... --request-id ID [--browser-session] [--manual-model-trigger]` | API/browser → Google Storage/ZIP → STL temporário | diagnóstico; não persiste |
| `cfaz-reimport` | planejar reimport com modelos; flags obrigatórias `--request-id --include-digital-models --browser-session --manual-model-trigger --dry-run`; `--apply` existe mas é bloqueado | API/browser → coleção → plano → consulta SQLite | apenas dry-run no contrato atual; retorna bloqueio sem destino remoto consultável |
| `cfaz-digital-models` | suplementar modelos; `... --request-id ID [--browser-session] [--manual-model-trigger] [--dry-run|--apply]` | provider/browser → validação STL → Graph → manifesto → índice | dry-run padrão; apply remoto/local, ainda em homologação |
| `cfaz-reset` | reset local preservando remoto; `... --request-id ID [--dry-run|--apply]` | histórico/manifests/SQLite → inventário → exclusão local/status | dry-run padrão; apply destrutivo local, OneDrive não é alterado |

Para todos os comandos, códigos de saída e exemplos completos de cada combinação:
**PENDENTE DE DOCUMENTAÇÃO**. Os comandos da linha 17.x estão versionados em
`18540ea`/`v0.17.8`; os de modelos digitais continuam sujeitos à homologação
operacional registrada em BUG-002/BUG-006.

## 6. Banco de Dados

A especificação completa de campos, relacionamentos e índices está em
“3.5 Especificação física dos bancos”, acima, e é normativa. A separação física
é lógica: `schema_info`/`radiology_imports`, `cfaz_import_history` e as tabelas
do índice podem coexistir no mesmo arquivo configurado.

### 6.1 Motivação e migrações

- `radiology_imports` guarda evidência operacional sanitizada e suporta prevenção de duplicidade, revisão e retomada.
- `cfaz_import_history` mantém identidade/estado do provider sem inferir a partir do índice.
- `patients`, `exams`, `studies`, `series`, `exam_assets` e `clinical_assets` são projeções consultáveis.
- `consistency_issues` registra divergências estruturais; `rebuild_state` é checkpoint; `import_history` audita indexações.
- O intake usa `SCHEMA_VERSION=2`, rejeita versão futura e adiciona colunas faltantes por `ALTER TABLE`.
- O índice usa `INDEX_VERSION=1`, `CREATE TABLE IF NOT EXISTS` e migra aditivamente `digital_model_id`/`stl_file_id`.
- Cfaz adiciona IDs e campos de reparo aditivamente.
- Não existe downgrade automático, framework externo de migração ou política formal de backup/restore: **PENDENTE DE DOCUMENTAÇÃO**.

## 7. ClinicalPackage

Há duas representações relacionadas: a interna em `clinical_normalizer.py` e a
serializável em `acquisition/models/clinical_package.py`. A consolidação é
dívida técnica.

### 7.1 Campos do pacote serializável

| Campo | Origem e função |
|---|---|
| `schema_version` | versão do contrato, obtida do manifesto |
| `provider` | provider de aquisição |
| `request` | metadados do pedido |
| `assets` | sequência imutável de `ClinicalAsset` |
| `normalizer_version` | versão do normalizador |
| `normalized_at` | instante UTC |
| `manifest` | cópia do manifesto de origem/compatibilidade |

### 7.2 Campos de `ClinicalAsset`

`original_filename`, `relative_path`, `mime_type`, `extension`, `size_bytes`,
`sha256`, `width`, `height`, `is_thumbnail`, `clinical_category`,
`provider_collection`, `provider_section`, `provider_display_name`,
`normalized_filename`, `download_source`, `provider_metadata`,
`digital_model_id` e `stl_file_id`.

`from_dict()` aceita aliases legados: `provider_exam_id` pode preencher
`digital_model_id`; `provider_asset_id` pode preencher `stl_file_id`; quando
`provider_metadata` falta, `provider`/`provider_id` são preservados. Essa
compatibilidade é aditiva. Não existe registro central de versões além dos
campos do manifesto: política formal de evolução é **PENDENTE DE DOCUMENTAÇÃO**.

## 8. Clinical Assets

Categorias confirmadas: `RADIOGRAPH`, `PHOTOGRAPH`, `TOMOGRAPHY`,
`DIGITAL_MODEL`, `REPORT`, `DOCUMENTATION`, `AUXILIARY` e `UNKNOWN`. Classificações
de aquisição também distinguem DICOM/arquivo compactado conforme o provider.

O normalizador usa extensão, MIME, magic bytes, dimensões, coleção/seção e
metadados do provider. O nome final é determinístico, sanitizado e organizado
por pastas clínicas numeradas; exemplos confirmados incluem
`04 - Modelos Digitais/modelo_maxila_001.stl` e
`modelo_mandibula_001.stl`. SHA-256 identifica conteúdo e duplicidades.

`provider_metadata` mantém apenas metadados autorizados; `provider_collection`,
`provider_section` e `provider_display_name` preservam a semântica da origem
sem acoplar consultas ao payload bruto. Thumbnails genéricos são ignorados;
preview de tomografia pode ser ativo clínico. A tabela `clinical_assets` é a
projeção de busca, timeline, dashboard e resumo, não o repositório do arquivo.

## 9. Fluxo Cfaz

### 9.1 Identidade e autenticação

`CfazProvider` prioriza a API oficial configurada do Cfaz. Token de API ou credenciais de
sessão ficam em memória/headers. `provider_request_id` é a chave interna,
`sequential_id` é o número visível e `clinic_number` é o número operacional da
clínica. A CLI `--request-id` aceita internal/sequential conforme o caminho.

### 9.2 Descoberta

Gmail é usado apenas para reconhecer notificações e extrair um Request ID. O
modo explícito não constrói Gmail. Um 404 no detalhe pode acionar somente a
listagem pública documentada, limitada a 20 páginas, 100 itens/página e 365
dias; a comparação de sequential ID é local. Múltiplas correspondências
produzem `AMBIGUOUS`; rotas não documentadas não são inferidas.

### 9.3 Payload e arquivos

O pedido é normalizado em request/assets. Imagens vêm de
`images_download_links`; relatórios podem conter
`reports[].associated_images_download_links`. Formas única, lista, objeto e
coleções aninhadas são aceitas. `reports[].link` só vira arquivo após HTTP 200,
conteúdo não HTML e validação. Download é streaming, limitado, validado por
MIME/magic bytes e deduplicado por SHA-256. URLs assinadas não são persistidas.

Teleradiografias são classificadas por coleção/metadados e entram no mesmo
normalizador. O mapeamento completo de todos os nomes vistos em produção é
**PENDENTE DE DOCUMENTAÇÃO**.

### 9.4 Digital Models

O payload expõe modelos e `stl_files` com IDs. Links podem ser resolvidos pela
API/sessão; quando a página exige interação, o browser observa requisições para
Google Storage. ZIP é validado contra HTML, magic bytes, traversal, tamanho,
quantidade e conteúdo STL. Associação individual usa IDs/filename quando
inequívoca; coleção conserva `digital_model_id` e IDs esperados. Inferência por
ordem não deve ser usada sem evidência explícita.

### 9.5 Publicação e persistência

Ativos validados compõem `ClinicalPackage`, entram no manifesto e são
publicados no OneDrive pelo mesmo contrato idempotente. `cfaz_import_history`
registra o provider/request/estado; `clinical_assets` projeta cada ativo; o
índice pode ser reconstruído. Fluxos suplementares de modelos atualizam
manifesto remoto/local e índice somente em `--apply`. O fluxo completo de
modelos permanece em homologação.

## 10. Descobertas Técnicas

1. `download_url`/URL assinada é credencial efêmera, não identidade persistente.
2. Alguns downloads Cfaz apontam para Google Storage e aparecem apenas durante a sessão/interação browser.
3. Resposta HTTP 200 pode ser HTML; magic bytes e conteúdo precisam validar arquivo real.
4. Modelo digital pode chegar como ZIP contendo STL, não como STL direto.
5. Dois STL precisam ser distintos por conteúdo; nome/ordem sozinhos não provam maxila/mandíbula.
6. IDs de request, sequential e clínica não são intercambiáveis.
7. Thumbnail não é sinônimo de ativo descartável: preview clínico de tomografia pode ser preservado.
8. `provider_collection` e `provider_section` mantêm contexto perdido por uma classificação única.
9. Reindexar manifesto é idempotente por `exam_id`; estudos/séries/assets são substituídos dentro da transação.
10. Falha do índice depois de publicação COMPLETE deve ser recuperada por rebuild, não por republicação cega.
11. Um nome de paciente pode corresponder a mais de uma `patient_key` histórica; consultas locais atuais tratam chaves equivalentes.
12. Não existem manifests clínicos reais versionados neste repositório; contrato e testes são a evidência disponível.

## 11. Bugs Resolvidos

### BUG-R001 — Cache Microsoft Graph versionado

- **Problema/causa raiz:** arquivo local não estava ignorado no primeiro commit Graph.
- **Descoberta:** diff Git `53d1a93`.
- **Correção/Sprint:** remoção em `ad9c6b5`, ignore em `9e9ede1`.
- **Arquivos:** `.ms_graph_token_cache.json`, `.gitignore`.

### BUG-R002 — Credenciais Gmail locais versionadas

- **Problema/causa raiz:** arquivos específicos de máquina foram incluídos com a entrega 16.1.
- **Descoberta:** histórico Git.
- **Correção/Sprint:** remoção/ignore em `def69a6`, após Sprint 16.1.
- **Arquivos:** `.gitignore` e quatro arquivos locais removidos.

### BUG-R003 — Reimportação silenciosa

- **Problema/causa raiz:** idempotência inicial era apenas em memória.
- **Descoberta:** evolução pós-MVP.
- **Correção/Sprint:** SQLite, SHA/fingerprints, bloqueio, `--allow-reimport` e `REIMPORTAR`.
- **Arquivos:** `intake_history.py`, `supervised_import.py`, `main.py`, testes.

### BUG-R004 — Associação ambígua automática

- **Problema/causa raiz:** similaridade isolada não garante unicidade.
- **Descoberta:** contratos de matching/segurança.
- **Correção/Sprint:** limiar, margem, allowlist, unicidade e revisão humana.
- **Arquivos:** resolver/matcher/importer e testes.

### BUG-R005 — Traversal/sobrescrita

- **Problema/causa raiz:** pacotes externos e destinos existentes são não confiáveis.
- **Descoberta:** threat model do intake.
- **Correção/Sprint:** validação de paths, limites e criação exclusiva no MVP.
- **Arquivos:** extractors, storage/publisher e testes.

### BUG-R006 — Histórico 16.2 e 17.1–17.8 sem contexto

- **Problema/causa raiz:** Sprints foram desenvolvidas sem commits individuais e o contexto permaneceu no histórico do chat.
- **Descoberta:** auditoria inicial do Git não encontrou evidência suficiente.
- **Correção:** extração integral do arquivo Pages histórico e confronto com código, testes e commits; criação/atualização de `docs/history/sprint-*.md`.
- **Resultado:** objetivos, decisões, resultados de testes, descobertas e bugs foram recuperados sem reescrever Git.

## 12. Bugs Abertos

### BUG-002 — Modelos digitais Cfaz ainda não homologados ponta a ponta

- **Descrição:** implementação e testes estão versionados em `18540ea`/`v0.17.8`, mas o fluxo real completo de dois STL não possui homologação conclusiva registrada.
- **Causa:** integração depende de sessão browser, links efêmeros, associação de STL e destino remoto.
- **Impacto:** modelos podem não integrar o pacote publicado de forma operacionalmente garantida.
- **Status:** lógica local coberta por teste integrado com STL sintético;
  homologação real supervisionada Cfaz/OneDrive permanece pendente.
- **Prioridade:** alta.
- **Como reproduzir:** executar diagnóstico/dry-run para pedido elegível e seguir até download, extração, reconciliação OneDrive, indexação e segunda execução.
- **Hipótese:** destino remoto e associação individual ainda não possuem evidência suficiente em todos os caminhos.
- **Próximo passo/aceite:** teste real controlado, publicação idempotente, rollback/rebuild, dois STL distintos e documentação aprovada.

### BUG-006 — Reset/reimportação e segunda camada de idempotência

- **Descrição:** a primeira homologação do `cfaz-reset` deixou um intake `COMPLETED` associado ao destination fingerprint, bloqueando a reimportação.
- **Causa:** idempotência existia em `cfaz_import_history` e também em `radiology_imports`/destino.
- **Impacto:** pedido era baixado novamente, mas bloqueado antes da publicação.
- **Status:** lógica local coberta por teste integrado. A reconciliação agora
  reutiliza o remoto `COMPLETE` somente quando `exam_id`, caminhos, SHA-256,
  tamanhos e existência remota coincidem integralmente; homologação real
  supervisionada Cfaz/OneDrive permanece pendente.
- **Prioridade:** alta.
- **Como reproduzir:** pedido COMPLETE + intake COMPLETED + reset + nova importação.
- **Hipótese:** outros marcadores locais/remotos podem ainda exigir reconciliação.
- **Próximo passo/aceite:** provar reimportação completa sem bloqueio, sem segunda pasta e sem duplicação remota.

### BUG-003 — Documentação operacional divergente

- **Descrição:** README ainda descreve principalmente o MVP TransferNow/local, enquanto a arquitetura atual inclui Cfaz, Graph, DICOM, índice e consultas.
- **Causa:** documentação distribuída evoluiu em ritmos diferentes.
- **Impacto:** operador novo pode usar uma visão incompleta.
- **Status:** aberto.
- **Prioridade:** média.
- **Como reproduzir:** comparar o fluxo do README com Cfaz/Graph/índice em `ARCHITECTURE.md` e no código atual.
- **Hipótese:** documentos foram atualizados por entrega, sem gate central.
- **Próximo passo/aceite:** alinhar README/guia ao documento mestre sem duplicar detalhes voláteis.

### BUG-004 — CLI monolítica

- **Descrição:** `src/main.py` concentra parsing, composição e várias regras operacionais.
- **Causa:** crescimento incremental.
- **Impacto:** maior risco de regressão, imports tardios inconsistentes e documentação difícil.
- **Status:** aberto como dívida técnica.
- **Prioridade:** média.
- **Como reproduzir:** inspecionar `src/main.py`, que contém composição e parsing das 31 entradas.
- **Hipótese:** crescimento incremental concentrou comportamentos no entry point.
- **Próximo passo/aceite:** subcomandos modulares preservando contratos e testes.

### BUG-005 — Versionamento do índice insuficientemente explícito

- **Descrição:** `INDEX_VERSION=1` convive com tabelas e colunas adicionadas por migração aditiva.
- **Causa:** evolução compatível sem incremento formal.
- **Impacto:** rebuild/migração pode não expressar toda alteração de contrato.
- **Status:** aberto.
- **Prioridade:** alta.
- **Como reproduzir:** comparar `INDEX_VERSION=1` com criação/migração de `clinical_assets`, `digital_model_id` e `stl_file_id`.
- **Hipótese:** compatibilidade aditiva foi priorizada sem incremento de versão.
- **Próximo passo/aceite:** política de versão e teste de migração/rebuild documentados.

### Decisões arquiteturais preservadas (ADR)

### ADR-001 — Markdown versionado como fonte única de verdade

- **Decisão:** usar `docs/ENGINEERING_MASTER.md`.
- **Motivo:** revisão em Git, diffs legíveis e independência de chat.
- **Consequência:** toda Sprint deve atualizar o arquivo.

### ADR-002 — Clinicorp como fonte mestre de identidade

- **Decisão:** arquivos/provedores não decidem sozinhos a identidade.
- **Consequência:** indisponibilidade ou ambiguidade gera revisão.

### ADR-003 — Manifesto/OneDrive como fonte publicada; SQLite como projeção

- **Decisão:** SQLite pode ser reconstruído dos manifestos.
- **Consequência:** falha de indexação não apaga nem invalida publicação completa.

### ADR-004 — Aquisição por providers

- **Decisão:** TransferNow e Cfaz implementam contrato comum e não conhecem destino/CLI.
- **Consequência:** novos provedores devem entrar pela mesma abstração.

### ADR-005 — Sem interpretação clínica

- **Decisão:** DICOM é lido estruturalmente e Future AI permanece fora do pipeline.
- **Consequência:** qualquer IA futura exige ADR próprio e governança formal.

### ADR-006 — Segurança fail-closed

- **Decisão:** dúvida não é autorização.
- **Consequência:** automação produz `REVIEW_REQUIRED`, não uma associação provável silenciosa.

### ADR-007 — URLs assinadas e segredos somente efêmeros

- **Decisão:** não persistir tokens/URLs em estado, banco, manifesto ou log.
- **Consequência:** retomada usa IDs estruturais e checksums.

### Notas operacionais complementares da CLI

Famílias confirmadas no código:

- diagnóstico: `browser-self-test`, testes/diagnósticos Microsoft Graph e TransferNow por scripts;
- intake: `radiology-gmail-dry-run`, `radiology-import-supervised`, `radiology-import-from-gmail`, `radiology-auto-run`;
- histórico/revisão: `intake-history`, `intake-review-list`, `intake-review-resume`, `radiology-auto-status`;
- índice/consulta: `rebuild-radiology-index`, busca de exames/assets, resumo de paciente, timeline, comparação e dashboard;
- Cfaz: lista/seleção/lote/histórico/reparo/reprocessamento/reset e fluxos de modelos digitais;
- suporte: `thumbnail-report`.

Exemplos estáveis:

```powershell
ireo-clinical-intelligence browser-self-test
ireo-clinical-intelligence radiology-gmail-dry-run
ireo-clinical-intelligence radiology-import-supervised --archive-path "C:\caminho\exame.rar" --patient-source clinicorp
ireo-clinical-intelligence intake-history --limit 20
ireo-clinical-intelligence intake-review-list
ireo-clinical-intelligence rebuild-radiology-index
```

Antes de publicar uma referência completa dos comandos Cfaz/modelos, consolidar as alterações locais e executar `--help`/testes no estado aprovado.

### Comandos históricos de validação

Estes comandos aparecem repetidamente nos checkpoints do livro histórico e
formam o gate mínimo antes de concluir uma Sprint:

```bash
python -m compileall -q src tests
PYTHONPATH=src:. ./.venv/bin/pytest -q
python -m pip check
git diff --check
git status --short
```

Para operações Cfaz, usar sempre identificadores substituíveis e executar
dry-run antes de apply:

```bash
python -m main thumbnail-report
python -m main cfaz-reprocess --request-id REQUEST_ID --dry-run
python -m main cfaz-digital-models --request-id REQUEST_ID --dry-run
python -m main cfaz-reset --request-id REQUEST_ID --dry-run
```

`System Doctor`/Sprint 16.2A apareceu como proposta no livro histórico, mas não
há comando implementado com esse nome; não deve ser apresentado como
funcionalidade existente.

### Testes e critérios globais de aceite

### Estado da suíte

- 428 funções de teste foram encontradas em 2026-07-26.
- A suíte cobre parsing, matching, contratos Clinicorp, Gmail, TransferNow, arquivos, DICOM, Graph, publicação, histórico, índice, Cfaz e modelos digitais.
- Testes offline/mocados não substituem homologação real de Gmail, Clinicorp, Cfaz, TransferNow e OneDrive.
- O resultado de execução da suíte no estado local deve ser registrado ao fechar a próxima Sprint.

### Critérios globais

Uma mudança no pipeline só é aceita quando:

1. testes unitários/contratuais relevantes passam;
2. não persiste PHI ou segredo em logs/estado indevido;
3. ambiguidade continua fail-closed;
4. reexecução é idempotente ou exige confirmação explícita;
5. falha parcial é retomável ou deixa evidência clara;
6. manifesto e schema permanecem compatíveis ou possuem migração;
7. documentação operacional e este arquivo são atualizados;
8. bugs novos recebem ID;
9. existe rollback/rebuild quando houver escrita remota ou derivada;
10. a “Próxima tarefa única” é substituída, não acumulada.

### Dívida técnica

| ID | Item | Prioridade |
|---|---|---|
| DT-001 | Modularizar `src/main.py` por família de subcomandos | média |
| DT-002 | Formalizar migrações e versões dos três conjuntos SQLite | alta |
| DT-003 | Consolidar modelos `ClinicalAsset`/`ClinicalPackage` duplicados em módulos distintos | média |
| DT-004 | Criar teste de recuperação/backup com WAL/SHM | média |
| DT-005 | Alinhar README, guia e arquitetura ao documento mestre | alta |
| DT-006 | Criar validação automatizada que exige atualização deste arquivo por Sprint | média |
| DT-007 | Definir retenção de histórico e quarentena | alta |
| DT-008 | Registrar inventário completo de comandos e flags após aprovação da linha 17.x | alta |
| DT-009 | Reescrever o histórico Git antigo para remover credenciais e dados identificáveis antes de colaboração, publicidade, release/distribuição, nuvem/multiusuário ou novas integrações externas | bloqueante antes desses gatilhos |

## 13. Roadmap

### Sequência obrigatória

1. **Concluir aquisição automatizada de exames de imagem.** Preservar o que já
   foi construído e fechar somente os riscos essenciais do fluxo utilizado.
2. **Validar benefício operacional e clínico no IREO.** Registrar evidência de
   uso, redução de trabalho manual, falhas observadas e benefício percebido.
3. **Iniciar Patient Recall Engine.** Entregar o menor fluxo acionável e
   mensurável para a equipe interna.
4. **Expandir robustez somente por risco ou valor demonstrado.** Retomar itens
   adiados apenas quando a condição registrada no gate pragmático ocorrer.

Backups, retenção, modularização, múltiplos exames, upload alternativo,
concorrência, novos painéis e outras ampliações permanecem no backlog. Cada
item precisa ser reavaliado pelo gate da seção 1.5 antes de entrar no escopo.

Future AI não recebe número de Sprint antes das três primeiras etapas e de
aprovação própria de governança clínica, privacidade, segurança e finalidade.

### Glossário

- **Acquisition:** camada que descobre e obtém ativos de um provider.
- **Asset clínico:** arquivo normalizado e classificado, com identidade/checksum.
- **ClinicalPackage:** contrato agregado dos ativos prontos para publicação.
- **Clinicorp:** fonte mestre de identidade de pacientes.
- **Cfaz:** provider de pedidos e ativos clínicos.
- **ExamID:** identidade estável da publicação/exame.
- **Fail-closed:** comportamento que bloqueia ação quando não há certeza suficiente.
- **Fingerprint:** SHA-256 usado para comparar dado sem persistir seu valor puro.
- **Manifesto:** registro JSON da aquisição, normalização, publicação e inteligência estrutural.
- **Normalizer:** componente que classifica e padroniza arquivos.
- **PHI:** informação de saúde identificável.
- **Provider:** adaptador de uma origem de aquisição.
- **Publisher:** componente que grava o pacote validado no destino.
- **Quarentena:** área local isolada para download e validação.
- **Rebuild:** reconstrução do índice SQLite a partir dos manifestos.
- **Staging:** área de preparação do pacote/manifesto antes ou durante publicação.

## 14. Próxima Tarefa Única

**Entrega vertical atual — concluir a aquisição automatizada de exames de
imagem e validar seu benefício no IREO.**

O menor escopo utilizável deve fechar os riscos essenciais do fluxo real, sem
acrescentar robustez para escala hipotética ou produto externo. A conclusão
técnica não basta: devem ser registrados benefício percebido, trabalho manual
reduzido e falhas relevantes observadas na operação inicial.

Somente depois dessa conclusão e avaliação começa o **Patient Recall Engine**,
como próxima entrega vertical. Não iniciar outra expansão radiológica, nova
integração, IA ou complexidade arquitetural sem passar pelo gate pragmático da
seção 1.5.

## 15. Convenções

### 15.1 Código e organização

- Python 3.11 ou superior, type hints e dataclasses para contratos de domínio.
- Componentes externos ficam em `integrations`; abstrações de origem em
  `acquisition`; regras de aplicação em `services`/`workflows`; persistência e
  publicação em `radiology`/`storage`.
- Provider não conhece CLI, OneDrive, SQLite ou dashboard.
- Imports tardios em `main.py` evitam carregar integrações não selecionadas,
  embora a modularização da CLI permaneça dívida.
- Testes espelham o módulo: `src/x.py` → `tests/test_x.py`.
- Mensagens de erro operacionais devem ser sanitizadas; exceção original pode
  ser encadeada internamente sem imprimir segredo.

### 15.2 Nomeação e contratos

- Classes e tipos: `PascalCase`; funções, módulos e campos: `snake_case`;
  constantes: `UPPER_SNAKE_CASE`.
- Estados persistidos usam maiúsculas (`COMPLETE`, `FAILED`,
  `REVIEW_REQUIRED`); categorias clínicas usam enum/string em maiúsculas.
- IDs de provider permanecem separados por significado. Não converter
  `provider_request_id`, `sequential_id` e `clinic_number` em um ID genérico.
- Timestamps persistidos usam UTC ISO-8601; datas clínicas preservam sua fonte.
- Nomes de arquivo publicados são normalizados e determinísticos.

### 15.3 Versionamento e compatibilidade

- Sprints aprovadas exigem commit, testes e atualização deste documento.
- Tags existentes não são uma sequência semântica coerente (`v1.0.0`,
  `v0.15.0`, `v0.16.1`, `v0.16.3`); a política futura é **PENDENTE DE
  DOCUMENTAÇÃO**.
- Schemas evoluem aditivamente; remoção/rename exige migração explícita.
- Manifestos antigos devem continuar legíveis; aliases só podem ser removidos
  após rebuild/migração comprovados.

### 15.4 Idempotência e rollback

- SHA-256 é a identidade forte de conteúdo.
- Exam ID e IDs estruturais de provider controlam retomada.
- Reexecução não pode duplicar publicação silenciosamente.
- `--apply` deve ser explícito em manutenção destrutiva; dry-run é o padrão
  quando o comando o oferece.
- Mudança de manifesto/índice deve preservar original e restaurá-lo em falha.
- Publicação `COMPLETE` não é desfeita por falha posterior do índice.

### 15.5 Segurança e logs

- Nunca versionar `.env`, credenciais, tokens, cache OAuth, URL assinada ou
  dados reais de paciente.
- Gmail usa somente `gmail.readonly`.
- Downloads entram em quarentena e são limitados/verificados antes da extração.
- Não contornar CAPTCHA, login, proteção ou associação ambígua.
- Logs usam correlation ID, códigos, contagens, hashes e nomes mascarados.
- PHI só pode aparecer em artefato clínico/destino autorizado e em interfaces
  operacionais cuja finalidade exija isso.
- Debug não amplia o conjunto de campos sensíveis.

### 15.6 Decisões arquiteturais registradas

Este documento preserva sete ADRs: fonte única em Markdown; Clinicorp como
fonte mestre; OneDrive/manifesto como publicação e SQLite como projeção;
providers de aquisição; ausência de interpretação clínica; fail-closed; e
segredos/URLs somente efêmeros.

A proposta de dividir o handbook em documentos especializados foi avaliada.
Decisão atual: manter um único documento autocontido porque a instrução de
inicialização exige leitura integral de uma única fonte. Reavaliar a
modularização quando tamanho/tempo de carregamento impedir o uso integral; um
índice só poderá ser fonte oficial se definir de forma inequívoca quais
documentos normativos devem ser lidos.

## 16. Changelog

Histórico append-only deste documento. Entradas futuras são acrescentadas no
topo desta seção; entradas antigas nunca são removidas ou reescritas, salvo
correção factual acompanhada de nova entrada explicativa.

### 2026-07-27 — Correção consciente de direcionamento

- Adotado formalmente o princípio de MVP de alto valor clínico para o uso
  interno do IREO.
- Preservadas as garantias mínimas de segurança, integridade, LGPD,
  idempotência necessária, associação inequívoca e revisão humana.
- Estabelecido o gate pragmático obrigatório para novas tarefas.
- Fixada a ordem: concluir aquisição automatizada, validar valor no IREO,
  iniciar Patient Recall Engine e só então ampliar robustez por risco ou valor
  demonstrado.
- Esta entrada registra uma mudança de direção em 27 de julho de 2026 sem
  reescrever retroativamente o histórico anterior.

### 2026-07-26 — Sprint 18.1 local, ainda não versionada

- Criado um cenário integrado determinístico do ciclo Cfaz base com fronteiras
  externas simuladas e componentes internos reais.
- Comprovados aquisição e normalização de imagem e STL sintéticos,
  `ClinicalPackage`, resolução inequívoca de paciente, publicação stateful,
  manifesto, históricos, índice SQLite, reset dry-run/apply, preservação de
  pedido de controle e reimportação.
- Corrigida a reconciliação pós-reset: remoto `COMPLETE` só é reutilizado
  quando inventário, caminhos, checksums, tamanhos e existência coincidem.
- Comprovado que a terceira execução é idempotente e não acessa o provider.
- Teste integrado direcionado aprovado; suíte completa aprovada com 525 testes
  e 2 testes ignorados; `pip check` e `git diff --check` aprovados.
- BUG-002 e BUG-006 permanecem abertos para homologação real supervisionada.
- Fase 2B de reescrita histórica adiada e registrada como DT-009, bloqueante
  antes de colaboração, publicidade, distribuição, nuvem/multiusuário ou novas
  integrações externas.

### 2026-07-26 — Documento mestre inicial

- Criado `docs/ENGINEERING_MASTER.md`.
- Auditados histórico Git, tags, código, schemas/migrações, testes e
  documentação existente.
- Registradas inicialmente as lacunas 17.1–17.8, posteriormente recuperadas do livro histórico.
- Catalogados 31 comandos CLI nomeados e o modo Clinicorp sem subcomando.
- Registrados três conjuntos lógicos SQLite, contratos de manifesto,
  `ClinicalPackage`, Clinical Assets e fluxo Cfaz.
- Registrados cinco bugs abertos, cinco bugs resolvidos e sete ADRs.
- Definida uma única tarefa seguinte: consolidação/homologação dos modelos
  digitais Cfaz.

### 2026-07-26 — Recuperação do livro histórico

- Exportado e lido integralmente o arquivo Pages histórico (28.190 linhas).
- Recuperados os escopos reais das Sprints 16.2 e 17.1–17.8.
- Diferenciados planejamento, implementação, validação offline e homologação real.
- Registradas as descobertas Cfaz/Google Storage/STL e o bug da segunda camada de idempotência.
- BUG-001 documental movido para resolvido como BUG-R006.
- Próxima tarefa restringida à homologação ponta a ponta dos modelos digitais.
