# Sanitized Radiology Intake Observability

## Scope

Radiology Intake emits structured operational audit events during Gmail
dry-run processing. The events support pilot correlation and operational
monitoring only. They are not a clinical record, do not provide diagnostic
evidence, and must not be used for diagnosis, treatment, or patient matching
decisions.

Every event is serialized as one JSON object. Logging failures are isolated
from the workflow: a handler, formatter, or injected logger failure cannot
change the dry-run result or interrupt patient review safeguards.

## Events

| Event | Meaning |
| --- | --- |
| `RADIOLOGY_EMAIL_DETECTED` | A candidate Gmail message entered the local dry-run. |
| `TRANSFERNOW_MESSAGE_PARSED` | The notification was parsed without downloading its content. |
| `PATIENT_RESOLUTION_STARTED` | Candidate evaluation started. |
| `PATIENT_RESOLUTION_MATCHED` | One candidate satisfied the existing clinical rules. |
| `PATIENT_RESOLUTION_REVIEW_REQUIRED` | The existing rules require human review. |
| `PATIENT_SOURCE_UNAVAILABLE` | The configured patient source could not provide a safe result. |
| `RADIOLOGY_DRY_RUN_COMPLETED` | A dry-run plan was produced. |
| `RADIOLOGY_DRY_RUN_FAILED` | Planning failed before a plan could be produced. |

The payload contains UTC timestamp, per-execution `correlation_id`, operational
status, masked identifiers, manual-review flag, reason code, and metadata from
a strict allowlist. Unknown metadata keys are discarded.

## Prohibited Data

Audit logs must never contain:

- Gmail body, HTML, or MIME content;
- complete TransferNow URLs, paths, queries, or download tokens;
- OAuth tokens, Clinicorp tokens, API users, credentials, Business IDs, or
  Subscriber IDs;
- patient or sender phone numbers and e-mail addresses;
- complete patient names;
- raw message IDs, raw patient IDs, or unmasked archive names;
- HTTP response bodies or raw external exception text.

These restrictions also apply at `DEBUG` level. The implementation accepts
only known metadata fields instead of trying to redact arbitrary text after it
has already entered a log record.

## Masking Policy

- Message and patient identifiers use deterministic, truncated SHA-256
  fingerprints with a type prefix.
- Archive names use a deterministic fingerprint and retain only a short safe
  extension such as `.zip`.
- TransferNow references retain only a syntactically safe lowercase hostname;
  path, query, fragment, port, and credentials are discarded.
- A random non-clinical `correlation_id` is generated once for each composed
  execution and shared by its components.

No salt, key, or secret is stored in source code. These fingerprints exist only
for short-term operational correlation. They are not a definitive
pseudonymization mechanism and must not be treated as anonymous clinical data.

## Levels And Pilot Operation

`IREO_AUDIT_LOG_LEVEL` accepts `WARNING`, `INFO`, or `DEBUG`:

- `WARNING` is the default and records unavailable-source and failed-run
  events;
- `INFO` is intended for the controlled pilot and records the complete event
  sequence above;
- `DEBUG` is development-only and does not add sensitive fields.

Invalid values fall back to `WARNING`. Log retention, destination, access
review, deletion schedule, and incident-export procedures are deliberately not
defined in this phase and must be approved before production use.

## LGPD And Health Data

Operational logs remain security-sensitive even after masking. Access must be
limited to authorized staff, and logs must not be combined with external
datasets to re-identify a person. Before real-data operation, the controller
must define purpose, lawful basis, minimization, retention, access controls,
incident handling, and data-subject procedures appropriate to LGPD and health
data. A privacy and security review is required before increasing pilot scope.
