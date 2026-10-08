# Runnable examples

[简体中文](README.zh-CN.md)

Install `darwinagent` in your Python environment. Save the script you need into your project and create `.env` beside it using the [configuration guide](../docs/en/configuration.md). Run the script from that directory.

| Script | Purpose | Run |
| --- | --- | --- |
| [live_demo.py](live_demo.py) | Run the bundled two-round improvement loop | `python live_demo.py` |
| [custom_task.py](custom_task.py) | Run your own maintenance record through the pipeline and score it | `python custom_task.py` |
| [custom_experiment.py](custom_experiment.py) | Optimize your own maintenance task with independent evaluation | `python custom_experiment.py` |

Each live script is self-contained and uses only installed dependencies. All use your configured model, at most 40 HTTP attempts and 1800 seconds. Choose a fresh output directory for an independent experiment. Complete copyable scripts and explanations are in the [quickstart](../docs/en/quickstart.md), [custom task tutorial](../docs/en/custom-tasks.md), and [custom experiment tutorial](../docs/en/custom-experiments.md).

The root `examples/` directory is not included in the installed package. You can copy scripts from the guides without cloning the repository.

[third_domain.py](third_domain.py) is an offline recorded-response example. From a source checkout, run `uv run python examples/third_domain.py`; it sends no model requests.
