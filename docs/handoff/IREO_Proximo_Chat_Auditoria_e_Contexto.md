# IREO Clinical Intelligence

## Auditoria documental, estado verificável e contexto para o próximo chat

**Data da consolidação:** 26 de julho de 2026
**Finalidade:** permitir que um novo chat compreenda o projeto, confronte documentação e repositório antes de alterar código e continue o desenvolvimento sem perder o objetivo pragmático de obtenção, validação e uso dos dados clínicos.
**Classificação do documento:** sanitizado para uso como contexto. Não contém nomes de pacientes, identificadores clínicos reais, credenciais, URLs assinadas nem valores de tokens.

---

## Diretriz vigente desde 27 de julho de 2026

Qualquer auditoria, chat ou ciclo de Codex deve conhecer primeiro o
[princípio canônico de MVP de alto valor clínico](../ENGINEERING_MASTER.md#15-princípio-obrigatório-de-mvp-de-alto-valor-clínico).
Ele exige a menor entrega utilizável com valor real no IREO e preserva
integralmente segurança essencial, LGPD, integridade, associação inequívoca,
idempotência necessária e revisão humana.

A sequência obrigatória é:

1. concluir a aquisição automatizada de exames de imagem;
2. validar seu benefício operacional e clínico no IREO;
3. iniciar o Patient Recall Engine;
4. ampliar robustez somente com risco, necessidade ou valor demonstrado.

O escopo não deve ser expandido automaticamente para requisitos de produto
comercial. Antes de aceitar qualquer novo comando, aplique o gate pragmático da
fonte canônica: problema atual, beneficiário direto, menor implementação,
risco concreto, medição de valor e itens adiados. As missões históricas
descritas abaixo ficam subordinadas a essa diretriz.

---

## 1. Veredito executivo

O projeto IREO Clinical Intelligence apresenta avanço técnico real e uma base documental substancial. Não é um protótipo vazio: há uma arquitetura coerente, ingestão de múltiplas fontes, persistência local, busca, timeline, sumário, painel, controle de idempotência, testes e evolução versionada descrita.

Entretanto, os documentos disponíveis ainda não formam uma fonte única de verdade confiável. Eles misturam:

- o MVP histórico da linha 1.0;
- implementações posteriores das linhas 16.x e 17.x;
- resultados observados manualmente;
- funcionalidades descritas, mas não homologadas com dados reais;
- estados locais, commits e tags que precisam ser confirmados diretamente no Git;
- exemplos operacionais que devem ser substituídos por marcadores sanitizados.

O segundo chat não ficou totalmente improdutivo. Ele comprovou que o fluxo real de modelos digitais conseguia produzir dois arquivos STL válidos em execuções manualmente assistidas e ajudou a revelar o comportamento real da interface e da sessão autenticada. O problema foi não converter essa evidência imediatamente em uma automação vertical mínima e repetível. O trabalho se dispersou em reset, reimportação, inventário, identidade, novos comandos, persistência e documentação antes de fechar o único elo crítico: dois acionamentos automáticos, dois downloads válidos e nenhuma intervenção humana.

Por isso, o próximo chat deve começar com uma auditoria somente leitura do repositório. Depois, deve executar uma única missão funcional: homologar a automação isolada de download dos modelos digitais. Integração com banco, manifestos, OneDrive, reset e reprocessamento só deve ocorrer depois dessa homologação.

---

## 2. Objetivo invariável do projeto

O IREO Clinical Intelligence existe para transformar dados clínicos odontológicos provenientes de sistemas externos em um acervo local:

- obtido de forma reproduzível;
- validado quanto a integridade e origem;
- associado corretamente ao paciente e ao atendimento;
- pesquisável e observável;
- idempotente;
- utilizável no fluxo clínico e operacional;
- protegido contra exposição indevida de dados pessoais, clínicos e credenciais.

Qualquer atividade que não reduza diretamente um risco dessa cadeia ou não entregue uma capacidade verificável deve ser tratada como secundária.

O critério de avanço não é a quantidade de módulos, comandos ou documentação criada. É a existência de um fluxo vertical verificável entre fonte, aquisição, validação, persistência, indexação e uso.

---

## 3. Hierarquia de evidências

O próximo chat não deve assumir que um documento está correto apenas por ser detalhado. Para cada alegação, use a seguinte precedência:

1. **Estado atual do repositório:** arquivos realmente presentes, `git status`, commit atual, tags e diferenças locais.
2. **Teste executado agora:** resultado reproduzível no ambiente atual.
3. **Homologação real registrada:** evidência de execução contra a fonte real, sem expor dados sensíveis.
4. **Commit ou tag identificável:** comprova que o conteúdo foi versionado, mas não que continua funcional.
5. **Documentação técnica e de sprint:** registra intenção, arquitetura e resultados históricos.
6. **Histórico bruto dos chats:** serve para reconstruir decisões, erros e hipóteses; não deve funcionar como especificação atual.

### Vocabulário obrigatório de status

Use apenas classificações verificáveis:

| Status | Significado |
|---|---|
| `DOCUMENTADO` | Existe apenas nos documentos ou no histórico. |
| `COMMITADO` | Foi localizado no código do commit atual ou em commit identificado. |
| `TESTADO_AGORA` | Passou em teste executado na sessão atual. |
| `HOMOLOGADO_REAL` | Foi validado contra a fonte real, com critério de aceite explícito. |
| `OPERACIONAL` | Além de homologado, está integrado ao fluxo normal com observabilidade e recuperação de falhas. |

Percentuais subjetivos de prontidão não devem ser usados como substitutos desses estados.

---

## 4. Estado consolidado que pode ser usado como ponto de partida

### 4.1 Capacidades com evidência histórica forte

- Fundação do projeto e configuração de ambiente.
- Integrações de ingestão associadas a Clinicorp, Gmail e TransferNow no ciclo histórico do MVP.
- Persistência local e controle de idempotência.
- Modelo `ClinicalPackage` e ativos clínicos.
- Timeline, busca, sumário e painel operacional documentados nas linhas posteriores.
- Captura, filtragem e reconstrução de ativos JPEG/DICOM, com resultados registrados em sprints.
- Implementação de reset operacional e descoberta de uma segunda camada de idempotência.
- Estrutura de testes automatizados ampla, embora a contagem e o resultado atuais precisem ser refeitos.
- Documentação específica de arquitetura, observabilidade, guia de uso e histórico de sprints.

Esses itens devem ser confirmados no commit atual antes de receberem status superior a `DOCUMENTADO`.

### 4.2 Evidência funcional mais importante produzida no segundo chat

No fluxo Cfaz de modelos digitais:

- a interface real foi acessada em sessão autenticada;
- dois downloads foram acionados em execuções assistidas;
- os arquivos resultantes foram reconhecidos como pacotes válidos;
- os conteúdos STL foram extraídos e validados;
- o resultado foi repetido, mostrando que a aquisição não era um acidente isolado.

Isso comprova viabilidade técnica, mas ainda não comprova automação autônoma.

### 4.3 Estado que não deve ser declarado como concluído

- Automação integral e autônoma dos dois downloads de modelos digitais.
- Integração desse download ao pipeline clínico completo.
- Idempotência homologada de ponta a ponta após reset e reimportação.
- Associação clínica definitiva de cada STL a maxila ou mandíbula quando o provedor não oferece metadado autoritativo.
- Estado atual dos commits, tags, arquivos modificados e suíte de testes.
- Segurança de todo o histórico Git.
- Prontidão de implantação, porque o arquivo de deployment mostrado no repositório está vazio.

---

## 5. Por que o segundo chat não avançou como esperado

### 5.1 Diagnóstico central

O chat recebeu um problema local — automatizar dois downloads já demonstrados manualmente — sem uma hierarquia clara entre objetivo clínico, evidência histórica e tarefas técnicas. Como consequência, passou a tratar cada dificuldade encontrada como uma nova frente de arquitetura.

O movimento predominante foi lateral:

- novos comandos;
- reset;
- inventário;
- identidade;
- reimportação;
- coleção;
- persistência;
- revisão de documentação.

O movimento necessário era vertical:

1. abrir a página correta em sessão autenticada;
2. localizar o atendimento de teste;
3. abrir a seção de modelos;
4. acionar automaticamente os dois controles de download;
5. aguardar dois artefatos distintos;
6. validar ZIP e STL;
7. encerrar sem escrita clínica.

Sem esse corte, o chat acumulou componentes sem fechar o caminho crítico.

### 5.2 Pontos positivos do segundo chat

- Produziu evidência real de que os dois modelos poderiam ser obtidos.
- Identificou que o payload observado no caso real não entregava necessariamente os nomes e URLs individuais esperados pela documentação.
- Confirmou a importância da sessão autenticada e da interação com a interface.
- Validou arquivos obtidos, em vez de considerar o download concluído apenas pela presença de um arquivo.
- Revelou limitações de idempotência e a necessidade de separar reset de reprocessamento.
- Manteve preocupação com segurança e evitou transformar inferências clínicas em fatos.

### 5.3 Pontos negativos do segundo chat

- Não recebeu uma fonte única de verdade curta e atualizada.
- Tratou documentação histórica como se representasse sempre o código e a API atuais.
- Criou ou ampliou superfícies antes de homologar o elo crítico.
- Misturou automação de interface, domínio clínico, persistência e operação na mesma etapa.
- Não definiu um critério de aceite binário para o download autônomo.
- Não impôs limite de escopo quando apareceram problemas adjacentes.
- Produziu progresso técnico, mas não uma entrega funcional vertical.
- Deixou ambígua a diferença entre “implementado”, “testado”, “observado manualmente” e “homologado”.

---

## 6. Auditoria dos documentos versionados

| Documento | Valor atual | Problema principal | Ação recomendada |
|---|---|---|---|
| `README.md` | Boa introdução histórica ao MVP | Apresenta limitações antigas como estado atual e entra em conflito com capacidades posteriores | Reescrever uma seção curta de “estado atual”; mover a narrativa 1.0 para “histórico” |
| `ROADMAP.md` | Lista prioridades e entregas | Repete como futuro itens descritos em outros arquivos como implementados | Vincular cada item a status verificável e critério de aceite |
| `CHANGELOG.md` | Útil para separar versões | Limitações antigas podem ser interpretadas como atuais fora do contexto da versão | Manter, mas marcar claramente cada limitação como histórica |
| `ENGINEERING_MASTER.md` | Documento mais completo | Contém contradições entre commit de referência, mudanças locais e posterior versionamento; mistura estado, intenção e hipótese | Corrigir após auditoria do Git; não tratá-lo ainda como fonte absoluta |
| `docs/history/README.md` | Define boa disciplina documental | Não elimina divergências já presentes nos outros arquivos | Manter e aplicar sua hierarquia de fontes |
| Sprints 16.1–16.3 | Boa rastreabilidade histórica | Alguns resultados são históricos e não representam a árvore atual | Preservar como evidência histórica |
| Sprints 17.1–17.4 | Documentam decomposição de uma entrega maior | Várias não têm commit dedicado | Vincular a arquivos e testes do commit consolidado |
| Sprint 17.5 | Registra deduplicação e reconstrução de imagens | Resultado deve ser marcado como execução histórica | Manter, com data, ambiente e critério |
| Sprints 17.6–17.7 | Estruturam modelos digitais | O contrato esperado de `stl_files` diverge do payload observado no teste real do segundo chat | Separar “contrato esperado” de “payload observado” |
| Sprint 17.8 | Registra reset e a pendência do E2E | O escopo de próxima etapa ficou amplo demais | Substituir por homologação isolada antes da integração |
| `ARCHITECTURE.md` | Excelente visão estrutural | Mistura arquitetura histórica v1.0 e arquitetura posterior; apresenta contratos não confirmados | Marcar seções por versão e atualizar o contrato real |
| `USER_GUIDE.md` | Útil para operação | Mistura eras do sistema e contém identificadores numéricos literais, remetente de serviço e nome operacional de raiz | Substituir por marcadores e separar guias por versão |
| `OBSERVABILITY.md` | Documento forte e prudente | Retenção ainda aparece como pendência | Manter; transformar retenção em decisão formal |
| `DEPLOYMENT.md` | Deveria orientar implantação | Aparece vazio na árvore mostrada | Preencher antes de declarar prontidão operacional |
| `API.md` | Existe na árvore mostrada | Não foi disponibilizado nesta auditoria | Anexar na próxima rodada de auditoria |

---

## 7. Contradições que precisam ser resolvidas antes de alterar a arquitetura

### P0 — estado Git desconhecido

O documento mestre apresenta um commit antigo como referência e menciona mudanças locais não versionadas, mas em outro trecho declara que a linha 17.x foi consolidada em commit e tag posteriores. Não é possível determinar o estado atual apenas pela documentação.

**Correção:** obter do repositório real o commit atual, o status, as tags e as diferenças locais. Atualizar o cabeçalho do documento mestre somente depois disso.

### P0 — contrato de modelos digitais

Os documentos de arquitetura e das sprints descrevem objetos STL com nome individual e URL de download. No caso real analisado no segundo chat, esses atributos não estavam necessariamente presentes; os downloads foram revelados por ações autenticadas na interface.

**Correção:** documentar separadamente:

- payload real observado;
- seletor ou ação da interface;
- resposta de download observada;
- metadados efetivamente confiáveis;
- campos desejados, mas ainda não fornecidos.

Não fabricar URL, nome ou classificação anatômica.

### P0 — inferência anatômica insegura

Alguns exemplos usam nomes equivalentes a maxila e mandíbula, enquanto a própria documentação reconhece que nome e ordem dos arquivos não provam a arcada.

**Correção:** usar nomes neutros e determinísticos até existir metadado autoritativo do provedor. A classificação anatômica deve ficar como desconhecida ou exigir revisão humana.

### P1 — README e roadmap defasados

O README descreve a ausência de DICOM e painel, enquanto documentos posteriores descrevem essas capacidades. O roadmap volta a listar itens já descritos como concluídos.

**Correção:** separar “MVP 1.0 histórico” de “estado atual verificado”.

### P1 — mistura de versões na arquitetura e no guia

Há referências a schema e arquitetura do MVP ao lado de comandos e capacidades posteriores.

**Correção:** marcar cada seção como histórica ou atual e declarar a versão do schema correspondente.

### P1 — contagem de testes e comandos

Os documentos registram contagens diferentes de testes e um conjunto amplo de comandos. Essas contagens podem ter causas legítimas, como parametrização, mas não devem ser usadas como evidência atual.

**Correção:** executar a suíte atual e enumerar a CLI diretamente do código.

### P2 — índice e evolução de schema

O documento mestre reconhece campos novos com versão de índice aparentemente inalterada.

**Correção:** decidir formalmente se a mudança exige incremento de versão, migração ou reconstrução.

---

## 8. Auditoria de sanitização e segurança

### 8.1 Resultado desta revisão documental

Não foram encontrados nos documentos Markdown disponibilizados:

- nomes identificáveis de pacientes;
- CPF claramente reconhecível em contexto pessoal;
- e-mails pessoais;
- tokens ou segredos com valores utilizáveis;
- URLs assinadas completas;
- caminhos locais contendo nome de usuário pessoal;
- conteúdo clínico individual.

Foram encontrados itens que devem ser sanitizados antes de servir como contexto:

- identificadores numéricos literais em exemplos do guia;
- endereço de remetente de um serviço externo;
- nome operacional literal de uma raiz de armazenamento;
- referências a variáveis de autenticação, sem valores;
- hashes de commits, que são aceitáveis e não constituem segredo.

Também há registro documental de que arquivos locais de credenciais foram incluídos em um commit antigo e removidos em commit posterior. Remover o arquivo de um commit posterior não remove o conteúdo do histórico Git.

### 8.2 Tratamento obrigatório do incidente histórico

O próximo chat deve verificar, sem imprimir valores:

- se as credenciais expostas foram revogadas ou rotacionadas;
- se o repositório tem acesso restrito;
- se o histórico completo ainda contém segredos;
- se é necessária reescrita de histórico;
- se `.gitignore` bloqueia arquivos locais, bancos e artefatos de sessão.

Qualquer relatório deve mostrar apenas:

- categoria do achado;
- arquivo;
- número da linha, quando seguro;
- commit;
- ação recomendada.

Nunca deve reproduzir o valor encontrado.

### 8.3 Pasta `data/`

A captura de tela mostra uma pasta `data/` na raiz do projeto. Ela não deve ser anexada ao chat. Antes de qualquer envio ou publicação, verificar se contém:

- bancos SQLite;
- arquivos WAL ou SHM;
- caches;
- JSON de respostas;
- downloads;
- sumários;
- filas de revisão;
- dados de pacientes;
- identificadores de atendimento.

É obrigatório confirmar se algum arquivo dessa pasta está rastreado pelo Git.

### 8.4 Históricos brutos dos chats

Os dois arquivos Pages originais não devem ser fornecidos ao próximo chat como material normal de trabalho. Eles são extensos, criam ruído decisório e podem conter dados operacionais ou clínicos não sanitizados.

Use:

- este documento consolidado;
- os documentos técnicos sanitizados;
- trechos específicos do histórico apenas quando necessários, já redigidos e sem identificadores.

---

## 9. Limites desta auditoria

Esta análise confrontou os dois históricos de conversa com os documentos fornecidos e com a árvore mostrada na captura de tela. Ela não teve acesso, nesta etapa, a:

- árvore completa do código-fonte;
- `.gitignore`;
- `.env.example`;
- `pyproject.toml` ou arquivo equivalente de dependências;
- `API.md`;
- conteúdo da pasta `data/`;
- histórico Git executado diretamente;
- saída atual da suíte de testes;
- saída atual de ajuda da CLI.

Portanto:

- nenhum commit deve ser considerado confirmado apenas porque aparece na documentação;
- nenhuma função deve ser considerada homologada apenas porque possui módulo, comando ou teste;
- a auditoria de segredos do Git ainda está pendente;
- o estado atual do projeto deve ser reconstituído no primeiro turno do próximo chat.

---

## 10. Materiais corretos para abrir o próximo chat

### Fornecer inicialmente

1. Este arquivo.
2. A árvore atual do repositório ou acesso direto ao diretório Git.
3. `ENGINEERING_MASTER.md`.
4. `ARCHITECTURE.md`.
5. `README.md`, `ROADMAP.md` e `CHANGELOG.md`.
6. Sprints 17.6, 17.7 e 17.8.
7. `OBSERVABILITY.md`.
8. `API.md`, `.gitignore`, `.env.example` e arquivo principal de dependências, depois de sanitizados.

### Não fornecer

- arquivos Pages brutos dos históricos;
- pasta `data/`;
- `.env`;
- bancos ou arquivos de sessão;
- credenciais;
- cookies;
- downloads reais;
- manifestos contendo identificadores;
- logs não redigidos;
- URLs assinadas.

### Fornecer apenas sob demanda

- sprints antigas;
- trechos sanitizados dos históricos;
- capturas de tela sem dados identificáveis;
- logs redigidos de uma falha específica.

---

## 11. Primeira fase obrigatória do próximo chat: auditoria somente leitura

Antes de editar qualquer arquivo, o chat deve:

1. Ler este documento e os arquivos técnicos prioritários.
2. Inspecionar o repositório real.
3. Informar o commit atual, a branch, as tags relevantes e se há mudanças locais.
4. Confrontar documentação e código.
5. Enumerar comandos existentes diretamente da CLI.
6. Executar testes seguros e locais.
7. Verificar arquivos rastreados potencialmente sensíveis.
8. Auditar o histórico Git com redaction, sem mostrar valores.
9. Produzir uma tabela de discrepâncias.
10. Propor a menor correção necessária para iniciar a homologação isolada.

Não são autorizados nessa fase:

- `--apply`;
- acesso a Gmail, Graph, Cfaz ou outra fonte externa;
- reset;
- reimportação;
- escrita em banco clínico;
- envio a OneDrive;
- alteração de arquitetura;
- criação de novos comandos;
- commit ou push.

### Evidências Git mínimas

O chat deve obter de modo seguro:

- status resumido;
- log recente com branches e tags;
- tags existentes;
- diferenças pendentes;
- lista de arquivos rastreados;
- confirmação de que `data/`, bancos, ambientes e credenciais não estão rastreados.

Na busca por segredos, usar ferramenta com saída redigida. Se só houver busca textual, o chat deve transformar o resultado antes de exibi-lo e nunca imprimir o lado direito de uma atribuição sensível.

---

## 12. Única missão funcional depois da auditoria

### Missão

Homologar o comando ou função isolada responsável por baixar os modelos digitais do Cfaz.

### Fora do escopo

- reset;
- reimportação;
- persistência clínica;
- manifestos;
- indexação;
- upload para OneDrive;
- alteração de identidade;
- classificação maxila/mandíbula;
- criação de comando adicional;
- refatoração ampla;
- atualização geral da documentação.

### Critério de aceite

A execução deve:

1. reutilizar ou abrir automaticamente uma sessão autenticada autorizada;
2. localizar o atendimento de teste por marcador sanitizado;
3. abrir a seção correta de modelos digitais;
4. acionar automaticamente os dois downloads, sem clique humano;
5. produzir exatamente dois artefatos de download esperados;
6. confirmar que os artefatos são distintos;
7. validar o contêiner e pelo menos um STL válido em cada artefato;
8. registrar apenas metadados técnicos sanitizados, como tamanho, hash e contagem;
9. não gravar banco, manifesto, índice ou armazenamento remoto;
10. encerrar com código de sucesso somente se todos os critérios forem atendidos.

Se a execução falhar, a correção deve permanecer no componente que falhou: sessão, navegação, seletor, evento de download, espera ou validação. Não abrir outra frente.

### Depois da homologação

Somente após obter duas execuções autônomas consecutivas com sucesso:

1. registrar a evidência sanitizada;
2. classificar a função como `HOMOLOGADO_REAL`;
3. propor, em etapa separada, sua integração ao pipeline;
4. definir idempotência, persistência e recuperação de falha;
5. atualizar documentação e versão.

---

## 13. Texto pronto para abrir o próximo chat

Copie o texto abaixo e anexe primeiro este documento e os arquivos indicados na seção 10.

> Estou dando continuidade ao projeto IREO Clinical Intelligence.
>
> Leia integralmente `IREO_Proximo_Chat_Auditoria_e_Contexto.md` antes de agir. Ele é a camada de contexto sanitizada que confronta os dois chats anteriores com a documentação do repositório. Os históricos Pages brutos não são fonte de verdade e não devem ser solicitados nem reutilizados sem necessidade específica.
>
> Seu primeiro trabalho é somente leitura. Não altere código, não crie arquivos, não faça commit, não use `--apply` e não acesse fontes externas. Inspecione o repositório real e confronte-o com `ENGINEERING_MASTER.md`, `ARCHITECTURE.md`, `README.md`, `ROADMAP.md`, `CHANGELOG.md`, `OBSERVABILITY.md` e as sprints 17.6–17.8.
>
> Entregue primeiro:
>
> 1. commit, branch, tags e estado atual do working tree;
> 2. tabela de discrepâncias entre código, Git e documentação;
> 3. relação dos comandos realmente existentes;
> 4. resultado atual dos testes locais seguros;
> 5. auditoria sanitizada de arquivos sensíveis no estado atual e no histórico Git, sem reproduzir valores;
> 6. classificação de cada capacidade como `DOCUMENTADO`, `COMMITADO`, `TESTADO_AGORA`, `HOMOLOGADO_REAL` ou `OPERACIONAL`;
> 7. confirmação do menor ponto de alteração necessário para homologar o download isolado dos modelos.
>
> Trate qualquer dado não sanitizado de forma segura: não mostre nomes, identificadores clínicos, CPF, e-mails pessoais, cookies, tokens, segredos, URLs assinadas, caminhos pessoais ou conteúdo de prontuário. Nos relatórios, use apenas categoria, arquivo, linha ou commit e ação recomendada. Use marcadores como `[REQUEST_ID_TESTE]` e `[PACIENTE_TESTE]`.
>
> Depois que eu aprovar a auditoria, a única missão funcional será homologar a automação isolada de download de dois modelos digitais no Cfaz, sem reset, reimportação, banco, manifestos, índice ou OneDrive. O aceite exige dois downloads automáticos distintos, sem interação humana, ambos com ZIP/STL válidos e duas execuções consecutivas bem-sucedidas.
>
> Se surgir um problema adjacente, registre-o como pendência e continue focado no elo que impede esse critério de aceite. Não crie novos comandos nem altere a arquitetura sem demonstrar que a mudança é indispensável.

---

## 14. Decisões que o novo chat não pode reinterpretar

- O foco é obtenção e uso confiável dos dados, não expansão indefinida da arquitetura.
- Documento não substitui código, teste nem homologação real.
- O histórico bruto é evidência secundária e pode conter dados inadequados para compartilhamento.
- Nenhuma arcada deve ser inferida pelo nome ou ordem do STL.
- Nenhum segredo deve ser reproduzido, mesmo para provar que foi encontrado.
- Remoção de credencial em commit posterior não limpa o histórico.
- A pasta `data/` não deve ser anexada nem publicada.
- A automação isolada deve ser homologada antes da integração.
- Uma falha localizada não autoriza refatoração ampla.
- A próxima etapa só termina quando o critério de aceite estiver comprovado.

---

## 15. Resultado esperado desta mudança de método

Com este contexto, o próximo chat deixa de reconstruir o projeto por tentativa e passa a trabalhar em três camadas explícitas:

1. **verdade do repositório**, verificada agora;
2. **história e intenção**, preservadas nos documentos;
3. **evidência clínica-operacional**, obtida por homologação controlada.

Essa separação evita que documentação defasada gere código novo, que uma descoberta lateral altere o objetivo e que uma execução manualmente assistida seja confundida com automação concluída.
