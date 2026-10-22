# School Auto AI — пакет для разработки ИИ-системы

Самостоятельное техническое задание для **School Resource Autopilot**: системы, которая помогает школьным столовым уменьшать пищевые отходы без ухудшения доступности питания и, после подтверждения экономии, помогает планировать разрешённое использование ресурсов.

**Статус:** здесь находятся промпт, требования и пустые шаблоны данных. Работающей ИИ-системы, обученной модели, реальных измерений и результатов пилота в этом репозитории пока нет.

## Как начать

Если ИИ не умеет открывать GitHub, скопируйте **весь текст [START_HERE_PROMPT.md](START_HERE_PROMPT.md)** в новый чат. Промпт содержит необходимый контекст и не требует доступа к другим файлам.

Если ИИ умеет работать с репозиториями, подключите этот репозиторий и отправьте:

> Прочитай AGENTS.md и START_HERE_PROMPT.md. Разработай систему по этому заданию. Начни с аудита доступного окружения и первого проверяемого этапа. Не выдавай демонстрацию за реальный результат.

## Состав

- [START_HERE_PROMPT.md](START_HERE_PROMPT.md) — главный самодостаточный промпт для ИИ-разработчика.
- [PROJECT_BRIEF.md](PROJECT_BRIEF.md) — смысл проекта, границы и научная задача.
- [docs/ARCHITECTURE_AND_GATES.md](docs/ARCHITECTURE_AND_GATES.md) — рекомендуемая архитектура и условия перехода между этапами.
- [docs/DATA_CONTRACT.md](docs/DATA_CONTRACT.md) — будущие входные данные, единицы, происхождение и валидация.
- [docs/PHASE1.md](docs/PHASE1.md) — отчёт по реализованному этапу 1–2: каркас, валидация, хранилище, метрики, демо, тесты.
- [docs/PHASE3.md](docs/PHASE3.md) — отчёт по этапу 3: базовые прогнозы, хронологический backtest, интервалы, дрейф.
- [docs/PHASE4.md](docs/PHASE4.md) — отчёт по этапу 4: симуляционный оптимизатор, жёсткие ограничения, согласования версий, сравнение стратегий.
- [docs/PHASE5.md](docs/PHASE5.md) — отчёт по этапу 5а: фазы исследования, SHADOW-контур, мониторинг, дизайн оценки.
- [docs/PHASE7.md](docs/PHASE7.md) — сводный отчёт этапа: реальные открытые данные, ML-трек, качество данных, подготовка пилота.
- [docs/PHASE8.md](docs/PHASE8.md) — этап 8: lifelike-год, калиброванный по реальным FI-данным, P2-протокол на нём, read-only дашборд (P3).
- [docs/INTERVENTIONS.md](docs/INTERVENTIONS.md) — каталог вмешательств с классами доказательности (чужие эффекты, переносить только через локальный протокол).
- [docs/AUDIT.md](docs/AUDIT.md) — аудит системы: найденные проблемы, исправления, новый план.
- [docs/QUICKSTART.md](docs/QUICKSTART.md) — быстрый старт для директора школы: 5 шагов, без жаргона.
- [docs/ROADMAP.md](docs/ROADMAP.md) — план развития: ML-трек с экспериментом на реальных данных, интеграция с «Социальный кошелёк», конформальные апгрейды.
- [docs/PHASE6.md](docs/PHASE6.md) — отчёт по этапу 6: детерминированные объяснения, ограниченные read-only инструменты помощника (без LLM).
- [AGENTS.md](AGENTS.md) — обязательные правила для ИИ, работающего в этом репозитории.
- [templates/sessions.csv](templates/sessions.csv) и [templates/observations.csv](templates/observations.csv) — пустые шаблоны, не результаты школы.

## Реализация (этапы 1–2 выполнены)

Ядро — модульный монолит `sra/` на чистой стандартной библиотеке Python 3.11
(без сторонних зависимостей; целевой стек FastAPI/PostgreSQL/React остаётся
ориентиром для этапа эксплуатации и требует отдельного разрешения на установку).

```bash
python3 -m unittest discover -s tests          # 343 теста (и без, и со sklearn)
python3 -m sra gen-calibrated --dir data/demo-calibrated --seed 2026
                                               # откалиброванный SYNTHETIC v2:
                                               # порции по нормам РК, доли отходов
                                               # по опубликованным исследованиям
                                               # (манифест происхождения каждого
                                               # параметра); см. docs/OPEN_DATA_PLAN.md
python3 -m sra demo --dir data/demo --seed 42  # изолированное синтетическое демо + backtest прогнозов
python3 -m sra import-open --db data/open/open.sqlite3 --environment open \
    --dataset finland --file data/incoming/fi.xlsx
                                               # чужие открытые данные (Zenodo)
                                               # в изолированную OPEN-среду
python3 -m sra eval-compare --db data/open/open.sqlite3 --environment OPEN \
    --school-id FI-ZEN-20825233
                                               # сравнение фаз чужого вмешательства
                                               # (разведочно; причинность не заявляется)
python3 -m sra forecast-diners --db data/open/open.sqlite3 --environment OPEN \
    --school-id FI-ZEN-20825233
                                               # backtest прогноза едоков:
                                               # базовые правила vs ridge vs GBDT
                                               # (ML: scripts/setup-ml.sh)
python3 -m sra forecast-composite --db data/open/open.sqlite3 --environment OPEN \
    --school-id FI-ZEN-20825233
                                               # композит (едоки × г/едока) vs
                                               # прямой прогноз + стоимость решения
python3 -m sra import-map --db data/open/open.sqlite3 --environment OPEN \
    --file выгрузка.csv --map карта.json       # любой внешний CSV («Соцкошелёк»
                                               # и др.) по явной карте колонок
python3 -m sra quality-check --db data/open/open.sqlite3 --environment OPEN \
    --school-id FI-ZEN-20825233 --file "Food waste data school Finland 2024-2025.xlsx"
                                               # качество данных + кросс-чек
                                               # с первоисточником
python3 -m sra alerts --db data/open/open.sqlite3 --environment OPEN \
    --school-id LV-ZEN-18441715-S1             # несъедаемость vs порог
                                               # нацстандарта РК (30%)
python3 -m sra pilot-package --out docs/pilot-package
                                               # документы пилота для школы КЗ:
                                               # согласие, протокол измерений,
                                               # чек-лист ворот, план эффекта
python3 -m sra forecast --db data/demo/demo.sqlite3 --environment demo \
  --school-id SCH-DEMO-001                     # backtest базовых прогнозов (этап 3)
python3 -m sra optimize --db data/demo/demo.sqlite3 --environment demo \
  --school-id SCH-DEMO-001 --target-date 2026-11-24 \
  --policy-id POLICY-DEMO-OPT-001              # симуляционное предложение объёма (этап 4)
python3 -m sra --help                          # все команды (init-db, import, metrics, verify,
                                               # revise, policies, decide, forecast, optimize,
                                               # compare, phase, shadow-run, monitor,
                                               # design, explain, assistant-tools,
                                               # assistant-call, selfcheck, audit)
python3 -m sra explain --db data/demo/demo.sqlite3 --environment demo \
  --school-id SCH-DEMO-001                     # русский отчёт-объяснение (без LLM)
python3 -m sra shadow-run --db data/demo/demo.sqlite3 --environment demo \
  --school-id SCH-DEMO-001 --policy-id POLICY-DEMO-OPT-001 \
  --days 10                                    # SHADOW-контур без исполнения (этап 5а)
```

Статус: каркас, демо, прогнозы, симуляционный оптимизатор, SHADOW-контур и
объясняющий слой, откалиброванный генератор, импорт чужих открытых данных
(OPEN-среда), сравнение фаз, генератор пакета пилота, прогноз едоков с ML-кандидатами, контроль качества, KZ-календарь,
алерты питания, композитный прогноз, импорт по карте колонок,
ML-трек в демо проверены автотестами (343); реальные открытые
датасеты Zenodo (Финляндия 155 дней, Латвия 4 школы) импортированы;
см. docs/OPEN_DATA_PLAN.md. Реальных данных школы КЗ нет
данных, обученных моделей, нормативов и одобрений в проекте НЕТ; при нехватке
данных прогноз возвращает DATA_NOT_READY; реальное вмешательство заблокировано
по построению (решение по умолчанию NO_CHANGE). Подробности — в
[docs/PHASE1.md](docs/PHASE1.md) и [docs/PHASE3.md](docs/PHASE3.md).

## Основной принцип

Измерения → проверка качества → прогноз с неопределённостью → ограниченная рекомендация → одобрение человека → наблюдение результата → проверка эффекта.

Языковая модель объясняет результаты. Расчёты, ограничения, права и подтверждение экономии контролируются проверяемым кодом.

Демо, техническая готовность и доказанная эффективность в реальной школе — три разных статуса. Отсутствие данных не мешает разработать и протестировать программный каркас; оно мешает честно заявлять о качестве модели и реальном эффекте.
