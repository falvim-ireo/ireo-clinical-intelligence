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
    -> PatientMatcher against names supplied in memory
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

`PatientMatcher` compares the candidate name only with the list supplied by the
caller. It normalizes accents, letter case, repeated whitespace, punctuation,
and the DICOM `^` separator before calculating deterministic similarity scores.

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
