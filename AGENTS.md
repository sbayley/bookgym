# BookGym source

This is the application source repository. Runtime data, production credentials, and private deployment instructions are not included.

- Read `README.md` for the application structure and local setup.
- Preserve existing working-tree changes.
- Run `python -m unittest discover -s tests` after application changes.
- Use temporary databases and mocked external interactions for tests.
- The API and scheduler are separate processes. Do not start a booking worker, provision accounts, or deploy while reviewing or testing source.
- Keep databases, `.env`, browser sessions, captures, and personal records out of commits.

- If `.local/AGENTS.md` exists, read it before operating the application. It contains private deployment and preview instructions. Never commit `.local/`.
