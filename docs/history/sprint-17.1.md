# Sprint 17.1

## Objetivo

Criar a Clinical Timeline por paciente sobre os ativos indexados.

## Motivação

Até a Sprint 16, o projeto possuía infraestrutura de aquisição e persistência,
mas o usuário ainda não tinha uma visão longitudinal navegável. A Sprint 17
deveria tornar os dados utilizáveis antes de introduzir IA.

## Arquitetura afetada

Somente a camada de consulta:

```text
clinical_assets + exams + patients
  → ExamIndexService
  → patient-timeline
```

Não acessa provider, manifesto, OneDrive, Gmail, Clinicorp ou IA.

## Arquivos alterados

- `src/radiology/exam_index_service.py`;
- `src/main.py`;
- testes do índice/consulta.

O diff individual não foi preservado em commit próprio.

## Comandos criados

```bash
python -m main patient-timeline --patient "Nome do paciente"
```

Ordena por data, tipo e provider e apresenta categorias, quantidades e pedido.

## Problemas encontrados

Ativos históricos estavam fielmente classificados como `DOCUMENTATION` quando
não havia metadado suficiente; a consulta não deveria reinterpretá-los.

## Problemas resolvidos

Foi criada uma timeline somente leitura, independente da aquisição e baseada
exclusivamente no SQLite.

## Critérios de aceite

- banco vazio e paciente inexistente não falham;
- ordenação cronológica;
- múltiplos exames/providers;
- nenhuma escrita ou chamada externa;
- nenhuma heurística de reclassificação.

Validação histórica: 492 testes aprovados, 2 ignorados e `git diff --check`.

## Resultado alcançado

Timeline implementada e validada com pacientes reais já indexados, refletindo
exatamente as categorias persistidas.

## Limitações

A qualidade da timeline depende da qualidade histórica de `clinical_assets`.

## Próximo Sprint

Sprint 17.2 — Clinical Search.
