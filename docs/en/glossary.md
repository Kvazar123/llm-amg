# AMG bilingual glossary (ru ↔ en)

This glossary pins the terminology used across AMG's two documentation mirrors
(`docs/ru` and `docs/en`, plus the two front-page READMEs). It exists so that a
term is rendered the same way in every file: without a fixed mapping, «сверка»
would drift between *reconciliation* and *sync*, «впитывание» between *absorb*
and *ingest*, and the two mirrors would stop being mirrors.

Rules of use:

- **Bidirectional nativeness.** The English term must be what a native speaker
  would write in technical documentation (never a word-for-word calque from
  Russian), and the Russian term must read as natural Russian technical prose
  (never a calque from English). Both directions are checked when a pair is
  added.
- **Artifact names stay English.** File names, config keys, CLI flags, node
  statuses, and code identifiers (`derived-*.json`, `cache/derivations/`,
  `derivation_cache`, `stale`, `part_of`, …) are never translated; the prose
  around them uses this glossary. On the Russian side, the first mention of a
  term whose artifact name differs keeps the English original in parentheses.
- **Extend on first encounter.** A translator who meets a project term not
  listed here adds the pair on the spot, in the right section, before using it.
- Every file of the English mirror is translated strictly against this table;
  the table is also the checklist for reverse edits (English → Russian).

## Core model

| Russian | English | Note |
|---|---|---|
| ассоциативная память | associative memory | |
| граф знаний | knowledge graph | |
| узел | node | |
| ребро | edge | |
| типизированные взвешенные рёбра | typed weighted edges | |
| энграмма | engram | a whole memory trace, not a neuron |
| контекстное окно | context window | |
| рабочая / эпизодическая / семантическая память | working / episodic / semantic memory | the CLS triad |
| две плоскости | two planes | deterministic code vs model judgment |
| детерминированный слой | deterministic layer | |
| слой суждения | judgment layer | |
| управляющая плоскость | control plane | engine: code, skills, agents, config |
| контентная плоскость | content plane | the project's graph |
| остов (дерево-остов) | spanning tree | the browsing skeleton, `part_of` |
| полииерархия / множественное членство | polyhierarchy / (weighted) multi-membership | |
| членство | membership | the `part_of` relation |
| хаб (узел-концентратор) | hub | |
| обзор | overview | node type |
| узлы-паттерны | pattern nodes | architectural pattern / recurring fix / anti-pattern / migration recipe |
| перенос по аналогии | transfer by analogy | what pattern nodes enable |
| бакет | bucket | physical node directory (`code/ doc/ data/ notes/ _hubs/`) |
| указатель (`путь:строка`) | pointer (`path:line`) | |
| дрейф указателя | pointer drift | lineno refresh without re-derivation |
| контентный хеш | content hash | |
| фильтр по хешу | content-hash filter | skip unchanged units |
| сводка | summary | |
| дистиллят | distillate | what absorb keeps |
| канон / канонический | canon / canonical | markdown as the source of truth |

## Retrieval

| Russian | English | Note |
|---|---|---|
| извлечение | retrieval | |
| распространение активации | spreading activation | |
| засев | seeding | building the teleport vector |
| лексический / семантический засев | lexical / semantic seeding | BM25 / embeddings |
| seed-узел (узел-затравка) | seed node | |
| вектор телепортации | teleport (personalization) vector | |
| коэффициент затухания | damping factor | PPR `d`; distinct from weight decay |
| смещать, а не запирать | bias, don't gate | relevance enters `p`, never `M` |
| многошаговый (multi-hop) | multi-hop | |
| проводимость | conductance | how strongly an edge carries activation |
| приоритет проводимости (β) | relation prior (β) | per edge type, `relation_priors` |
| ярус | tier | |
| стратегический / тактический / оперативный ярус, периферия | strategic / tactical / operational tier, periphery | |
| пакет (контекста) | context pack (the pack) | |
| бюджет токенов | token budget | |
| потолок выдачи | output ceiling | a ceiling per query, not a mandatory load |
| порог активации | activation threshold | |
| жадная укладка | greedy packing | the (1 − 1/e) bound |
| статусный приоритет | status prior | multiplier on final activation |
| пометка (в пакете) | trust flag / pack marking | flag, don't demote |
| помечать, а не понижать | flag, don't demote | |
| намерение запроса | query intent | `--intent history\|conflict` |
| поднятие по намерению | intent-driven surfacing | retired/disputed nodes lifted on ask |
| производный read-индекс | generated read-index | disposable SQLite cache |
| эмбеддинг | embedding | |
| кросс-язычный | cross-lingual | |
| многоязычная модель | multilingual model | |

## Sources and ingest

| Russian | English | Note |
|---|---|---|
| загрузка данных / извлечение структуры | ingest / structure extraction | |
| источник | source | |
| политика (обработки источника) | (source) policy | |
| зеркало / зеркалирование | mirror | live projection |
| впитывание / впитать | absorb | survives source deletion |
| разовый снимок (замороженный) | frozen snapshot | the `absorb_once` policy |
| нарезатель | chunker | |
| единица | unit | what a chunker yields |
| классификатор | classifier | |
| неоднозначные файлы | ambiguous files | |
| информационный домен | information domain | code / doc / data |
| игнорирование / правила игнора | ignore rules | |
| явный источник важнее `.gitignore` | an explicit source beats `.gitignore` | |
| эпизод (журнала) | (log) episode | time-window grouping |
| чат-экспорт | chat export | |
| смежность диалога | conversation adjacency | the `follows` edge |
| ход (диалога) | turn | |
| тред | thread | |
| маркеры ролей | role markers | `=== Human ===` / `=== Assistant ===` |
| вложение | attachment | numbered marker in a dump |
| прогоны непустых строк | runs of non-blank lines | the paragraph chunker's blocks |

## Building the graph

| Russian | English | Note |
|---|---|---|
| сверка | reconciliation (to reconcile) | never "sync" in prose |
| сборка (графа) | build(ing) | |
| структурный скелет | structural skeleton | |
| хребет (принадлежности) | (containment) backbone | the `defines` edges |
| структурные / смысловые рёбра | structural / semantic (meaning-bearing) edges | |
| детерминированное — прежде модели | deterministic before the model | |
| резолвер | resolver | import-table name resolution |
| канонизация целей | target canonicalization | path-suffix repair |
| висячее ребро | dangling edge | inert; dropped at retrieval |
| неверное ребро хуже висячего | a wrong edge is worse than a dangling one | |
| семантическое обогащение | semantic derivation | artifacts keep “derivation”: `derived-*.json`, `cache/derivations/` |
| очередь (обогащения) | (semantic) work queue | `work/queue.json` |
| партия / пачка | batch | ru distinguishes builders' партия / linker's пачка; en uses batch for both |
| чекпоинт / контрольная запись | checkpoint (part) | `derived-*-pNN.json` |
| возобновляемость / возобновлять | resumability / to resume | |
| переотправка (контекста) | (context) resend | the dominant token sink |
| тонкая оркестрация | thin orchestration | aggregates in the orchestrator, paths to workers |
| авто-сводка (тривиальных единиц) | auto-summary (of trivial units) | code writes it, no model |
| тривиальная единица | trivial unit | |
| кэш дериваций (кэш обогащения) | derivation cache | |
| глобальная смысловая линковка | global semantic linking | |
| связыватель / линковщик | linker | the `amg-linker` subagent |
| кандидаты / номинация | candidates / nomination | similarity nominates, judgment confirms |
| якоря хабов | hub anchors | deterministic, from directory structure |
| таксономия | taxonomy | the hub/topic set |
| отчёт о пробелах | gap report | `gap-report.md` |
| недокументированный код | undocumented code | |
| рассинхронизированные документы | drifted docs | |
| связность | connectivity | |
| компонента связности | connected component | |
| узлы-сироты | orphan nodes | |
| приёмочный гейт связности | connectivity acceptance gate | advisory ok / attention |
| воспроизводимая сборка | reproducible build | same input → same graph |
| экономная сборка | economical build | savings from eliminating repeated work |
| ложный «Готово» | a false "Done" | why completion is a verifiable claim |
| завершение — проверяемое утверждение | completion is a verifiable claim | `BATCH COMPLETE\|PARTIAL: N/M` |
| ленивая / жадная деривация | lazy / eager derivation | |
| первое касание (синхронное) | (synchronous) first touch | |
| фоновый добор | background fill | |

## Weights, salience, consolidation

| Russian | English | Note |
|---|---|---|
| консолидация | consolidation | |
| захват | capture | cheap, during the session |
| захват дёшево, отбор позже | capture cheaply, select later | |
| заметка | note | |
| правило Хебба / хеббово обучение | Hebbian rule / Hebbian learning | |
| ко-активация | co-activation | |
| журнал ко-активаций | co-activation log | |
| свёртка весов | weight folding | |
| затухание (весов) | decay | distinct from PPR damping |
| обрезка (рёбер) | pruning | |
| магистрали (проводимости) | (conductance) highways | the rich-get-richer failure |
| усиление по исходу задачи | outcome-gated reinforcement | |
| различающее усиление | discriminative reinforcement | headroom `(1 − w)` |
| провенанс использования | usage provenance | `work/usage.log` |
| значимость | salience | |
| ценность информации | value of information | |
| новизна / (байесовское) удивление | novelty / (Bayesian) surprise | |
| связность / мостовость (сигнал значимости) | bridging | connects clusters |
| заземлённость | groundedness | backed by a source |
| повышение (в долговременную память) | promotion | threshold sits on promotion, not deletion |
| компрессия | compaction | |
| поэтапное сжатие | staged compaction | |
| свернуть эпизоды | summarize episodes | |
| слить почти-дубли | merge near-duplicates | |
| ввести под-хаб | introduce a sub-hub | |
| укоротить с потерями | lossy shorten | last resort, archives first |
| бюджет ветки | branch budget | |
| ветвь / ветка (графа) | branch (of the graph) | a hub's subtree |
| забывание (управляемое, обратимое) | (controlled, reversible) forgetting | |
| архив | archive | |
| защищённые типы | protected types | decisions, ADRs |
| эпизодические типы | episodic types | candidates for folding |
| дайджест | digest | the always-on block |
| eval-предохранитель (автоматическая проверка полноты) | eval guard (automatic recall check) | clone-measured before compaction commits |

## Trust and arbitration

| Russian | English | Note |
|---|---|---|
| слой доверия | trust layer | |
| провенанс | provenance | |
| уверенность | confidence | |
| верификация | verification | |
| проверка перед ответом | verify before answering | |
| иерархия источников | source hierarchy | code > docs > ADR > session > legacy > guess |
| уверенно-ложная память | confidently-wrong memory | worse than none |
| эпистемический арбитраж | epistemic arbitration | |
| вердикт | verdict | supersede / dispute / reject / keep both with context / ask user |
| вытесненный | superseded | |
| спорный | disputed | |
| отклонённый | rejected | |
| недеструктивный (вердикт) | non-destructive (verdict) | status + linking edge, nothing deleted |
| аудит-след | audit trail | `arbitration.md` |
| обнажать, а не разрешать молча | surface, don't resolve silently | |
| проверка свежести по коммиту | source freshness by commit | `verify_claims --by-commit` |

## Storage and consistency

| Russian | English | Note |
|---|---|---|
| хранилище | store | |
| транзакционное хранилище | transactional store | |
| журнал упреждающей записи | write-ahead journal (WAL) | |
| декларативный повтор | declarative redo | the journal holds target state |
| блокировка на запись | writer lock | single writer |
| восстановление | recovery (to recover) | |
| самовосстановление | self-healing | |
| идемпотентность | idempotency | |
| устойчивость к сбоям | crash safety | |
| атомарная запись | atomic write | |
| журнал действий | action log | `actions.log`, txid-deduped, rotated |
| ротация | rotation | |
| раскладка каталогов | directory layout | |
| разрешение корня (хранилища) | store(-root) resolution | |
| чекаут исходников | source checkout | never resolves as a store |

## Lifecycle and control

| Russian | English | Note |
|---|---|---|
| петля активации | activation loop | |
| точка входа | entry point | `CLAUDE.md` / `AGENTS.md` |
| блок активации | activation block | between the AMG markers |
| каталог агента | agent directory (agent dir) | `.claude` is the Claude Code default |
| хуки сессии | session hooks | SessionStart / SessionEnd |
| словесный вызов (триггер) | verbal trigger | fires only when the request names the memory / AMG |
| автоматика | automation | the `automation` key |
| ручной аналог | manual counterpart | every automatic operation has one |
| сохранение сессий | session saving | |
| выгрузка стенограммы | transcript dump | |
| политика сессий | session policy | `session_policy: absorb \| mirror` |
| скилл | skill | |
| субагент | subagent | |
| сборщик | builder | `amg-builder` |
| извлекатель | retriever | `amg-retriever` |
| синтез / синтезатор | synthesis / synth | `amg-synth` |
| консолидатор | consolidator | `amg-consolidator` |
| оркестратор | orchestrator | |
| тиринг моделей | model tiering | the `models` block |
| уровень рассуждения | reasoning effort | |

## Measurement and tooling

| Russian | English | Note |
|---|---|---|
| измерительный стенд | eval harness | |
| размеченные случаи | labeled cases | `cases.json`, `gold_ids` |
| полнота | recall | |
| точность | precision | |
| hop-recall | hop-recall | recall on edge-only-reachable gold |
| полнота переноса | transfer recall | pattern metric |
| доля ложных аналогий | false-analogy rate | pattern metric |
| бенчмарк | benchmark | `bench.py`, speed regression |
| объяснимость | explainability | `--explain`, mass inflow decomposition |
| 3D-просмотр графа | 3D graph viewer | |
| режим большого графа | large-graph mode | hubs-first, expand on click |
| раскраска по кластерам | cluster coloring | |
| сквозной passthrough | pass-through | `viewer.options` |

## Team work and installation

| Russian | English | Note |
|---|---|---|
| командная работа | team work | |
| общая папка | shared folder | |
| граф в git | the graph in git | |
| конфликт-маркеры | merge-conflict markers | |
| осведомлённость о ветке | branch awareness | |
| различение машин (блокировкой) | host-aware (lock) | |
| установщик | installer | `install.py` |
| локальная / глобальная установка | local / global install | |
| переустановка | reinstall | |
| удаление | uninstall | |
| слои конфигурации | configuration layers | global personal defaults under the local config |
| глобальный конфиг личных умолчаний | global personal-defaults config | `~/<agent_dir>/amg/config.yml` |
| переносимость | portability | |
| среда (агентская) | (agent) environment | |
| переносимый блок без скиллов | portable skill-less block | `--env generic` |
| мягкий пропуск (зависимости) | graceful skip | optional deps never crash |
