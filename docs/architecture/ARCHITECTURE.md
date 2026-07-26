# IREO Clinical Intelligence Architecture

> Esta visão deriva do
> [Documento Mestre de Engenharia](../ENGINEERING_MASTER.md), dos registros em
> `docs/history/`, do histórico Git e do livro histórico exportado do Pages.
> Em caso de divergência, o documento mestre é normativo.

## Sprints 16.2–17.8 — repositório de ativos clínicos

```text
Provider
  → Provider Metadata
  → ClinicalPackage
  → ClinicalAssetNormalizer
  → Publisher
  → OneDrive/manifest.json
  → clinical_assets (SQLite derivado)
  → Timeline / Search / Summary / Dashboard
```

### ClinicalPackage como fronteira

A Sprint 16.2 estabeleceu `ClinicalPackage`/`ClinicalAsset` como contrato entre
aquisição e processamento. Coleção, seção, nome exibido, categoria e metadados
do provider são preservados; módulos posteriores não reinterpretam o payload
bruto do Cfaz. A Sprint 16.3 materializou cada ativo na tabela
`clinical_assets`, sem mudar a fonte clínica: OneDrive e manifestos continuam
duráveis, SQLite continua reconstruível.

### Camada clínica somente leitura

As Sprints 17.1–17.4 criaram timeline, busca, resumo e dashboard. Todas as
consultas usam exclusivamente SQLite. Elas não acessam Cfaz, TransferNow,
Gmail, Clinicorp, OneDrive ou IA e não reclassificam conteúdo durante a leitura.

### Seleção de ativos

A Sprint 17.5 comprovou que um caso de “duplicação” era, na realidade, um
conjunto de originais e thumbnails com SHA-256 distintos. A categoria e a
publicabilidade são decididas por asset: original e preview clínico autorizado
seguem; thumbnail genérico é ignorado. Rebuilds aplicam a mesma regra sem
excluir conteúdo histórico do OneDrive.

### Modelos digitais Cfaz

A descoberta 17.6 identificou no frontend Cfaz
`digital_models[*].stl_files[*]`, com `id`, `download_url` e
`document_file_name`, além de downloads efêmeros pelo Google Storage. A Sprint
17.7 converte esses itens em ativos `DIGITAL_MODEL`, preserva IDs do modelo/STL
e mantém URL/query/token somente em memória. Rotas de escrita como
`PUT /digital_models/{id}.js` são proibidas.

### Reset e reimportação

A Sprint 17.8 introduziu reset local restrito ao pedido. Ele preserva OneDrive
e auditoria Cfaz, remove/invalida projeções e marcadores locais e prepara
`READY_FOR_REIMPORT`. A homologação revelou que a idempotência é multicamada:
`cfaz_import_history` e `radiology_imports`/destination fingerprint precisam
ser tratados conjuntamente. A homologação completa de dois STL continua sendo
a única tarefa operacional seguinte.

## Sprint 15 — aquisição multiprovedor

`acquisition.base.AcquisitionProvider` separa autenticação, descoberta,
download, classificação e finalização da cadeia clínica. Providers produzem um
`AcquiredPackage` dentro da quarentena; a partir daí o mesmo
`SupervisedRadiologyImporter` executa Clinicorp, inventário DICOM, manifesto,
OneDrive e índice. `TransferNowProvider` adapta o conector/downloader existente
sem mudar seu comando operacional.

`CfazProvider` usa prioritariamente a API oficial `max.cfaz.net/api/v1`.
Notificações Gmail servem apenas para extrair um único Request ID; os dados e
arquivos são enumerados diretamente no pedido autenticado. Token ou credenciais
de sessão existem somente em memória e headers HTTP. O manifesto recebe uma
seção aditiva `acquisition` com provider/request, datas, origem canônica sem
token, clínica, profissional e classificações. A identidade publicada é o
SHA-256 de provider + request + Exam ID do provider + SHA-256 determinístico do
pacote.

### Sprint 15.1 — interface operacional Cfaz

`CfazNotificationCatalog` pagina a consulta isolada por `CFAZ_GMAIL_QUERY`,
extrai Request ID e paciente e cruza os pedidos exclusivamente com
`cfaz_import_history`. O limite maior dessa consulta não altera o limite
de segurança do piloto TransferNow. O histórico fica no mesmo SQLite do índice
radiológico, mas em tabela fisicamente independente das projeções do acervo, e
registra Request ID, Exam ID do provider, SHA-256 da aquisição, estado, instante
da importação, duração e destino; o Message ID do Gmail é persistido somente
como SHA-256. Não existe inferência a partir de `exams`, timeline, dashboard ou
OneDrive, e nenhum dado da antiga tabela genérica é migrado implicitamente.

Identificadores Cfaz são mantidos separadamente: `provider_request_id` é a
chave interna da API, `sequential_id` é o número visível do pedido e
`clinic_number` é o número operacional da clínica. `--request-id` aceita os
dois primeiros. Um 404 no detalhe ativa somente a listagem pública documentada,
limitada a 20 páginas, 100 itens por página e janela de 365 dias; a comparação
de `sequential_id` ocorre localmente. Nenhuma rota não documentada é inferida.
Correspondências múltiplas geram histórico `AMBIGUOUS` e exigem revisão.

Os comandos de lista, seleção, Request ID, lote pendente e histórico usam esse
catálogo somente nos modos de descoberta, seleção e lote. O caminho explícito
`--request-id` consulta apenas `cfaz_import_history` e chama diretamente
`CfazProvider.discover_request()`, sem construir o conector Gmail. O Message ID
permanece um detalhe interno dos modos baseados em notificações, sem aparecer
na interface operacional.

Cada item é baixado primeiro para a quarentena, validado por tamanho e SHA-256
e consolidado em ZIP determinístico. Estado local sem URLs permite retomar
itens completos. Pedidos com múltiplos artefatos usam `Documentação
Radiológica`; nenhum provider conhece OneDrive, Clinicorp, SQLite ou dashboard.

O normalizador Cfaz mapeia explicitamente `images_download_links` do pedido e
`reports[].associated_images_download_links`, aceitando URL única, listas,
objetos e coleções aninhadas. `reports[].link` é apenas sondado: somente HTTP
200 com conteúdo de arquivo não HTML é aceito. Downloads são streaming,
limitados por tamanho, refinados por MIME/magic bytes e deduplicados pelo
SHA-256 do conteúdo. URLs assinadas existem apenas em memória; estado de
retomada e identidade dos assets usam a posição estrutural, nunca a URL.

## Sprint 14 — índice radiológico inteligente

`radiology.exam_index_service.ExamIndexService` mantém um SQLite local
versionado e inteiramente derivado dos manifestos. O banco não substitui o
OneDrive nem replica o JSON completo: projeta somente campos necessários para
consulta em `patients`, `exams`, `studies`, `series`, `import_history` e
`consistency_issues`. A chave estável é `publication.exam_id`; reindexar o
mesmo exame atualiza suas projeções e não duplica estudos ou séries.

Após a publicação chegar a `COMPLETE`, o pipeline indexa o manifesto local. Se
o índice estiver indisponível, o exame remoto permanece válido e uma execução
posterior do rebuild recupera a projeção. Consultas por paciente, data,
modalidade, fabricante, Study/Series UID e ExamID, linha do tempo, comparação
estrutural e dashboard usam exclusivamente o SQLite e não fazem interpretação
clínica.

`python -m main rebuild-radiology-index` percorre somente a hierarquia de
pastas e lê `manifest.json` (e `dicom_summary.json` quando a inteligência não
está incorporada). Não baixa DICOMs, não executa upload e não move, renomeia ou
exclui itens. Checkpoints `PENDING`/`COMPLETE`/`FAILED` permitem retomada; a
versão gravada por exame força reprocessamento quando o modelo do índice
evolui. `--full` limpa apenas as projeções locais derivadas, e `--patient`
limita o reprocessamento a um paciente.

### Sprint 14.1 — observabilidade e inventário eficiente

O rebuild materializa primeiro um inventário temporário da árvore relevante e
mantém em memória o resultado de cada chamada `children`. Itens já enumerados
são convertidos diretamente em `GraphFolder`; não se executa uma nova busca
por nome para cada filho. Assim, cada pasta de paciente e cada pasta
`Radiologia` é listada uma única vez por execução.

Chamadas Graph do rebuild têm timeout HTTP explícito, até cinco tentativas para
timeout, HTTP 429 e HTTP 5xx, além de heartbeat enquanto a resposta está
pendente. A saída informa conexão, raiz, número e posição dos pacientes,
exames, retries e duração de inventário/indexação. No modo `--full`, as
projeções locais só são limpas depois que o inventário remoto termina com
sucesso; uma falha inicial de rede não destrói o índice utilizável.

## Sprint 13 — inteligência estrutural DICOM

O fluxo supervisionado reutiliza `radiology.dicom_reader.DicomReader` como o
único leitor estrutural. `read()` mantém o contrato estrito consumido pelo
workflow radiológico, enquanto `analyze()` inventaria pacotes heterogêneos sem
carregar pixels (`stop_before_pixels=True`). A análise agrupa estudos e séries,
gera somente estimativas geométricas com fontes explícitas e não realiza
diagnóstico, segmentação ou interpretação clínica.

Após a confirmação do paciente Clinicorp, nomes e identificadores DICOM são
comparados sem reescrever os valores originais. Múltiplos pacientes bloqueiam
a publicação e exigem revisão. `dicom_summary.json` e `resumo_do_exame.txt` são
gerados no staging, incorporados de forma aditiva ao `manifest.json` e enviados
pelo mesmo publicador idempotente do OneDrive. Assim, os relatórios seguem o
mesmo controle de checksum, estados `IN_PROGRESS`/`COMPLETE`/`FAILED`, retomada
e proibição de sobrescrita não verificada dos demais arquivos do exame.

Nenhuma etapa DICOM move, renomeia, mescla ou exclui itens remotos. Logs do
leitor contêm apenas contagens e estados; dados identificadores permanecem nos
artefatos clínicos autorizados e não são enviados a serviços de IA.

A identidade temporal do exame é resolvida uma única vez e registrada no
manifesto. A prioridade é: `StudyDate` consistente, timestamp validado do nome
do pacote, metadado confiável do remetente, recebimento do e-mail e, apenas
como fallback explícito, início da importação. `exam_date` define o destino;
`exam_time` diferencia exames distintos no mesmo dia, seguido por modalidade
ou pelo identificador estável do exame. O `exam_id` sempre prevalece para
retomada, e sufixos automáticos `(2)`/`(3)` não são usados.

Falha de leitura não equivale por si só a corrupção DICOM. O inventário separa
DICOM com marcador reconhecível realmente inválido, arquivos não DICOM,
formatos não reconhecidos e extensões proprietárias. Isso evita transformar
conteúdo arbitrário de visualizadores em alertas falsos de corrupção.

## Fluxo oficial v1.0.0

```text
Gmail API (readonly)
    -> seleção supervisionada da mensagem
    -> parsing e priorização do link público TransferNow /dl/
    -> download HTTPS ou Playwright em contexto temporário
    -> checksum SHA-256 e quarentena
    -> extração segura ZIP/RAR
    -> Clinicorp (fonte mestre de identificação)
    -> confirmação humana do paciente e da pasta
    -> prévia de arquivos, tamanho e duplicidades
    -> confirmação explícita CONFIRMAR
    -> cópia exclusiva para pasta OneDrive local
    -> manifest.json
```

O Gmail é acessado somente com o escopo `gmail.readonly` e nenhuma mensagem é modificada ou marcada como processada. O link público `/dl/` do TransferNow é priorizado; quando a página exige JavaScript, Playwright abre Chromium visível em contexto temporário, sem perfil persistente e sem contornar proteções externas.

Todo arquivo baixado e extraído passa pela quarentena. ZIP e RAR têm caminhos validados contra traversal antes da extração. O Clinicorp é a fonte mestre para identificação; associação ausente ou ambígua nunca é resolvida automaticamente e exige revisão humana.

O comando `radiology-auto-run` é um orquestrador local agendável e fail-closed. Ele usa Gmail readonly, a mesma quarentena, o mesmo SQLite e a mesma cópia/manifesto do fluxo supervisionado. Só copia com flags explícitas, candidato Clinicorp único com score mínimo 0,98 e motivo `EXACT_NAME`/`NORMALIZED_NAME`, uma pasta compatível dentro da raiz e nenhuma duplicidade. As demais situações persistem `REVIEW_REQUIRED` com código e estágio sanitizados.

A pasta do paciente e a cópia final também exigem confirmação. A aplicação copia sem mover ou apagar a origem, usa criação exclusiva e não sobrescreve arquivos automaticamente. O OneDrive v1.0.0 é integrado por uma pasta sincronizada localmente; upload direto via Microsoft Graph é uma evolução futura.

### Auto-seleção supervisionada

A feature flag `IREO_AUTO_SELECT_UNAMBIGUOUS`, falsa por padrão, controla somente as seleções intermediárias. A decisão do paciente reutiliza o resultado estruturado do `PatientResolver`, mas aplica o limiar independente `IREO_AUTO_SELECT_MIN_SCORE` (padrão `0.95`) e uma allowlist fechada de motivos. Valores futuros desconhecidos falham para seleção manual.

A pasta só acompanha uma auto-seleção segura do paciente quando há exatamente um diretório compatível, resolvido dentro da raiz, coerente após normalização e diferente de `REVIEW_REQUIRED`. Symlinks e junctions que resolvem fora da raiz são descartados. A aplicação nunca cria pastas de pacientes.

As decisões emitem eventos sanitizados de auto-seleção ou revisão manual. O manifesto registra os modos e motivos. Nenhuma dessas decisões ignora a prévia completa ou a confirmação final `CONFIRMAR`.

### Idempotência persistente

`IntakeHistoryRepository` usa exclusivamente `sqlite3` da biblioteca padrão. O schema versionado (`schema_version = 1`) é criado de forma idempotente e nunca recria ou apaga automaticamente um banco existente.

```text
mensagem selecionada -> verificação do Gmail fingerprint
download + SHA-256 -> DOWNLOADED -> verificação forte do arquivo
extração -> EXTRACTED
paciente + destino -> verificação contextual -> READY_FOR_CONFIRMATION
CONFIRMAR -> cópia + manifesto -> COMPLETED
```

Falhas e cancelamentos são preservados como `FAILED` e `CANCELLED` e não bloqueiam retry. Duplicidades concluídas geram `DUPLICATE_DETECTED`; uma reimportação exige opção CLI, `REIMPORTAR` e depois a confirmação final independente. Conexões SQLite são curtas, usam transações explícitas, busy timeout e WAL para concorrência local simples.

O banco não contém nomes completos, IDs puros, URLs, tokens ou caminhos completos. Fingerprints SHA-256 representam Gmail, transferência, paciente, destino, manifesto e checksums. O arquivo compactado mantém seu SHA-256 integral como identificador técnico forte.

## Radiology Intake Phase 2

Phase 2 implements a dry-run planning flow. It receives data already loaded in
memory and returns a structured `RadiologyIntakePlan`. It does not connect to an
email provider, Clinicorp, TransferNow, OneDrive, or any other remote service.
It also does not download, extract, read, create, move, or delete files.

### Flow

```text
EmailMessage (in memory)
    -> ImagingWorkflow.run_dry_run
    -> RadiologyImportService.create_plan
    -> TransferNowConnector validation and parsing
    -> ImagingExam transient domain object
    -> PatientResolver through PatientRepository
    -> RadiologyIntakePlan
```

`EmailMessage` is provider-independent and contains only the message fields
needed by the planning flow. `TransferNowConnector` accepts only HTTPS links
whose hostname is exactly `transfernow.net` or one of its subdomains.

`RadiologyImportService` creates an `ImagingExam` transiently to represent the
proposed intake. The object is not persisted. The proposed destination is a
sanitized logical string and does not cause a filesystem or OneDrive operation.

The service keeps an in-memory plan cache keyed by `message_id`. Reprocessing
the same ID in the same service instance returns the original plan and does not
create a second logical intake. This cache is intentionally non-persistent and
will be replaced by durable idempotency in a later production phase.

### Patient Matching

`PatientResolver` compares the candidate name only with candidates returned by
the configured `PatientRepository`. It normalizes accents, letter case,
repeated whitespace, punctuation, and the DICOM `^` separator before calculating
deterministic similarity scores.

An automatic match requires a score of at least `0.90`. A second candidate at
or above that threshold and less than `0.10` behind the best score makes the
result ambiguous.

**An ambiguous patient association must never be automatic.** Ambiguous,
low-score, missing-name, and missing-patient cases receive status
`DRY_RUN_REVIEW_REQUIRED`, use the `REVIEW_REQUIRED` destination segment, and
include explicit review reasons. Only a strong and unique match receives status
`DRY_RUN_READY`.

### Out of Scope

The following capabilities are explicitly outside Phase 2:

- Gmail or other mailbox access
- Clinicorp queries
- TransferNow network requests or downloads
- archive extraction
- DICOM file reading
- OneDrive access
- filesystem operations
- production import execution

## Radiology Intake Phase 3A

Phase 3A adds a read-only Gmail adapter while preserving the Phase 2 dry-run
boundary. Gmail is the only external service accessed by this phase.

```text
Gmail API: messages.list + messages.get(format=full)
    -> GmailConnector
    -> MIME text/plain and text/html parsing
    -> EmailMessage (Gmail message ID)
    -> ImagingWorkflow.run_dry_run
    -> RadiologyIntakePlan
    -> restricted CLI output
```

The connector requests only the Gmail readonly OAuth scope. It does not implement calls
to send, modify, delete, trash, archive, label, or mark messages as read. The
pilot query is configured locally to identify TransferNow notifications, and
the connector enforces an absolute maximum of five messages per execution.

OAuth client credentials and user tokens are local secrets. Their default file
names are ignored by Git, they are never printed, and Gmail passwords are not
used or stored. The desktop OAuth flow writes only the local refresh/access
token required for later Gmail sessions.

After Gmail returns each full MIME message, all remaining processing is local.
Message bodies and headers are not sent to AI models or AI services.
The TransferNow connector only parses and validates text; it makes no HTTP
request. The default workflow does not access Clinicorp, OneDrive, archives,
DICOM, or the filesystem. Clinicorp is only composed when the operator uses
`--patient-source clinicorp`. Ambiguous patient matching remains manual as
defined in Phase 2.

CLI output is restricted to the masked Gmail message ID, archive name, probable
patient, matching status, manual-review flag, and logical destination. Message
bodies and TransferNow URLs are never emitted.

## Patient Resolution (Issues #31 and #32)

Patient identity resolution is isolated behind the following flow:

```text
TransferNowConnector
    -> ImagingExam
    -> PatientNormalizer
    -> PatientResolver
    -> PatientRepository.find_candidates(name)
    -> deterministic normalization and scoring
    -> ResolvedPatient
    -> RadiologyIntakePlan
```

### PatientNormalizer

`PatientNormalizer` is the single reusable text-normalization component. It is
stateless, deterministic, and has no matching, scoring, repository, network, or
domain rules. It removes accents, uppercases text, converts the DICOM `^`
separator to spaces, removes punctuation and special characters, and collapses
whitespace. `PatientResolver` and the legacy `PatientMatcher` consume its
canonical comparison representation instead of implementing normalization.

`PatientResolver` contains no Clinicorp, Gmail, OneDrive, TransferNow, DICOM
file, or AI integration code. A repository is a port that only returns `Patient`
candidates. `InMemoryPatientRepository` is used exclusively by automated tests.
Until a real repository is approved, the workflow defaults to an
`EmptyPatientRepository` and therefore requires manual review. A future
`ClinicorpPatientRepository` can implement the same port without changing the
resolver.

`ResolvedPatient` records the selected identity, confidence, candidate names,
candidate count, matching method, and a `ResolutionReason`. Scores at or above
`0.90` are eligible. More than one eligible candidate always produces
`MULTIPLE_HIGH_SCORE`, clears the selected patient, and requires human review.
No ambiguity can be resolved automatically, even when one candidate is an exact
literal match.

The official workflow now creates `ImagingExam`, invokes `PatientResolver`, and
uses `ResolvedPatient` to build the dry-run plan. The former `PatientMatcher`
module remains temporarily available for backward compatibility but is no
longer used by `RadiologyImportService` or `ImagingWorkflow`.

## Clinicorp Patient Repository (Issue #33)

The Clinicorp integration is an adapter behind the repository port:

```text
ImagingExam
    -> PatientNormalizer
    -> PatientResolver (matching and score)
    -> PatientRepository (port)
    -> ClinicorpPatientRepository (adapter)
    -> ClinicorpAPI (Basic Auth HTTP client)
```

`ClinicorpPatientRepository` receives `ClinicorpAPI` through its constructor.
It removes only external whitespace from the supplied name and performs
exactly one active-patient query while preserving accents, letter case,
internal whitespace, and word order. After Clinicorp returns candidates,
`PatientNormalizer` supports their internal validation and comparison; it
never rewrites the external query parameter at this stage. The adapter maps
valid external records to domain `Patient` objects, removes duplicate
`PatientId` values, and preserves response order. It does not score or select
patients. `PatientResolver` depends only on the repository port and has no
knowledge of Clinicorp or HTTP.

The current Clinicorp endpoint works best with an exact full name. The adapter
makes no automatic variations and does not search by CPF, phone, birth date, or
aliases. Broader search strategies are intentionally deferred.

Timeouts, HTTP errors, service unavailability, and unusable response structures
become `PatientRepositoryUnavailableError` with a sanitized message. The
resolver converts that condition to `PATIENT_SOURCE_UNAVAILABLE`, and the plan
is always marked `DRY_RUN_REVIEW_REQUIRED`. Source failure can never produce an
automatic patient association.

Application composition remains safe by default. `radiology-gmail-dry-run`
uses `EmptyPatientRepository`; the Clinicorp adapter is instantiated only for
`radiology-gmail-dry-run --patient-source clinicorp`. Automated tests inject
fakes and never construct the real HTTP path.

## Sanitized Observability (Issue #34)

The Gmail dry-run, `RadiologyImportService`, `PatientResolver`,
`ClinicorpPatientRepository`, and `ImagingWorkflow` share one injected
`AuditLogger` per composed execution:

```text
Gmail dry-run
    -> masked operational AuditEvent
    -> RadiologyImportService
    -> PatientResolver / ClinicorpPatientRepository
    -> ImagingWorkflow result
    -> masked operational AuditEvent
```

The logger uses a strict field allowlist. Raw e-mail bodies, full TransferNow
URLs, tokens, credentials, patient contacts, complete patient names, external
response bodies, and raw identifiers never enter the event model. Message and
patient IDs are represented by deterministic SHA-256 fingerprints, archive
names by a fingerprint plus safe extension, and URLs by hostname only. A
random `correlation_id` links the events from one execution without becoming a
clinical identifier.

Audit calls are failure-isolated and cannot alter clinical matching, manual
review, or dry-run results. `WARNING` is the default level, `INFO` enables the
pilot event sequence, and `DEBUG` remains equally sanitized. The detailed
event catalog, LGPD constraints, and pending retention decisions are documented
in [`OBSERVABILITY.md`](OBSERVABILITY.md).

Contract fixtures under `tests/fixtures/clinicorp/` are explicitly synthetic.
They exercise active/deleted records, ambiguity, duplicate IDs, empty and
invalid responses, missing fields, optional contact and birth-date fields, and
mixed case with accents. Tests override the Clinicorp transport and block
socket and HTTP entry points, so contract validation cannot reach real
services.

## Supervised Radiology Import MVP (Issue #35)

The supervised command is an explicit foreground workflow and does not replace
the safe dry-run:

```text
Gmail message ID (readonly) OR local .zip/.rar
    -> quarantine download/extraction
    -> Clinicorp candidates or offline manual name confirmation
    -> human patient selection
    -> normalized local patient-folder search
    -> human folder selection
    -> complete copy preview
    -> exact CONFIRMAR input
    -> copy-only destination + manifest.json
```

ZIP extraction validates every member before writing and rejects absolute
paths, parent traversal, Windows drive paths, alternate streams, and symbolic
links. RAR extraction invokes only the configured UnRAR console executable,
first with `l` for path validation and then with `x -o-`, using separate
arguments, `shell=False`, exit-code validation, and a configurable timeout. Both
formats extract only below the configured quarantine. Archives and extracted
folders are retained.

Patient folders are searched as immediate child directories of the configured
local OneDrive root. `PatientNormalizer` is used only to compare folder names;
the operator must select a result even when there is only one. The workflow
does not use Microsoft Graph and never creates a patient folder.

The dated destination is created only after the exact confirmation text. A
pre-existing destination receives an incremental suffix. Files are opened in
exclusive-create mode, are copied rather than moved, and are never overwritten
or deleted. The manifest records the execution correlation ID, UTC timestamp,
original archive name, masked PatientId, counts, total size, SHA-256 checksums,
source mode, destination, and completion status.
