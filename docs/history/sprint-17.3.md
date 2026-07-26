# Sprint 17.3

## Objetivo

Criar resumo clínico estruturado do acervo radiológico de um paciente.

## Motivação

Timeline e busca exibiam eventos/ativos, mas faltava uma visão quantitativa
compacta com período e categorias do acervo.

## Arquitetura afetada

Consulta agregada somente leitura em `ExamIndexService`, exposta pela CLI.
O resumo consome o que está persistido; não reinterpreta arquivos.

## Arquivos alterados

- `src/radiology/exam_index_service.py`;
- `src/main.py`;
- testes do resumo.

## Comandos criados

```bash
python -m main patient-summary --patient "Nome do paciente"
```

Exibe exames, radiografias, fotografias, tomografias, DICOM, modelos STL,
laudos, primeiro e último exame.

## Problemas encontrados

Pedidos históricos sem metadados suficientes continuavam como
`DOCUMENTATION`. Inventar categorias na consulta violaria a arquitetura.

## Problemas resolvidos

Resumo quantitativo foi implementado sobre `clinical_assets`/`exams`, mantendo
aquisição → normalização → persistência → consulta.

## Critérios de aceite

- somente SQLite;
- paciente inexistente retorna zero sem falha;
- contagens consistentes;
- datas extremas corretas;
- nenhuma heurística ou escrita.

## Resultado alcançado

Sprint implementada no escopo e sem contaminar a consulta com regras de
aquisição.

## Limitações

O Clinical Patient Record mais amplo, health score, tamanho total e indicadores
de qualidade foram propostos, mas o comando atual é um resumo quantitativo.

## Próximo Sprint

Sprint 17.4 — Dashboard.
