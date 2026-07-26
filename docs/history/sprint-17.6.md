# Sprint 17.6

## Objetivo

Diagnosticar por que modelos digitais e outras coleções indicadas no pedido
Cfaz não chegavam ao payload persistido. A subetapa foi denominada
“17.6B — Cfaz Network Discovery”.

## Motivação

O pipeline não estava descartando STL: ele nunca recebia informação suficiente
para adquiri-los. `request.images_download_links` continha apenas JPEG, embora
o portal mostrasse Modelo Digital.

## Arquitetura afetada

Nenhuma alteração produtiva era objetivo. A investigação ficou na fronteira
externa Cfaz:

```text
Portal Cfaz autenticado
  → chamadas de rede/componentes Vue
  → mapa sanitizado de endpoints/campos
  → futura aquisição somente leitura
```

## Arquivos alterados

- investigação manual via DevTools;
- posteriormente `src/acquisition/cfaz_browser_session.py` e testes preservaram
  mecanismos de sessão/observação, já no estado consolidado.

A parcela exata de código pertencente à descoberta e à implementação posterior
não foi commitada separadamente.

## Comandos criados

Não havia comando produtivo obrigatório. Foram previstos diagnósticos
sanitizados e reutilização de sessão somente quando já autenticada.

## Problemas encontrados

- ausência de `digital_models` no payload principal persistido;
- arquivos do modelo baixados pelo frontend via componentes Vue;
- requisições ao Google Storage iniciadas por JavaScript;
- risco de expor cookie, token, Authorization ou URL assinada;
- distinção entre `DigitalModel` e `PhysicalModel`.

## Problemas resolvidos

A investigação identificou:

- componente/API específica `digitalModel`;
- objeto de modelo com `data.stl_files`;
- campos `id`, `download_url` e `document_file_name`;
- `digitalModelId` e `uploadStlUrl`;
- endpoint de escrita `PUT /digital_models/{id}.js`, explicitamente excluído;
- download_url como informação suficiente para aquisição de leitura.

## Critérios de aceite

- não baixar nem alterar dado clínico durante descoberta;
- sanitizar query, token, cookie, nomes e conteúdo;
- registrar somente método, domínio, path, status, Content-Type e chaves seguras;
- parar se não houver sessão autorizada;
- não tocar OneDrive, SQLite, Clinicorp, Gmail ou pipeline.

## Resultado alcançado

Foi encontrada a evidência técnica que desbloqueou a Sprint 17.7: o frontend
recebe STL em `digital_models[*].stl_files[*]`, com URL de download efêmera.

## Limitações

A descoberta manual não é documentação oficial do Cfaz. Rotas/campos externos
podem mudar e toda aquisição deve continuar fail-closed.

## Próximo Sprint

Sprint 17.7 — Cfaz Digital Model Acquisition.
