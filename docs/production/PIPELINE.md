# Единый расчётный pipeline (T09)

Общее ядро inference живёт в `agrocast/forecast/pipeline.py`. Все entrypoints — LIVE API,
очередной worker, CLI (`agrocast backtest/forecast`), аудит и историческая проверка
(`/api/prepare` kind=hindcast → `run_hindcast`) — считают прогноз одним кодом.

## Состав ядра

- `prepare_artifacts(config, point, variables, mode, season_len)` — единственная точка
  загрузки артефактов: blender, терцильные и conformal-калибраторы, режимная климатология,
  skill-map, shrink-контекст из ledger, ospr, станционная калибровка, `stds/raws`.
  Никакого второго «ручного» списка загрузчиков в вызывающих модулях не осталось.
- `predict_target(config, ctx, variable, tgt, sm, lead, issue, first_year)` — подгонка
  model set под as-of из T08 (`until = observation_cutoff − delay`, `span/embargo`),
  blend, калибровки, regime/shrink guards, ospr, конвертация z→физические единицы,
  confidence. Возвращает `block` (тот же контракт payload) и `phys`; в режиме `extra=True`
  дополнительно отдаёт сырые вероятности и число обучающих строк каждой модели — из них
  бэктест строит records.
- `nn_for(ctx, config, variable, first_year)` — ядро NN-стека; кэшируется на контекст.

Бэктест (`backtest/engine.py`) больше не содержит собственного цикла fit/predict: для каждой
точки он вызывает `predict_target`, а строки `backtest_records_*.parquet` — это ровно те же
`preds`, что видит LIVE. Дополнительно движок пишет `backtest_pipeline_<mode>.parquet` —
финальные blend-вероятности и квантили той же конфигурации, что и LIVE-ответ на том же
as-of; `tests/test_pipeline_single.py` сверяет таблицу с ответом API (1e-12) и
`model_probs` ответа с records (после округления до 3 знаков).

Историческая проверка `run_hindcast` больше не пересобирает blend/calibration inline:
она читает walk-forward ledger (T08). Если ledger-файла нет, она вызывает тот же
`build_ledger` по записям бэктеста — один алгоритм, а не вторая копия.

## Политика против JSON

`core/policy.py: nn_alpha_allowed(variable, config)` — NN-альфа для `tp` запрещена, пока
`config.allow_tp_nn_alpha` явно не включена. Проверка стоит на общем inference-пути
(`nn_for`, `load_alpha`) и в аудит-консьюмере, поэтому подкинуть `stack_<mode>_tp.json`
с ненулевой альфой напрямую в artifacts бесполезно: pipeline обнулит её независимо от
содержимого файла.

## Артефакты не грузятся молча

Загрузчики (`core/artifacts.py`) помечены схемами: `blender-v2`, `stack-v1`, `calib-v1`,
`conformal-v1`. Правила:

- файла нет — bootstrap-путь (`Blender.default`, калибраторы отсутствуют) — легально;
- битый JSON или не-объект — `ArtifactError` (раньше молчаливый `except: return 0.0`);
- `schema` не совпал — `ArtifactError`;
- `models` в артефакте содержат модель вне текущего `MODEL_NAMES` — `ArtifactError`;
- у blender-весов неизвестная модель в ключах — `ArtifactError` (в том числе legacy-файлы
  без схемы: ключи проверяются всегда).

`save()` записывает `schema` и `models` (для blender), `load()` проверяет. Это же
используют refit/pipeline-скрипты, так что свежесобранный bundle всегда совместим.

## Приёмка этапа

- Одинаковые P/Q entrypoints: `test_live_api_and_hindcast_return_same_pq` — engine-таблица
  против LIVE-ответа API (атол 1e-12) плюс совпадение `model_probs` ответа с records.
- Monthly leads 1–6 покрывает `Horizon = ge=1, le=6` и `Timing.consistent_mode`
  (seasonal: season_len=3, кратность horizon); непроверенные leads API отклоняет 422 —
  `test_api_rejects_unverified_leads_and_modes`.
- `test_tp_alpha_policy_is_enforced_independently_of_json` — файл с альфой 0.5 для tp не
  меняет P/Q; для t2m — меняет (или NN-ядро честно непригодно на бандле).
- `test_incompatible_artifacts_fail_loud`, `test_corrupt_bundle_blender_is_not_silently_dropped`,
  `test_refit_writes_validated_artifacts` — контракты загрузчиков.
- `test_hindcast_serves_walk_forward_ledger` — ответ проверки совпадает с ledger-строками.

Осознанные границы: единый **построитель** артефактов (blender-пересборка, калибровки
refit) — это вход бэктеста в `serve/pipeline.run_job`/`scripts/refit_world`; они питают
тот же `build_ledger`, отдельной формулы больше нет. Точная воспроизводимость refit на
production-контейнере и least privilege — T13/T14.
