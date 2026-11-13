# CLAUDE.md — spool-sync

Start from `README.md` (how it works, the unofficial Bambu endpoints and their quirks).

- Chat in Russian; code, docs, commits in English. Commit subjects only; push only when asked.
- **Public repo**: no printer serial, spool ids, chat id, tokens or emails in tracked files — they live in
  `.env` / `data/` (gitignored) and in the docker volume.
- Runs on the phil docker VM: `docker` here defaults to the remote context, so `docker compose up -d --build`
  from this directory deploys. Logs: `docker logs spool-sync`; journal: `docker compose run --rm spool-sync log`.
- Writing to Filament Manager touches the user's real inventory — test with `once --dry-run` or the fake
  cloud in `tests/` (`python -m unittest discover -s tests`), not by poking spools.
