# Sprint 16.1

## Objetivo

Implementar a normalização completa de arquivos clínicos no pipeline de
aquisição multiprovedor e integrar Cfaz, DICOM, publicação OneDrive e índice
radiológico. Evidência: commit `da3a195`, tag `v0.16.1`.

## Motivação

O pipeline anterior publicava exames radiológicos, mas ainda precisava de um
contrato uniforme para ativos de formatos e coleções diferentes. A aquisição
Cfaz exigia classificação, nomes determinísticos, deduplicação e preservação
do contexto do provider sem acoplar a origem ao publicador.

## Arquitetura afetada

`AcquisitionProvider` passou a alimentar o fluxo comum de quarentena,
normalização, `ClinicalPackage`, DICOM, manifesto, publicação e índice.
`CfazProvider` e `TransferNowProvider` ficaram atrás da mesma abstração.
`ExamIndexService` tornou o SQLite uma projeção reconstruível dos manifestos.

## Arquivos alterados

O commit alterou 39 arquivos. Principais:

- `src/acquisition/base.py`, `service.py`, `cfaz_provider.py`,
  `cfaz_operations.py`, `cfaz_repair.py`, `clinical_normalizer.py`;
- `src/acquisition/transfernow_provider.py`;
- `src/radiology/dicom_reader.py`, `exam_index_service.py`,
  `supervised_import.py`, `archive_extractor.py`, `auto_run.py`;
- `src/integrations/gmail_connector.py`, `onedrive_graph.py`;
- `src/main.py`, `src/core/config.py`;
- testes de acquisition, Cfaz, normalização, DICOM, índice, Graph e importação.

O commit também incluiu arquivos locais de credenciais/`.DS_Store`, removidos
posteriormente por `def69a6`.

## Comandos criados

Foram ampliados/adicionados comandos Cfaz, reparo, rebuild e consultas no
`src/main.py`. A atribuição exata de cada subcomando dentro do grande commit
não foi registrada separadamente: **PENDENTE DE DOCUMENTAÇÃO**.

## Problemas encontrados

- múltiplas formas de links no payload Cfaz;
- URLs assinadas não podiam ser persistidas;
- arquivos precisavam de validação por conteúdo, não apenas extensão;
- rebuild remoto precisava de retries, heartbeat e inventário eficiente;
- dados locais de credenciais foram incluídos por engano no commit.

## Problemas resolvidos

- contrato multiprovedor;
- normalização clínica multiformato;
- downloads Cfaz por streaming com limites e SHA-256;
- deduplicação de conteúdo;
- inventário DICOM estrutural;
- índice SQLite reconstruível;
- reparo de importações Cfaz;
- ignore/remoção posterior dos arquivos locais sensíveis.

## Critérios de aceite

- providers não conhecem OneDrive, SQLite ou CLI;
- links assinados não são persistidos;
- arquivos são validados antes da publicação;
- nomes/pastas são determinísticos;
- ambiguidades falham para revisão;
- manifestos completos podem reconstruir o índice;
- testes adicionados no commit passam.

O resultado exato da execução da suíte no momento do commit:
**PENDENTE DE DOCUMENTAÇÃO**.

## Resultado alcançado

Normalização e aquisição clínica multiprovedor consolidadas, com Cfaz integrado
ao pipeline comum e base para indexação de ativos.

## Limitações

- corpus real completo não foi documentado;
- modelos digitais ainda não estavam homologados;
- política formal de migrações/backup não foi concluída;
- o commit foi grande e agrupou mais de uma evolução arquitetural.

## Próximo Sprint

Sprint 16.2. Objetivo e escopo individual: **PENDENTE DE DOCUMENTAÇÃO**.
