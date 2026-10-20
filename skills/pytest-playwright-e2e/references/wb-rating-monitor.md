# WB Rating Monitor E2E

Use this note only when working in the `wb-rating-monitor` repository. The `./dev e2e` command owns the isolated E2E lifecycle:

- It creates a uniquely named database `wb_e2e_<uuid>`, starts the API on a dynamically allocated loopback port, and seeds fake users and data.
- When the run ends, it closes the API and drops only the database it created. Do not add cleanup that can discover or delete other databases.
- E2E tests live in the repository-root `e2e/` directory; their output goes to `outputs/e2e/<run-id>/`. The backend suite under `tests/` runs separately.
- The runner is `scripts/run_e2e.py`. Read the current runner and project config before changing fixtures, dependencies, or lifecycle behavior.

Install and run using the project environment:

```bash
uv sync --group e2e
uv run --group e2e python -m playwright install chromium
./dev e2e
```

Useful selections:

```bash
./dev e2e --headed
./dev e2e -k catalog
```

The app's authentication token is held in JavaScript memory. A Playwright `storage_state` file therefore does not preserve this app's authenticated session. Use the test app's supported login/setup flow instead of assuming cookies or local storage contain the token.

Routine local E2E runs must use the isolated runner and fake data. Do not connect these tests to the main application database, send real Wildberries actions, or make real LLM or messaging requests. This recipe does not authorize GitLab deployment or remote mutations.

## Official Playwright references

- [Playwright test runners, including pytest](https://playwright.dev/python/docs/test-runners)
- [Locators](https://playwright.dev/python/docs/locators)
- [Actionability and auto-waiting](https://playwright.dev/python/docs/actionability)
- [Trace Viewer](https://playwright.dev/python/docs/trace-viewer-intro)
