# my first agent.md for project gemini_api
gemini_api/
├── AGENTS.md          ← 项目级 AI 总规则
├── README.md
├── scheduled_runner_v1.3.py
├── custom_strategy_v1.py
├── utils/
├── tests/
└── .vscode/           ← VS Code 专属配置，可选

# AGENTS.md

## Project

`gemini_api` is an automated Forex trading system.

The system is developed and tested on a demo account first and may later be deployed to a live trading account.

## Core Principles

- Treat the repository source code as the source of truth.
- Do not assume that documented or intended behavior is implemented behavior.
- Preserve existing behavior unless a change is explicitly requested.
- Do not modify code without explicit approval when the task is analysis, audit, or design.
- Prefer small, controlled, testable changes over broad refactoring.
- Never silently change trading logic, risk parameters, position sizing, or exit behavior.
- Never introduce live-trading behavior without explicit approval.

## Trading Safety

- Demo trading is the default environment.
- Live trading must require an explicit configuration change and explicit approval.
- Risk controls, SL, TP, position sizing, position lifecycle, and exit logic are critical components.
- Never disable, bypass, or weaken risk controls to make tests pass.
- When behavior is ambiguous, stop and report the ambiguity rather than guessing.

## Development Workflow

For changes:

1. Inspect the existing implementation and dependencies.
2. Trace the actual execution flow.
3. Identify the smallest necessary change.
4. Implement only the approved change.
5. Run relevant tests.
6. Verify that existing behavior has not unintentionally changed.
7. Report what changed, what was tested, and any remaining risks.

## Code Quality

- Keep modules focused and maintainable.
- Avoid unnecessary abstractions and speculative features.
- Preserve existing interfaces unless a breaking change is explicitly approved.
- Add regression tests for confirmed bugs and important behavior changes.
- Do not modify unrelated files.

## AI Agent Behavior

- Do not invent missing code, configuration, dependencies, or behavior.
- If required information is missing, identify exactly what is missing.
- Distinguish clearly between:
  - implemented behavior
  - intended behavior
  - observed runtime behavior
  - proposed future behavior
- For analysis-only tasks, do not edit files.
- Before significant architectural or trading-logic changes, present the findings and wait for approval.