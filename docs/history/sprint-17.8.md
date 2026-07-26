# Sprint 17.8

## Objetivo

Permitir reset local e reimportação controlada de um único pedido Cfaz,
preservando auditoria e sem exclusão remota.

## Motivação

A homologação de modelos digitais exigia repetir um pedido como importação
virgem, sem editar SQLite/manifests manualmente, sem afetar outros pacientes e
sem duplicar dados no OneDrive.

## Arquitetura afetada

```text
cfaz-reset
  → inventário dry-run por request
  → clinical_assets/exams/manifests/caches/intake
  → preservação cfaz_import_history como READY_FOR_REIMPORT
  → nenhuma exclusão OneDrive
  → nova aquisição/reconciliação idempotente
```

## Arquivos alterados

- `src/main.py`;
- `src/acquisition/cfaz_operations.py`;
- `src/radiology/intake_history.py`;
- `src/radiology/exam_index_service.py`;
- módulos de modelos digitais/coleção/sessão;
- testes de reset, inventário, modelos e idempotência.

O estado final dessas alterações foi preservado no commit consolidado
`18540ea`.

## Comandos criados

```bash
python -m main cfaz-reset --request-id ID --dry-run
python -m main cfaz-reset --request-id ID --apply
```

Também foram construídos/experimentados fluxos de `cfaz-reimport`,
`cfaz-download-models`, identificação e collection-identify.

## Problemas encontrados

- primeira versão limpava `cfaz_import_history`, `clinical_assets`, `exams` e
  manifesto, mas deixava um intake `COMPLETED`;
- a segunda camada de idempotência bloqueava por `DESTINATION`;
- reimportação baixava novamente e parava antes da publicação;
- destino remoto existente precisava ser reconciliado, nunca duplicado;
- histórico precisava ser preservado, não apagado.

## Problemas resolvidos

- dry-run mostra manifesto, pacote, assets, exames, histórico, intake e
  fingerprints;
- reset remove/invalida somente projeções e estados locais do pedido;
- `cfaz_import_history` passa a `READY_FOR_REIMPORT`;
- demais pacientes permanecem intactos;
- OneDrive não é alterado pelo reset;
- inventário/diagnóstico de modelos e coleção foi ampliado.

## Critérios de aceite

- reset restrito ao request;
- nenhuma exclusão remota;
- auditoria preservada;
- intake/destination fingerprints incluídos;
- nova importação não bloqueada por idempotência local residual;
- destino remoto reconciliado sem nova pasta ou duplicação;
- modelos ausentes adicionados;
- dashboard/summary atualizados sem rebuild;
- suíte completa.

## Resultado alcançado

O dry-run e o primeiro apply foram executados em caso real: 1 manifesto,
1 pacote, 15 assets, 1 exame e 30 arquivos locais; histórico mudou para
`READY_FOR_REIMPORT` e os outros pacientes permaneceram intactos.

A homologação revelou o bug da segunda fonte de idempotência
(`radiology_imports`/destination fingerprint). O código consolidado inclui a
evolução local posterior, mas a homologação ponta a ponta dos STL não foi
registrada como concluída.

## Limitações

Sprint implementada, porém homologação produtiva completa permanece aberta:
download → extração → publicação → indexação → consulta de dois STL e segunda
execução idempotente.

## Próximo Sprint

Sprint 18 deve começar pela homologação única pendente dos modelos digitais e
da reimportação completa, não por IA.
