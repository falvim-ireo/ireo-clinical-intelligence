# Sprint 16.2

## Objetivo

Consolidar o `ClinicalPackage` como contrato oficial entre aquisição e
processamento, preservando integralmente a estrutura clínica e os metadados
fornecidos por Cfaz, TransferNow e futuros providers.

## Motivação

A Sprint 16.1 normalizava arquivos, mas ainda havia risco de o restante do
pipeline depender do payload bruto do Cfaz ou perder a semântica de coleção,
seção e nome exibido pelo provider. A classificação por MIME não era suficiente
para diferenciar radiografia, fotografia, laudo, tomografia e modelo digital.

## Arquitetura afetada

```text
Provider
  → Provider Metadata
  → ClinicalPackage
  → ClinicalAssetNormalizer
  → OneDrive Publisher
  → SQLite
```

Nenhum módulo posterior deve interpretar diretamente o payload Cfaz. A Sprint
separou deliberadamente o contrato clínico da persistência: `clinical_assets`
ficou para 16.3.

## Arquivos alterados

- `src/acquisition/models/clinical_package.py`;
- `src/acquisition/models/__init__.py`;
- `src/acquisition/base.py`;
- `src/acquisition/cfaz_provider.py`;
- `src/acquisition/transfernow_provider.py`;
- `src/acquisition/clinical_normalizer.py`;
- `src/acquisition/service.py`;
- `src/radiology/supervised_import.py`;
- testes do pacote, providers, normalizador e retrocompatibilidade.

A lista exata do snapshot 16.2 não possui commit individual; foi preservada
posteriormente no commit consolidado.

## Comandos criados

Nenhum comando operacional novo era requisito. O fluxo do usuário, Clinicorp e
OneDrive deveriam permanecer inalterados.

## Problemas encontrados

- perda potencial de `provider_collection`, `provider_section` e
  `provider_display_name`;
- classificação indevida baseada somente em MIME;
- coleções genéricas `images_download_links`;
- necessidade de manter modelos, tomografias e relatórios separados;
- risco de introduzir `clinical_assets` antes de estabilizar o contrato.

## Problemas resolvidos

- `ClinicalPackage` tornou-se fronteira canônica;
- cada arquivo tornou-se `ClinicalAsset`;
- metadados do provider foram preservados;
- mapeamento collection → clinical category foi centralizado;
- manifesto passou a transportar pacote e versões;
- TransferNow passou a obedecer ao mesmo contrato;
- retrocompatibilidade com manifestos anteriores foi mantida.

## Critérios de aceite

- todo asset com coleção e categoria;
- nenhum metadado autorizado do provider perdido;
- STL em Modelos Digitais, tomografia em Tomografia e laudo separado;
- pacote como único contrato entre aquisição e pipeline;
- nenhuma indexação `clinical_assets` nesta Sprint;
- testes de múltiplas coleções, modelos, seções, manifesto e compatibilidade.

Validação histórica registrada: 492 testes aprovados, 2 ignorados.

## Resultado alcançado

Sprint concluída no escopo: `ClinicalPackage` e `ClinicalAsset` passaram a
preservar a semântica do provider sem modificar prematuramente banco,
OneDrive, Clinicorp ou fluxo operacional.

## Limitações

A validação real com pedido contendo múltiplas seções foi recomendada. O commit
e a tag `v0.16.2` foram propostos, mas não criados durante o desenvolvimento.

## Próximo Sprint

Sprint 16.3 — criar `clinical_assets` consumindo exclusivamente o
`ClinicalPackage`.
