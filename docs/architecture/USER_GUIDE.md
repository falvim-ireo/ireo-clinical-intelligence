# Gmail Radiology Dry-Run

## Purpose

The command below reads a small set of TransferNow notifications from an
authorized Gmail account and produces Radiology Intake plans locally:

```powershell
ireo-clinical-intelligence radiology-gmail-dry-run
```

It does not download TransferNow content, alter Gmail messages, create clinical
folders, query Clinicorp, or access OneDrive. In Phase 3A the CLI does not load
a patient directory, so patient association remains queued for manual review.

## Create OAuth Credentials

1. Create or select a project in Google Cloud Console.
2. Enable the Gmail API.
3. Configure the OAuth consent screen and add the authorized pilot account as a
   test user when the app is in testing mode.
4. Create an OAuth client with application type **Desktop app**.
5. Download the client JSON and store it as `gmail_credentials.json`, or use a
   protected location outside the repository.

The application never requests or stores the Gmail password. On first use, the
system browser opens the Google authorization page. After consent, the OAuth
library stores a token in `gmail_token.json` by default.

The following names are ignored by Git:

```text
gmail_credentials.json
gmail_token.json
credentials.json
token.json
client_secret*.json
```

Prefer an operating-system protected directory. Configure non-default paths in
the local environment without committing their values:

```powershell
$env:GMAIL_CREDENTIALS_FILE = "C:\secure\ireo-gmail-credentials.json"
$env:GMAIL_TOKEN_FILE = "C:\secure\ireo-gmail-token.json"
```

## Scope And Query

The only OAuth scope requested is:

```text
https://www.googleapis.com/auth/gmail.readonly
```

The initial safe configuration is:

```text
GMAIL_QUERY=from:noreply@transfernow.net subject:TransferNow
GMAIL_MAX_MESSAGES=5
```

`GMAIL_QUERY` may be changed locally using Gmail search syntax. The pilot limit
is always clamped between one and five, even if a larger value is configured.

## Install And Run

Install the project and development dependencies:

```powershell
python -m pip install -e ".[dev]"
```

Then run the command. The first execution may open the browser for OAuth
consent. Output is restricted to masked message ID, archive name, probable
patient, matching status, review requirement, and proposed logical destination.
The full body and TransferNow link are not printed.

## Revoke Access

To revoke user authorization, open the Google Account security page, find the
application under third-party connections, and remove its access. Then delete
the local `gmail_token.json` file (or the configured token path).

If the OAuth client itself is no longer needed, delete it in Google Cloud
Console under Google Auth Platform credentials. Revoking or deleting a client
does not require changing application source code.

Official references:

- https://developers.google.com/workspace/gmail/api/quickstart/python
- https://developers.google.com/identity/protocols/oauth2/resources/best-practices
