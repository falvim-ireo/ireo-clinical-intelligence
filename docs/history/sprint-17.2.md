# Sprint 17.2

## Objetivo

Criar busca clínica estruturada por paciente, categoria, provider e intervalo
de datas.

## Motivação

A timeline resolvia navegação longitudinal de um paciente, mas faltava uma
consulta transversal sobre todo o repositório clínico.

## Arquitetura afetada

`ExamIndexService` recebeu busca agregada sobre SQLite; a CLI apenas formata o
resultado. Nenhuma integração externa participa.

## Arquivos alterados

- `src/radiology/exam_index_service.py`;
- `src/main.py`;
- testes da camada de consulta.

## Comandos criados

```bash
python -m main find-assets \
  --patient "PACIENTE_EXEMPLO" \
  --category Radiografia \
  --provider cfaz \
  --after 2025-01-01 \
  --before 2026-12-31
```

Retorno: paciente, data, categoria, provider, quantidade e destino OneDrive.

## Problemas encontrados

A pesquisa não poderia voltar a ler manifests ou consultar providers, sob risco
de resultados lentos e divergentes.

## Problemas resolvidos

Busca parcial por paciente, filtros combináveis e agrupamento por paciente,
data, categoria e provider foram implementados no índice local.

## Critérios de aceite

- somente leitura;
- filtros independentes/combinados;
- datas inclusivas conforme contrato;
- banco vazio;
- múltiplos providers/categorias;
- nenhuma chamada Cfaz, Gmail, TransferNow, Clinicorp ou OneDrive.

Validação histórica: 492 testes aprovados, 2 ignorados e `git diff --check`.

## Resultado alcançado

Camada de busca clínica independente da aquisição implementada.

## Limitações

Busca é estruturada, não semântica; não usa OCR, embeddings ou IA.

## Próximo Sprint

Sprint 17.3 — Clinical Summary.
