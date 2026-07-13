# IREO Clinical Intelligence Architecture

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
request. The workflow does not access Clinicorp, OneDrive, archives, DICOM, or
the filesystem. Ambiguous patient matching remains manual as defined in Phase
2.

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
