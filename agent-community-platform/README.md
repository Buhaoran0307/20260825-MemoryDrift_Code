# Experiment Platform — English Edition

Updated 2026-10-26. This package contains the platform runtime and the five retained memory mechanisms: LangMem, A-Mem, Mem0, Hindsight, and MemOS.

## Start

Use Python 3.12 and Git. Linux/WSL background experiment workers also require GNU screen. Use the existing deployment interpreter when updating a deployment.

```bash
python -m pip install -r requirements.txt
python script/platform_server.py --host 127.0.0.1 --port 8780
```

Open http://127.0.0.1:8780/. Copy `.env.example` to `.env` and configure your own endpoints, model names, and credentials before running real experiments. The interface can open without live credentials. A new deployment creates an empty database automatically.

All Python dependencies are in `requirements.txt`; versions and the pinned A-Mem commit are unchanged. Local memory mechanisms may download embedding models on first use. Hosted memory mechanisms need service credentials. The package does not include an interpreter, environment, model cache, credentials, accounts, experimental data, results, or logs.

## Language and experiment compatibility

The interface and operational messages are in English, including when a browser previously selected Chinese. API field names, database schemas, IDs, account scopes, model parameters, tool definitions, and experiment settings remain compatible. New account display names use `Experiment 01`, numbered per provider; existing names and IDs are preserved.

Experimental prompts and multilingual answer-parsing rules retain their original runtime text. In particular, the opt-in attribution probe is not translated: translating a model-facing prompt would change its experimental protocol. No stored experiment, resource revision, memory, or result is rewritten by this update.

The synthetic test credential remains `sk-DEMO_NOT_A_REAL_SECRET`. Configure real service credentials only in your local environment. `PACKAGE_MANIFEST.json` lists package files and SHA-256 hashes.

The separate English input-material package contains five MCP catalogs, three post datasets, and a target mapping. Only unused group display labels are translated in the MCP files; their normalized experiment definitions are verified identical. Post files and target mapping retain their original bytes.
