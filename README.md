# IREO Clinical Intelligence

Plataforma operacional para ingestão segura e supervisionada de exames radiológicos no IREO. O MVP recebe notificações do TransferNow no Gmail, baixa e valida o arquivo, confirma a identidade no Clinicorp e copia o exame para a pasta local sincronizada pelo OneDrive.

**Status:** MVP v1.0.0 validado em piloto real supervisionado. O sistema exige supervisão humana em todas as importações.

## Fluxo

```text
Gmail readonly -> mensagem TransferNow -> link público /dl/
-> download HTTP ou Chromium temporário -> SHA-256 -> quarentena
-> extração segura -> Clinicorp -> confirmação do paciente
-> seleção da pasta OneDrive local -> revisão humana -> cópia sem sobrescrita
-> manifest.json
```

## Requisitos

- Windows 11 e Python 3.11 ou superior;
- UnRAR (console) para arquivos RAR;
- Playwright Chromium para páginas que exigem JavaScript;
- credencial OAuth Desktop do Gmail com escopo somente leitura;
- acesso de leitura à API Clinicorp;
- pasta de pacientes do OneDrive sincronizada localmente.

## Instalação

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m playwright install chromium
```

Copie `.env.example` para `.env` e preencha somente no arquivo local. Configure as credenciais Clinicorp, os caminhos protegidos dos arquivos OAuth Gmail, a raiz local do OneDrive, a quarentena e o executável UnRAR. Não versione `.env`, tokens, credenciais ou caminhos com dados reais.

## Comandos principais

```powershell
ireo-clinical-intelligence browser-self-test
ireo-clinical-intelligence radiology-gmail-dry-run
ireo-clinical-intelligence radiology-import-supervised --archive-path "C:\caminho\exame.rar" --patient-source clinicorp
ireo-clinical-intelligence radiology-import-from-gmail --patient-source clinicorp
```

## Procedimento operacional

Valide primeiro OAuth, navegador, UnRAR, espaço livre e acesso às pastas. Execute o fluxo do Gmail em primeiro plano, selecione a mensagem, autorize o download e revise checksum, paciente, pasta, quantidade, tamanho e duplicidades. A cópia final só ocorre após digitar exatamente `CONFIRMAR`. Ao concluir, confira os arquivos e `manifest.json` no destino. Consulte o [guia do operador](docs/architecture/USER_GUIDE.md).

## Segurança e limitações

Gmail é readonly; URLs, credenciais e identificadores são sanitizados nos logs; downloads e extrações permanecem em quarentena; caminhos de arquivo são validados; destinos existentes não são sobrescritos; associações ambíguas e a cópia final exigem decisão humana.

O MVP depende da pasta local do OneDrive, não roda continuamente em background, não interpreta metadados DICOM, não remove temporários automaticamente, não marca e-mails como processados e não possui dashboard. Não substitui validação clínica nem deve operar sem supervisão.

### Auto-seleção segura (opt-in)

A redução de confirmações intermediárias é desativada por padrão. Para habilitá-la explicitamente:

```env
IREO_AUTO_SELECT_UNAMBIGUOUS=true
IREO_AUTO_SELECT_MIN_SCORE=0.95
```

O limiar é independente do `PatientResolver`. A auto-seleção exige exatamente um paciente elegível, sem revisão, com PatientId, score mínimo e motivo `EXACT_NAME`, `NORMALIZED_NAME` ou `SINGLE_HIGH_SCORE`, além de uma única pasta coerente e contida na raiz local. Ambiguidade, fonte indisponível, modo offline, pasta ausente/múltipla/incoerente ou motivo desconhecido mantêm a seleção manual.

Para forçar o comportamento manual em uma execução, acrescente `--force-manual-selection` a `radiology-import-supervised` ou `radiology-import-from-gmail`. Para desativar rapidamente em todo o ambiente, defina `IREO_AUTO_SELECT_UNAMBIGUOUS=false`. A prévia e a confirmação final digitada `CONFIRMAR` permanecem obrigatórias em todos os casos.

### Histórico e prevenção de reimportações

Importações são registradas localmente em SQLite, por padrão em `data/ireo_intake.db`. Configure outro local protegido com:

```env
IREO_INTAKE_DATABASE_PATH=data/ireo_intake.db
```

O SHA-256 do arquivo compactado é o sinal mais forte. Message ID do Gmail, URL de transferência, PatientId, destino, manifesto e checksums são persistidos somente como fingerprints SHA-256; nomes de paciente, URLs e caminhos clínicos completos não são armazenados.

Uma duplicidade concluída é bloqueada. A opção `--allow-reimport` apenas habilita a decisão: o alerta continua visível e o operador precisa digitar exatamente `REIMPORTAR`, além de manter a confirmação final `CONFIRMAR`.

Consulte o histórico sanitizado com:

```powershell
ireo-clinical-intelligence intake-history --limit 20
ireo-clinical-intelligence intake-history --status COMPLETED
ireo-clinical-intelligence intake-history --correlation-id correlation-local
```

Inclua o banco em backups locais protegidos, preferencialmente com o aplicativo parado e junto aos arquivos auxiliares `-wal` e `-shm`, se existirem. A política formal de retenção ainda não foi definida; não apague registros automaticamente.

### Execução automática agendada

O piloto começa desligado e nunca copia arquivos enquanto `IREO_AUTO_RUN_ALLOW_COPY=false`. Para ativar a consulta automática, configure em ambiente protegido `IREO_AUTO_RUN_ENABLED=true`, mantenha a cópia desligada e execute `ireo-clinical-intelligence radiology-auto-run`. Casos não inequívocos ficam em `intake-review-list`; consulte `radiology-auto-status` para o resumo sanitizado em `data/last_auto_run_summary.json`.

Para o Agendador de Tarefas do Windows, execute `scripts/run_radiology_auto.ps1` com uma conta que já possua token OAuth válido e acesso às pastas locais. Agende a cada 10 minutos, também na inicialização, somente com rede, com repetição em falha e sem iniciar uma segunda instância. O computador precisa estar ligado. O script usa lock local e remove lock abandonado após seis horas. Os modos aceitos são `review`, `headless` e `visible`. Em `visible`, marque **Executar somente quando o usuário estiver conectado**, mantenha o computador ligado e uma sessão Windows ativa; nenhuma intervenção do operador é necessária, embora o Chromium possa aparecer brevemente na tela. Falhas de automação, CAPTCHA, login, senha ou proteção viram revisão, sem tentativa de contorno. Para interromper imediatamente, defina `IREO_AUTO_RUN_ENABLED=false`.

`IREO_AUTO_RUN_START_DATE=YYYY-MM-DD` limita o primeiro processamento a mensagens a partir da data indicada. As pendências também são escritas sem dados clínicos em `data/radiology_review_required.txt`; execute `scripts/open_radiology_review.ps1` para listá-las.

## Solução de problemas

- **OAuth Gmail:** confira os caminhos de credencial/token e o escopo readonly; revogue e refaça o consentimento se o token estiver inválido.
- **Chromium ausente:** execute `python -m playwright install chromium` e depois `browser-self-test`.
- **RAR não extrai:** confirme `IREO_ARCHIVE_TOOL_PATH`, permissões e se o arquivo não exige senha.
- **Link não baixa:** use o fallback manual `radiology-import-supervised --archive-path ...`.
- **Paciente ou pasta ambíguos:** não prossiga até conferir manualmente a identidade e o destino.
- **Destino ou cópia parcial:** preserve as evidências e revise manualmente; o sistema não apaga resultados automaticamente.
