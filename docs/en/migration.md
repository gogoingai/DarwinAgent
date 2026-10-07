# Migration

[English](../en/migration.md) · [简体中文](../zh-CN/migration.md) · [README](../../README.md)

DarwinAgent 0.1.0 has a new package identity; earlier historical framework versions are not directly comparable by version number. Both distribution and import name are `darwinagent`; no old-namespace compatibility shim is provided.

1. Install from the current source checkout and use `from darwinagent import ...`; changing imports alone does not migrate a frozen run.
2. Generic model connections use `DARWINAGENT_API_KEY`, `DARWINAGENT_BASE_URL`, and `DARWINAGENT_MODEL`. Historical dataset configuration loads explicitly at the dataset boundary.
3. Create a new output directory and freeze source/assets/model/evaluator identity again. Old runs require their original source and dependencies; do not recompute old locks to bypass identity checks.
4. Tasks implement `DatasetAdapter` and `Evaluator`, register S/F/C/P, and use the common `Pipeline`. Arbitrary task execution callbacks and external agent plugin entry points are not provided.
5. The core wheel excludes root datasets, historical outputs, tests, and third-party environments. Demo resources are packaged.

The [history index](../history/README.md) retains earlier docs and research outputs. Historical scripts remain byte-preserved and may require their original checkout. Start with the current [quickstart](quickstart.md); old docs do not describe the current API.
