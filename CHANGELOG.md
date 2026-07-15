# Changelog

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
