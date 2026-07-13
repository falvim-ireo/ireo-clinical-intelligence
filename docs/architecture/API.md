# Clinicorp Patient Lookup Boundary

## Responsibilities

Patient lookup is separated into three layers:

```text
PatientResolver
    -> PatientRepository
    -> ClinicorpPatientRepository
    -> ClinicorpAPI
```

- `PatientResolver` owns similarity, thresholds, ambiguity, and the final
  resolution decision.
- `PatientRepository` is the provider-independent candidate lookup port.
- `ClinicorpPatientRepository` validates and maps active Clinicorp records to
  domain `Patient` objects.
- `ClinicorpAPI` owns the existing HTTP and Basic Auth behavior configured by
  `Config`.

The adapter receives a `ClinicorpAPI` instance through constructor injection.
Tests use a fake with the same `buscar_paciente` operation, so they require no
credentials and make no HTTP calls.

An offline contract suite also drives the real `ClinicorpAPI.buscar_paciente`
method with anonymized JSON fixtures. It verifies the complete
`ClinicorpAPI -> ClinicorpPatientRepository -> PatientResolver` boundary
without constructing authentication or opening a network connection.

## Candidate Query

`find_candidates(name)` rejects an empty value, removes only external
whitespace, and sends the remaining original text to Clinicorp. Accents,
letter case, internal whitespace, and word order are preserved:

```python
api.buscar_paciente(original_name.strip(), somente_ativos=True)
```

There is at most one API call per method invocation. The adapter keeps only
well-formed `ACTIVE` records containing a usable `PatientId` and name, removes
duplicates by `PatientId`, and retains the API response order. It returns an
empty list when there are no valid candidates. It performs no matching or
scoring.

`PatientNormalizer` is applied only after Clinicorp returns candidates, for
internal comparison and validation (and logical deduplication when needed). It
never changes the external query parameter at this stage.

Clinicorp's current search is most reliable with an exact full name. The
adapter does not synthesize name variations or search CPF, phone, birth date,
aliases, or other identifiers.

## Failure Contract

Timeout, HTTP failure, unavailability, and a structurally unusable response are
translated to `PatientRepositoryUnavailableError`. Its public text contains no
token, API user, sensitive URL parameters, full response body, or clinical
data. The resolver maps this error to `PATIENT_SOURCE_UNAVAILABLE`, which always
requires manual review and never yields an automatic association.

The HTTP client also omits status response bodies and raw request exceptions
from its own public errors. Operational failure details are represented only by
sanitized reason codes in the audit stream described in
[`OBSERVABILITY.md`](OBSERVABILITY.md).

## Supervised Gmail Input

`GmailConnector.get_message(message_id)` performs one readonly Gmail `get`
operation with `format="full"` and converts the result through the existing MIME
parser. It does not list, modify, label, mark, or delete messages. The
supervised import passes the parsed TransferNow URL directly to a foreground
download attempt and falls back to `--archive-path` when no local archive is
produced.
