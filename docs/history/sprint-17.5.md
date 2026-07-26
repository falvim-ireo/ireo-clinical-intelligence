# Sprint 17.5

## Objetivo

Impedir que thumbnails genéricos fossem publicados/indexados como ativos
clínicos e garantir classificação individual por asset.

## Motivação

Um pedido Cfaz de homologação apresentava 30 JPEG aparentemente duplicados. A investigação
demonstrou 15 originais + 15 thumbnails, todos com SHA-256 distintos. O defeito
era seleção de ativos, não duplicação de upload.

## Arquitetura afetada

```text
ClinicalPackage
  → ClinicalAssetNormalizer
      ├── ORIGINAL → publicar/indexar
      ├── PREVIEW_CLINICO → publicar quando autorizado
      └── THUMBNAIL → ignorar
  → OneDrive
  → clinical_assets
```

A categoria do pacote não pode sobrescrever a categoria individual do asset.

## Arquivos alterados

- `src/acquisition/clinical_normalizer.py`;
- `src/acquisition/cfaz_provider.py`;
- `src/radiology/exam_index_service.py`;
- `src/main.py`;
- testes de normalização, provider e índice.

## Comandos criados

```bash
python -m main thumbnail-report
python -m main cfaz-reprocess --request-id ID --dry-run
python -m main cfaz-reprocess --request-id ID --apply
```

## Problemas encontrados

- metadados do pedido e coleções baixáveis nem sempre correspondiam;
- `digital_models=1`, `teleradiographies=1` e `reports=1` não significavam que
  tais coleções estavam presentes nos assets entregues;
- rebuild inicialmente revelou divergência de identidade/consulta para um
  paciente;
- importações antigas precisavam ser corrigidas sem remover arquivos remotos.

## Problemas resolvidos

- thumbnails genéricos excluídos de novas normalizações e rebuild;
- preview DICOM/tomográfico pode ser preservado;
- nenhum arquivo antigo foi removido do OneDrive;
- `cfaz-reprocess` permite atualizar manifesto/índice local de forma
  idempotente e com rollback;
- classificação por asset foi preservada.

## Critérios de aceite

- nenhum thumbnail genérico publicado/indexado;
- previews clínicos autorizados preservados;
- rebuild e reprocessamento idempotentes;
- compatibilidade com manifests antigos;
- nenhuma alteração remota durante diagnóstico;
- suíte completa e `git diff --check`.

Validação histórica: 492 testes aprovados, 2 ignorados. O rebuild reduziu 58
registros para 29 ativos clínicos, sem exclusão no OneDrive.

## Resultado alcançado

O diagnóstico confirmou 30 SHA únicos, zero duplicatas reais, 15 originais e
15 thumbnails no pedido sanitizado. O índice passou a representar somente ativos
clinicamente relevantes.

## Limitações

Modelos digitais não estavam presentes no payload persistido. A aquisição
dessas coleções exigiu investigação posterior do portal.

## Próximo Sprint

Sprint 17.6 — descoberta da lacuna de aquisição Cfaz.
