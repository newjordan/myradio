# File ownership for parallel work

This directory is not a git repository, so ownership is a written rule plus a checker. **One writer per file at any time.** A lane may read anything and may change only the paths listed for it.

## Orchestrator-only (no lane may modify these)

- `myradio/extensions.py`, `station.py`, `server.py`, `mcp.py`, `broadcast.py`, `backup.py`, `importing.py`, `media.py`, `scheduling.py`, `agent_settings.py`, `__init__.py`
- `myradio/chat.py` and `myradio/model_transport.py`: one writer, the deadline-fix agent, until it hands back. Their sha256 values are recorded in `reports/` at hand-back, and only then does anyone else edit `chat.py`.
- `myradio/web/**` (the Midnight UI shell and, later, `web/panels/*`)
- `pyproject.toml`, `Start myradio.command`, `README.md`, `PRODUCT.md`
- `docs/HANDOFF.md`, `docs/RELEASE-STATUS.md`, `docs/DEPLOYMENT.md`, this file
- every test file not listed under a lane below, and `tests/test_agent_deadline.py` in particular (the read-only verifier)

## Lane-owned paths

| Lane | May create or change |
| --- | --- |
| audio | `myradio/audio/**`, `tests/test_audio*.py`, `docs/lanes/audio.md` |
| generation | `myradio/generation/**`, `tests/test_generation*.py`, `docs/lanes/generation.md` |
| shows | `myradio/shows/**`, `tests/test_shows*.py`, `docs/lanes/shows.md` |
| ads | `myradio/ads/**`, `tests/test_ads*.py`, `docs/lanes/ads.md` |
| research | `myradio/research/**`, `tests/test_research*.py`, `docs/lanes/research.md` |
| hosts | `myradio/hosts/**`, `tests/test_hosts*.py`, `docs/lanes/hosts.md` |
| prompts | `myradio/prompts/**`, `tests/test_prompts*.py`, `docs/PROMPTS.md`, `docs/lanes/prompts.md` |
| doctor | `myradio/doctor/**`, `tests/test_doctor*.py`, `docs/lanes/doctor.md` |
| soak | `tests/soak_*.py`, `tests/test_soak*.py`, `reports/soak-*`, `docs/lanes/soak.md` |
| security review | `reports/security-review-*.md` (read-only otherwise) |

A lane that needs a change in an orchestrator-only file records it under "Requested changes to shared files" in its own `docs/lanes/<lane>.md`. It does not make the change.

## How it is enforced

1. Before a fan-out starts, the tree is copied to a snapshot outside the repository and a sha256 manifest is written to `reports/fanout-<n>-manifest.sha256`, in the same style as `reports/handoff-source-manifest.sha256`.
2. After every phase, a checker compares the tree with the snapshot, attributes each changed file to a lane or to the orchestrator, and flags anything that belongs to neither. Flagged changes are restored from the snapshot.
3. Independent reviewers run the same checker for their lane, so a lane that wandered outside its paths is caught by someone other than itself.
4. When a real git repository exists, this file becomes a plain CODEOWNERS-style reference.
