# IREO Clinical Intelligence Architecture

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

A pasta do paciente e a cópia final também exigem confirmação. A aplicação copia sem mover ou apagar a origem, usa criação exclusiva e não sobrescreve arquivos automaticamente. O OneDrive v1.0.0 é integrado por uma pasta sincronizada localmente; upload direto via Microsoft Graph é uma evolução futura.

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

The connector requests only the OAuth scope
`https://www.googleapis.com/auth/gmail.readonly`. It does not implement calls
to send, modify, delete, trash, archive, label, or mark messages as read. The
pilot query defaults to `from:noreply@transfernow.net subject:TransferNow`, and
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
