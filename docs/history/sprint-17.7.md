# Sprint 17.7

## Objetivo

Adicionar aquisição somente leitura dos modelos digitais associados a pedidos
Cfaz por `digital_models[*].stl_files[*]`.

## Motivação

A Sprint 17.6 comprovou que o portal entregava `id`, `download_url` e
`document_file_name`, mas esses itens não eram convertidos em ativos clínicos.

## Arquitetura afetada

```text
Cfaz request
  → digital_models[]
  → stl_files[]
  → AcquisitionAsset (IDs do modelo/arquivo)
  → download efêmero
  → quarentena + extração segura
  → ClinicalAsset DIGITAL_MODEL
  → 04 - Modelos Digitais
  → clinical_assets
```

## Arquivos alterados

- `src/acquisition/base.py`;
- `src/acquisition/cfaz_provider.py`;
- `src/acquisition/clinical_normalizer.py`;
- `src/acquisition/models/clinical_package.py`;
- `src/radiology/exam_index_service.py`;
- `src/main.py`;
- testes de provider/modelos/índice.

## Comandos criados

```bash
python -m main cfaz-digital-models --request-id ID --dry-run
python -m main cfaz-reprocess --request-id ID \
  --include-digital-models --dry-run
```

## Problemas encontrados

- URL assinada só pode existir em memória;
- download pode ser ZIP/HTML em vez do arquivo esperado;
- identidade individual exige `digital_model_id` e `stl_file_id`;
- formatos de modelo precisam ser reconhecidos por conteúdo;
- homologação real não podia iniciar diretamente com `--apply`.

## Problemas resolvidos

- `AcquisitionAsset` preserva `provider_exam_id` e `provider_asset_id`;
- parser reconhece `digital_models[].stl_files[]`;
- ativos recebem categoria `DIGITAL_MODEL`;
- nomes normalizados usam `modelo_digital_###`;
- `ClinicalPackage` preserva coleção, seção, IDs e origem;
- `clinical_assets` ganhou `digital_model_id` e `stl_file_id`;
- migração SQLite é incremental;
- nenhum endpoint Cfaz de escrita/upload/exclusão foi adicionado.

## Critérios de aceite

- detectar modelos e arquivos;
- HTTPS, domínio permitido, streaming, limites, timeout e SHA-256;
- não persistir URL/query/token/cookie;
- ZIP protegido contra traversal e expansão;
- modelos em pasta/categoria correta;
- idempotência e nenhuma escrita Cfaz;
- suíte completa e `git diff --check`.

Validação histórica da implementação: 492 testes aprovados, 2 ignorados.

## Resultado alcançado

Suporte de leitura e diagnóstico foi implementado. Nenhum reprocessamento real
foi executado naquele checkpoint; homologação produtiva permaneceu pendente.

## Limitações

O caminho efetivo de download e publicação dos dois STL ainda dependia de
sessão/URL efêmera e validação ponta a ponta.

## Próximo Sprint

Sprint 17.8 — Local Reset & Reimport.
