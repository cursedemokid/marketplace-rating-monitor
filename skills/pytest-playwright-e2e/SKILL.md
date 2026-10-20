---
name: pytest-playwright-e2e
description: Add, debug, or run browser-based end-to-end tests for Python web applications using pytest and Playwright. Use for pytest E2E work; use a JavaScript Playwright or Cypress workflow for those test stacks.
---

# Python web E2E with pytest and Playwright

Use this skill for tests that exercise a Python web app through its browser UI. First inspect the project's test configuration, fixtures, scripts, and existing E2E runner. Extend the established setup when it is sound; avoid adding a second runner or installing browser dependencies before checking what the project already uses.

## Workflow

1. Identify the user-visible path and the test data it needs. Keep browser E2E checks focused on behavior that matters across the UI and app boundary; leave detailed business-rule combinations to backend tests.
2. Reuse the project's fixtures and lifecycle. Give each test a fresh `BrowserContext` (and page) so cookies, local storage, and tabs do not leak between tests. Use the test framework's context fixture or create and close a context explicitly.
3. Locate controls by role and accessible name, label, placeholder, or an intentional test id. Prefer locators that express what a user sees. Use Playwright's auto-retrying locator assertions and actionability waits; do not add arbitrary sleeps or wait for `networkidle` as a generic readiness check.
4. Perform business actions through the UI: click, fill, select, and submit as a user would. Do not bypass the UI by calling application functions in page JavaScript or directly changing app state to make an interaction pass. API helpers may arrange or clean up isolated test data and verify targeted HTTP authorization boundaries; they must not substitute for the UI business flow under test.
5. Keep the app, database, and external effects isolated. Use test-owned data and resources, and clean up only resources created by the current run. Never point routine E2E tests at a user's main application database, Chrome profile, or real third-party account. Avoid sending real marketplace orders, LLM requests, messages, or other external mutations in routine UI tests; use fakes, local fixtures, or a dedicated safe test integration.
6. Configure failure artifacts to include a Playwright trace, screenshot, and JUnit report where the runner supports them. Inspect the first failure and trace, reproduce it, then fix the cause or the test. Do not mass-retry failures, weaken assertions, or add sleeps to hide flakiness.
7. Run the smallest relevant E2E selection, then the project's broader E2E command if the change affects shared setup. Report the exact command and result. Do not claim a browser test passed if it was not run.

## Authentication and permission boundaries

Use the project's documented test authentication path. A saved `storage_state` file is suitable only when the app's authentication actually persists in browser storage; it does not capture tokens held only in JavaScript memory. Never copy a user's browser profile or production credentials into a test. Keep secrets out of traces, screenshots, and committed fixtures.

The request to write or run a test authorizes only the requested test work. It does not authorize deployment, publishing, production access, or remote state changes. Stop before any such action unless the user explicitly authorized it.

For this repository's isolated runner, setup, and commands, read [the WB Rating Monitor reference](references/wb-rating-monitor.md). For locator choice, action waits, or trace inspection, consult the relevant official Playwright documentation linked there.
