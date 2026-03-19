# Почему одновременно используются Postgres и YAML

## Короткий ответ

Потому что в системе одновременно живут два разных типа знания:
- **структурные runtime-факты**, которые удобнее автоматически извлекать из кода, быстро обновлять и запрашивать адресно;
- **человеко-читаемое проектное знание**, которое удобно хранить рядом с кодом и редактировать как обычный текст.

## Что хранится в Postgres

Postgres хранит машинные структурные факты runtime:
- файлы проекта и их hash;
- symbols и их диапазоны;
- graph relations;
- search documents metadata для semantic retrieval.

Это нужно для:
- incremental indexing;
- targeted reindex изменённых файлов;
- быстрых выборок для search/context/apply;
- хранения graph relations и confidence.

## Что хранится в YAML

`knowledge.yaml` хранит человеко-читаемый knowledge-слой:
- title и description;
- keywords;
- requirements и их связи с symbol-ами;
- architecture layers.

Это нужно для:
- удобного чтения и review в git diff;
- ручной правки описаний;
- проектных решений, которые не должны быть скрыты в БД.

## Итог

- **Postgres** — runtime graph/index storage.
- **YAML** — curated project knowledge рядом с кодом.
