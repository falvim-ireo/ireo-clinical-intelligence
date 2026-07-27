# Roadmap

## Concluído

- Foundation
- Clinicorp
- Gmail
- TransferNow
- Radiology Intake supervisionado
- MVP v1.0.0
- idempotência persistente e prevenção de reimportações
- ClinicalPackage e preservação de metadados do provider
- índice `clinical_assets`
- timeline, busca, resumo clínico e dashboard somente leitura
- filtragem de thumbnails genéricos
- descoberta e implementação de leitura de modelos digitais Cfaz
- reset local auditável por pedido
- homologação sintética integrada do ciclo Cfaz base: importação, normalização,
  resolução de paciente, publicação local stateful, indexação, reset e
  reimportação idempotente;
- reconciliação fail-closed de publicação remota `COMPLETE` quando `exam_id`,
  caminhos, checksums, tamanhos e arquivos remotos coincidem integralmente.

## Próximos

Sequência obrigatória:

1. Concluir aquisição automatizada de exames de imagem.
2. Validar benefício operacional e clínico no IREO.
3. Iniciar Patient Recall Engine.
4. Expandir robustez somente quando sustentada por risco ou valor demonstrado.

Os itens listados como concluídos acima são entregas históricas preservadas;
eles não constituem, isoladamente, prova de que a entrega vertical atual já
esteja concluída ou tenha benefício operacional validado.

Itens ainda sem prova permanecem abertos dentro da primeira entrega apenas
quando forem necessários ao fluxo real ou às garantias mínimas. Homologações,
reconciliação, validação em múltiplos exames e outras ampliações não devem ser
marcadas como concluídas sem evidência. Uploads alternativos, novas
integrações, alta disponibilidade e generalizações ficam adiados até que o
gate pragmático do documento mestre demonstre necessidade atual.

IA, busca semântica e comparação inteligente permanecem posteriores à
aquisição, à validação de benefício e ao Patient Recall Engine, além de
exigirem governança própria.

## Segurança adiada para antes de distribuição

A reescrita do histórico Git anterior ao commit de sanitização permanece como
débito técnico obrigatório antes de adicionar colaboradores, tornar o
repositório público, criar releases/distribuir a aplicação, implantar em nuvem
ou ambiente multiusuário, ou integrar serviços externos adicionais.

Enquanto isso, o projeto permanece protótipo local em repositório privado,
individual, sem dados identificáveis ou segredos em novos commits.
