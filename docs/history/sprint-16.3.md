# Sprint 16.3

## Objetivo

Implementar a indexação de ativos clínicos. Evidência: commit `4c2860f`, tag
`v0.16.3`.

## Motivação

O índice radiológico já projetava pacientes, exames, estudos e séries, mas as
consultas precisavam enxergar cada radiografia, fotografia, tomografia,
relatório ou modelo como ativo clínico estruturado.

## Arquitetura afetada

Foi formalizado `acquisition.models.ClinicalPackage`; `ExamIndexService`
passou a projetar ativos em `clinical_assets`; normalizador/provider passaram
a fornecer os metadados necessários. O SQLite continuou derivado do manifesto.

## Arquivos alterados

- `src/acquisition/cfaz_provider.py`;
- `src/acquisition/clinical_normalizer.py`;
- `src/acquisition/models/__init__.py`;
- `src/acquisition/models/clinical_package.py`;
- `src/acquisition/service.py`;
- `src/main.py`;
- `src/radiology/exam_index_service.py`;
- `tests/test_cfaz_repair.py`;
- `tests/test_clinical_package.py`.

## Comandos criados

O commit acrescentou interface de consulta em `src/main.py`. O nome exato do
subcomando incluído neste commit deve ser confirmado pelo diff histórico:
**PENDENTE DE DOCUMENTAÇÃO**.

## Problemas encontrados

- ativos existiam no manifesto, mas não tinham projeção consultável própria;
- contratos de normalização e indexação precisavam compartilhar estrutura;
- reparos antigos precisavam continuar compatíveis.

## Problemas resolvidos

- modelo serializável de pacote/ativo;
- tabela `clinical_assets`;
- índices por paciente, request, categoria, provider e SHA;
- ingestão dos ativos a partir de formatos de manifesto compatíveis.

## Critérios de aceite

- reconstrução de `ClinicalPackage` por dicionário;
- ativos vinculados a exame/paciente;
- unicidade por exame, SHA e caminho;
- consultas não leem arquivos remotos;
- compatibilidade com manifestos anteriores.

## Resultado alcançado

Ativos clínicos tornaram-se entidades consultáveis no SQLite sem transformar o
banco em fonte clínica primária.

## Limitações

Campos de modelos digitais foram adicionados posteriormente; versionamento do
índice permaneceu em 1.

## Próximo Sprint

Sprint 17 — camada de consulta clínica, commit `d065a6f`; a decomposição 17.1
começa depois, sem commit individual.
