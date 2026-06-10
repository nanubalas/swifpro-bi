# Database Audit Exports

This folder contains **generated database documentation** for the `swifpro_bi`
application, produced by introspecting the live Django model metadata and the
actual database schema.

## What's here

| File | Contents |
|---|---|
| `database_tables.md` | Full table-by-table data dictionary (every model: table name, columns, types, nullability, defaults, choices, FK/O2O, `on_delete`, related names, constraints/indexes, and business-logic properties/methods). |
| `database_columns.csv` | One row per field across all models (machine-readable). |
| `database_relationships.csv` | All foreign-key / one-to-one / many-to-many edges with `on_delete` and related names. |
| `database_constraints.csv` | Unique-together, indexes, conditional unique constraints, and field-level unique/index flags. |
| `database_feature_coverage.md` | Inferred feature-coverage assessment (complete vs partial/scaffold), with the schema facts behind each judgment. |

## Important notes

- **Generated, documentation only.** These files are exports — they are not read
  by the application and changing them has no runtime effect.
- **Release-candidate snapshot.** They reflect the schema at the current
  release-candidate stage (migrations through `0094`).
- **Facts vs inference.** Tables, columns, types, constraints, and relationships
  are *confirmed* from the ORM/DB. Business purpose, data classification, and the
  complete/partial judgments in `database_feature_coverage.md` are *inferred* and
  marked as such in that file.
- **They may become stale.** Any future migration (new model, field, index, or
  constraint) can make these exports out of date.

## Regenerating

Regenerate after any major schema change (new models, fields, indexes, or
constraints) so the documentation stays accurate. The exports were produced by a
one-off introspection pass over `core` models using Django's model `_meta` API
and `connection.introspection`; re-run an equivalent introspection and replace
the files in this folder.
