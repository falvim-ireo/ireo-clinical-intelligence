# Guia do Operador — Radiology Intake v1.0.0

Este fluxo é exclusivamente supervisionado. Antes de começar, confirme que o exame, o ambiente e as autorizações pertencem ao atendimento correto.

## Execução automática (piloto)

Mantenha `IREO_AUTO_RUN_ENABLED=false` e `IREO_AUTO_RUN_ALLOW_COPY=false` até validar o piloto. A tarefa do Windows deve executar `scripts/run_radiology_auto.ps1`. Execute `ireo-clinical-intelligence intake-review-list` para pendências e `ireo-clinical-intelligence radiology-auto-status` para a última execução. Em caso de dúvida, desative `IREO_AUTO_RUN_ENABLED` e retome o caso pelo fluxo supervisionado normal usando o arquivo preservado na quarentena.

No piloto A, configure `IREO_AUTO_RUN_BROWSER_MODE=headless` e mantenha a cópia desligada. Confirme uma única mensagem, download, extração, destino proposto e status de revisão/aguardo, sem cópia. Só após autorização explícita avance ao piloto B com `IREO_AUTO_RUN_ALLOW_COPY=true`. A máquina deve permanecer ligada para o Agendador executar.

O modo `visible` executa a mesma automação com `headless=False`, perfil temporário e sem solicitar clique, confirmação ou `ABRIR NAVEGADOR`. No Agendador de Tarefas, selecione **Executar somente quando o usuário estiver conectado**, mantenha o computador ligado e uma sessão Windows ativa. Nenhuma intervenção do operador é necessária; o Chromium pode aparecer brevemente na tela. Se o botão não for localizado, ou se houver CAPTCHA, login, senha ou outra proteção, o navegador fecha e o caso segue para `REVIEW_REQUIRED`, sem contornar a proteção.

## Fluxo operacional aprovado

1. Verifique se o OAuth Gmail está configurado com escopo readonly.
2. Execute:

   ```powershell
   ireo-clinical-intelligence radiology-import-from-gmail --patient-source clinicorp
   ```

3. Selecione a mensagem TransferNow exibida pelo comando.
4. Confirme o download.
5. Quando solicitado, autorize a abertura do navegador. O Chromium usa um contexto temporário; não tente contornar CAPTCHA, senha ou proteções do site.
6. Aguarde o término do download e o cálculo do checksum SHA-256.
7. Confira o resultado e confirme a continuidade para a etapa de importação.
8. Revise os candidatos do Clinicorp e confirme o paciente correto. Em caso de ambiguidade, interrompa até realizar a revisão humana.
9. Revise e confirme a pasta correta do paciente no OneDrive local.
10. Confira o arquivo de origem, destino, quantidade de arquivos, tamanho total e possíveis duplicidades.
11. Somente quando toda a prévia estiver correta, digite exatamente `CONFIRMAR`.
12. Confira a pasta final e o arquivo `manifest.json`. Preserve a quarentena para investigação quando houver falha ou resultado parcial.

### Fallback por arquivo local

Se a aquisição pelo Gmail/TransferNow não puder ser concluída, baixe o arquivo por um meio aprovado, coloque-o na quarentena e execute:

```powershell
ireo-clinical-intelligence radiology-import-supervised `
  --archive-path "C:\caminho\controlado\exame.rar" `
  --patient-source clinicorp
```

O fallback mantém as mesmas confirmações de paciente, pasta, prévia e cópia final. Não use arquivos fora do fluxo autorizado.

### Redução opcional de confirmações

Por padrão, paciente e pasta continuam manuais. Um administrador pode habilitar localmente:

```env
IREO_AUTO_SELECT_UNAMBIGUOUS=true
IREO_AUTO_SELECT_MIN_SCORE=0.95
```

Com a flag ativa, o terminal informa o paciente e a pasta selecionados automaticamente e seus motivos. Isso só ocorre com um candidato Clinicorp elegível, PatientId presente, score suficiente, resolução não ambígua e uma única pasta coerente dentro da raiz. Modo offline, mais de um candidato ou pasta, score baixo, indisponibilidade, inconsistência ou motivo não reconhecido exigem seleção humana.

Use `--force-manual-selection` em qualquer comando de importação para ignorar a automação naquela execução. Em caso de dúvida operacional, altere imediatamente `IREO_AUTO_SELECT_UNAMBIGUOUS=false` e reinicie o comando. Mesmo com auto-seleção, revise todos os dados da prévia e digite `CONFIRMAR`; sem essa confirmação nenhum arquivo é copiado.

### Duplicidade e histórico

O histórico local fica no caminho configurado por `IREO_INTAKE_DATABASE_PATH`. Antes do download e da cópia, o sistema compara fingerprints e o SHA-256 do arquivo. Registros `FAILED` ou `CANCELLED` permitem nova tentativa; registros `COMPLETED` coincidentes são bloqueados.

Para consultar sem revelar dados clínicos:

```powershell
ireo-clinical-intelligence intake-history --limit 20
ireo-clinical-intelligence intake-history --status COMPLETED
```

Se uma reimportação for clinicamente justificada, execute o comando original com `--allow-reimport`, revise o alerta e digite `REIMPORTAR`. Isso não substitui a prévia nem `CONFIRMAR`. Sem a opção, a duplicidade é bloqueada; com a opção mas sem a palavra exata, ela é cancelada.

Faça backup do banco em armazenamento local protegido com o processo parado. Preserve também arquivos SQLite `-wal` e `-shm` presentes. Não edite o banco manualmente. A retenção ainda não possui política aprovada e nenhuma limpeza automática é executada.

## Gmail Radiology Dry-Run

## Purpose

The command below reads a small set of TransferNow notifications from an
authorized Gmail account and produces Radiology Intake plans locally:

```powershell
ireo-clinical-intelligence radiology-gmail-dry-run
```

It does not download TransferNow content, alter Gmail messages, create clinical
folders, query Clinicorp, or access OneDrive. Without an explicit patient
source, patient lookup remains offline and associations stay queued for manual
review.

To opt in to the existing Clinicorp read-only lookup, run:

```powershell
ireo-clinical-intelligence radiology-gmail-dry-run --patient-source clinicorp
```

This option uses the existing `Config` values and Basic Auth implementation. It
performs one active-patient query using the original full name extracted from
the intake, removing only external whitespace. Accents, letter case, internal
whitespace, and word order are preserved. Normalization occurs only after
candidate records return. The adapter does not generate name variants or search
by CPF, phone, birth date, or aliases.

If Clinicorp times out, returns an HTTP error, is unavailable, or returns an
unusable structure, the output remains a manual-review plan. No patient can be
associated automatically while the source is unavailable, and technical
response details or credentials are not included in review reasons.

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

## Pilot Audit Log

Sanitized structured logging defaults to `WARNING`. For a controlled pilot,
enable the operational event sequence locally:

```powershell
$env:IREO_AUDIT_LOG_LEVEL = "INFO"
ireo-clinical-intelligence radiology-gmail-dry-run
```

Use `DEBUG` only in development. It exposes no additional clinical or secret
fields. Logs contain a UTC timestamp, a per-execution `correlation_id`, status,
reason codes, manual-review state, deterministic masked identifiers, a masked
archive reference, and at most the TransferNow hostname.

Do not copy e-mail bodies, URLs, tokens, credentials, patient names, telephone
numbers, e-mail addresses, or external response bodies into operational notes.
Retention and deletion policy are not yet approved, so pilot logs must remain
access-restricted and short-lived under the organization's interim security
controls. Logs have no diagnostic purpose. See
[`OBSERVABILITY.md`](OBSERVABILITY.md) for the event catalog and LGPD cautions.

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

## Supervised Radiology Import

The foreground-only MVP accepts exactly one input mode:

```powershell
ireo-clinical-intelligence radiology-import-supervised `
  --archive-path "D:\IREO_Radiology_Quarantine\EXAME_FICTICIO.zip" `
  --patient-source clinicorp
```

or a selected Gmail message through the existing readonly connector:

```powershell
ireo-clinical-intelligence radiology-import-supervised `
  --email-message-id "GMAIL_MESSAGE_ID" `
  --patient-source clinicorp
```

Use `--patient-source offline` when Clinicorp must not be queried. In that mode,
the probable name from the archive still requires an explicit manual
confirmation and no PatientId is recorded.

The Gmail mode reads only the selected message and attempts to save the parsed
TransferNow archive in quarantine. It never marks or modifies the message. A
TransferNow link may be a landing page rather than a direct archive; if the
download cannot be completed, download the `.zip` or `.rar` manually and use
`--archive-path`.

For supervised selection and download from up to five recent readonly Gmail
messages, run:

```text
ireo-clinical-intelligence radiology-import-from-gmail --patient-source clinicorp
```

Quando o link do TransferNow exigir JavaScript, o fluxo oferece abrir um Chromium
visível e isolado. Confirme com `ABRIR NAVEGADOR`; se o controle não for localizado
automaticamente, clique manualmente no botão de download. O próximo e único download
será validado e salvo na quarentena antes de o navegador fechar.

Configuração do piloto:

```env
IREO_BROWSER_HEADLESS=false
IREO_BROWSER_DOWNLOAD_TIMEOUT_SECONDS=3600
```

O piloto permanece visível (`headless=False`). Para executar o primeiro piloto real:

```text
ireo-clinical-intelligence radiology-import-from-gmail --patient-source clinicorp
```

The download uses HTTPS only, follows a limited number of validated
TransferNow redirects, streams into a correlation-specific quarantine folder,
keeps failed `.part` files, and atomically renames only complete `.rar` or
`.zip` files. An HTML landing page is inspected only for an HTTPS TransferNow
download link. If no reliable HTTP endpoint exists, the supervised Chromium
fallback is offered. JavaScript supplied externally is not executed by the
application, and CAPTCHA, passwords, and browser protections are not bypassed.
Failures preserve the manual `radiology-import-supervised --archive-path` fallback.

The configured local paths are:

```text
IREO_ONEDRIVE_PATIENTS_PATH=C:\IREO\OneDrive\Pacientes
IREO_RADIOLOGY_QUARANTINE_PATH=C:\IREO\QuarentenaRadiologia
IREO_ARCHIVE_TOOL_PATH=C:\Program Files\WinRAR\UnRAR.exe
IREO_ARCHIVE_TIMEOUT_SECONDS=1800
IREO_TRANSFERNOW_CONNECT_TIMEOUT_SECONDS=30
IREO_TRANSFERNOW_READ_TIMEOUT_SECONDS=1800
IREO_TRANSFERNOW_MAX_DOWNLOAD_BYTES=10737418240
```

The command displays all Clinicorp candidates and compatible local patient
folders. Selection is always manual. Before any destination is created, it
shows the archive, extracted folder, confirmed patient and PatientId, selected
patient folder, final destination, file count, total size, and possible
duplicate count. Copying starts only when the operator types exactly:

```text
CONFIRMAR
```

The command copies without moving, deleting, or overwriting. It retains the
archive and extraction folder in quarantine and creates `manifest.json` beside
the copied files with SHA-256 checksums.

### Checklist Before The First Real Pilot

1. Confirm that the archive belongs to the intended Sorrimagem examination.
2. Confirm free space and write access in quarantine and the patient root.
3. Confirm the configured UnRAR console path and its 1800-second pilot timeout.
4. Confirm Gmail OAuth readonly scope if Gmail mode will be used.
5. Confirm Clinicorp credentials without displaying or copying them to logs.
6. Start with `--archive-path` so Gmail/TransferNow acquisition is separated
   from the first supervised copy.
7. Review every Clinicorp candidate and every matching patient folder.
8. Verify the proposed destination, file count, total size, and duplicates.
9. Type `CONFIRMAR` only after the complete preview is correct.
10. Inspect `manifest.json` and compare the retained source archive after the
    copy.

The MVP does not inspect DICOM, create missing patient folders, resolve archive
passwords, guarantee that a TransferNow landing URL is a direct download, or
clean partial destinations after a local write failure. Any partial result must
be reviewed manually because the application deliberately performs no
automatic deletion.
