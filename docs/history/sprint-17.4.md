# Sprint 17.4

## Objetivo

Fechar a primeira camada de inteligência clínica com dashboard operacional
somente leitura e sem interface gráfica.

## Motivação

Era necessário observar o estado global do repositório depois de timeline,
search e summary, sem introduzir dashboard web ou IA.

## Arquitetura afetada

Agregações globais no SQLite, expostas pelo comando `dashboard`.

## Arquivos alterados

- `src/radiology/exam_index_service.py`;
- `src/main.py`;
- testes do dashboard.

## Comandos criados

```bash
python -m main dashboard
```

Exibe pacientes, pedidos/exames, assets, categorias, última importação, órfãos
e duplicados.

## Problemas encontrados

O relatório precisava funcionar com banco vazio e colunas opcionais, sem
reclassificar ou acessar serviços externos.

## Problemas resolvidos

Dashboard textual, rápido e somente leitura foi incorporado à camada de
consulta.

## Critérios de aceite

- totais consistentes com `clinical_assets`;
- banco vazio;
- múltiplos providers/categorias;
- registros incompletos;
- nenhuma escrita ou chamada externa.

## Resultado alcançado

A camada de consulta clínica passou a oferecer timeline, busca, resumo e
dashboard sobre uma única projeção local.

## Limitações

Não há UI gráfica, métricas clínicas interpretativas ou IA. A qualidade das
estatísticas depende da indexação histórica.

## Próximo Sprint

Sprint 17.5 — Thumbnail Filtering & Clinical Publication.
