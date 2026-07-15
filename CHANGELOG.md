# Changelog

## [Unreleased]

### Added

- modo `radiology-auto-run` fail-closed, fila SQLite `REVIEW_REQUIRED` e resumo sanitizado da execução agendada
- download TransferNow headless opcional, filtro de data inicial, relatório de pendências e lock do agendador Windows
- comandos `intake-review-list` e `radiology-auto-status`, além do script Windows `scripts/run_radiology_auto.ps1`
- histórico persistente SQLite e schema versionado para o Radiology Intake
- detecção por SHA-256, Gmail, arquivo/paciente e destino
- comando sanitizado `intake-history`
- reimportação consciente com `--allow-reimport` e confirmação `REIMPORTAR`
- auto-seleção opt-in de paciente e pasta quando a resolução é única e segura
- override CLI `--force-manual-selection`
- modos e motivos de seleção no `manifest.json`
- eventos sanitizados de seleção automática e manual

### Security

- banco local ignorado pelo Git e composto somente por dados operacionais mascarados
- falha do histórico bloqueia o fluxo antes da cópia
- confirmação final `CONFIRMAR` permanece obrigatória
- ambiguidades, modo offline e motivos desconhecidos falham para seleção manual

## [1.0.0] - 2026-07-15

### Added

- Gmail OAuth readonly
- parsing TransferNow
- download HTTP e Playwright
- quarentena
- extração RAR/ZIP
- PatientResolver
- ClinicorpPatientRepository
- localização OneDrive
- importação supervisionada
- SHA-256
- manifest.json
- observabilidade sanitizada
- testes automatizados

### Security

- proteção de credenciais
- validação de domínio
- proteção contra traversal
- bloqueio de sobrescrita
- revisão humana obrigatória em ambiguidades

### Known limitations

- depende da pasta OneDrive local;
- não executa continuamente em background;
- não lê metadados DICOM;
- não remove temporários automaticamente;
- não marca e-mails como processados;
- não possui dashboard.
