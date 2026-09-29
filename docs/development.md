# Development

## Environment

Clone your fork and work on a local branch:

```sh
git clone https://github.com/bpepino/ha-openwebui-agent.git
cd ha-openwebui-agent
git switch -c feature/my-change
```

The protocol/state tests run on Python 3.12+ without Home Assistant. Full HA tests require Linux and Python **3.14.2+** for the pinned HA 2026.6.0. The dev container uses Python 3.14. Do not install Home Assistant into your production instance to run unit tests.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-test.txt
python -m pytest tests/test_client.py tests/test_migration.py tests/test_history.py
python -m ruff check .
python -m ruff format --check .
```

On Windows, use `.venv\Scripts\Activate.ps1`. The `tests/ha` folder skips if HA's test plugin is absent; report that skip explicitly.

For the real Home Assistant suite on Linux/Python 3.14:

```sh
python -m pip install -r requirements.txt -r requirements-ha-test.txt
python -m pytest -q
```

`scripts/setup`, `scripts/lint`, and `scripts/develop` remain available. The development script starts a separate local Home Assistant using `config/configuration.yaml`. Keep credentials and real runtime configuration out of Git.

## Validation

- Ruff lint and formatting cover Python source and tests.
- Mock HTTP tests exercise actual client/state source, with no HA runtime required.
- `tests/ha` uses real Home Assistant fixtures for setup/options, reauth, migration, Assist, diagnostics and unload.
- `.github/workflows/lint.yml` runs the full Linux suite.
- `.github/workflows/validate.yml` retains official hassfest and HACS validation. HACS remote checks need the repository to be published/accessible; a local JSON check is not a substitute.
- For a local hassfest run, use a compatible Home Assistant core checkout and its `python -m script.hassfest --integration-path /absolute/path/to/custom_components/openwebui_conversation` command. Follow that checkout's development dependencies.
- CI configuration is prepared locally only. No workflow, release or remote publication is triggered by this task.

## Protocol probe

The probe does read-only discovery by default. Use environment variables, not command-line secret arguments:

```sh
export OWUI_URL='https://your-openwebui.example'
read -rs OWUI_KEY
export OWUI_KEY
python scripts/probe.py
```

For an explicit end-to-end native-agent test, select a printed model ID and run:

```sh
python scripts/probe.py --model '<discovered-id>' --prompt 'Hello, reply briefly.'
```

That command creates a normal saved chat and enables model-default tools, Memory and Web Search. A prompt can cause real tool actions. It prints only the resulting final text, never the key. It does not upload files or execute model functions locally. The same `OpenWebUIClient` and state manager are used by the integration.

## Contribution boundaries

Keep HA lifecycle/configuration, conversation state and Open WebUI protocol separate. Do not add HA tool executors, caller-provided OpenAI tool definitions, completion retries or provider-direct calls. Add a regression test when changing API behavior and update the documented source revision/compatibility evidence. Live feature tests must be identified separately from mocked tests.
