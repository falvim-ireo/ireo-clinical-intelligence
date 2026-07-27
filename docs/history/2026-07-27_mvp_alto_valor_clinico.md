# MVP de alto valor clínico

**Data da decisão:** 27 de julho de 2026
**Natureza:** correção consciente de direcionamento, sem reescrita retroativa
dos registros anteriores.

## Contexto

O projeto construiu uma base técnica relevante para aquisição, normalização,
publicação e consulta de exames. A avaliação de continuidade identificou,
porém, risco de ampliar robustez e arquitetura antes de comprovar o valor
entregue no fluxo interno real. A correção preserva o trabalho existente e
reorienta novas decisões para entregas verticais menores e mensuráveis.

## Decisão

Enquanto o IREO Clinical Intelligence permanecer uma solução de uso exclusivo
do IREO, cada função será construída como um MVP de alto valor clínico: o menor
esforço e a menor complexidade capazes de produzir benefício real,
transformador, perceptível e verificável para a clínica.

MVP não significa protótipo descartável nem autoriza fragilidade. Permanecem
obrigatórios:

- LGPD e minimização de dados;
- proteção de credenciais e segredos;
- associação inequívoca entre paciente, solicitação e exame;
- prevenção de perda, sobrescrita e duplicidade relevante;
- idempotência suficiente para o fluxo utilizado;
- rastreabilidade necessária e falha segura;
- revisão humana antes de consequências clínicas;
- testes sintéticos dos caminhos reais e riscos essenciais.

## Ordem das entregas

1. Concluir a aquisição automatizada de exames de imagem.
2. Validar seu benefício operacional e clínico no IREO.
3. Iniciar o Patient Recall Engine.
4. Expandir robustez somente quando sustentada por risco ou valor demonstrado.

A aquisição automatizada permanece a entrega vertical atual. O Patient Recall
Engine permanece a próxima entrega e não deve começar antes da conclusão
técnica e da avaliação operacional inicial da aquisição.

## Validade

A diretriz vale enquanto o uso permanecer exclusivo do IREO. Escala
hipotética, distribuição comercial, múltiplos workers, alta disponibilidade,
generalização prematura e integrações não utilizadas ficam adiados. A mudança
para produto externo exige decisão formal e nova avaliação de arquitetura,
risco, segurança e conformidade.

Novas tarefas devem passar pelo gate pragmático definido em
`docs/ENGINEERING_MASTER.md`: problema real, beneficiário direto, menor entrega
utilizável, risco concreto, medição de valor e itens explicitamente adiados.
