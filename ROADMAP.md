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

- homologação real supervisionada de dois STL Cfaz e do destino OneDrive;
- reconciliação idempotente com destino remoto existente;
- validação em múltiplos exames;
- upload direto Microsoft Graph;
- limpeza supervisionada da quarentena;
- fila de revisão;
- retomada supervisionada de pendências automáticas por correlation id;
- leitura DICOM;
- painel operacional.

IA, busca semântica e comparação inteligente permanecem posteriores à
homologação do repositório clínico e exigem governança própria.

## Segurança adiada para antes de distribuição

A reescrita do histórico Git anterior ao commit de sanitização permanece como
débito técnico obrigatório antes de adicionar colaboradores, tornar o
repositório público, criar releases/distribuir a aplicação, implantar em nuvem
ou ambiente multiusuário, ou integrar serviços externos adicionais.

Enquanto isso, o projeto permanece protótipo local em repositório privado,
individual, sem dados identificáveis ou segredos em novos commits.
