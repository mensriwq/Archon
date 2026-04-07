# Archon UI

Web dashboard for monitoring Archon formalization runs — browse iteration logs in real time, inspect file-centric diffs backed by recorded snapshots, and review proof-journal milestones across sessions.

## Quick Start

```bash
# From the Archon root directory:
bash ui/start.sh --project /path/to/your-lean-project

# With options:
bash ui/start.sh --project workspace/my-project --port 9090 --open
```

`start.sh` handles everything: checks dependencies, installs npm packages, builds the client, and starts the server. Running it again for the same project restarts that project's UI instance on the same port when possible. Different projects can run concurrently, and if the requested port is already occupied by another project or process, `start.sh` automatically falls back to the next free port.

## Views

| View | Path | What it shows |
|------|------|---------------|
| **Overview** | `/` | Current stage, sorry count, tasks, cost summary |
| **Diffs** | `/diffs` | File-centric snapshot playback with diff/file toggle and structure navigation |
| **Logs** | `/logs` | Iteration-grouped log browser with real-time streaming |
| **Journal** | `/journal` | Proof milestones per session, cross-session target aggregation |

The **Diffs** view is a file-centric replay surface built from recorded Lean code snapshots. It supports step/iteration playback, diff vs. full-file inspection, Diffs → Logs navigation, structure navigation for long files/diffs, and lightweight Lean syntax highlighting.

The **Logs** view is the main session-centric interface. The left sidebar organizes logs by iteration, showing phase status (plan → prover → review) and per-prover completion. Selecting any log file shows the full agent output with event filtering and live WebSocket streaming. New entries appear at the top.

The **Journal** view has two tabs:
- **Milestones** — per-session: milestones, summary, and recommendations from the review agent
- **Targets** — cross-session: each theorem's status evolution across all sessions, with full attempt history

## Architecture

```text
ui/
├── start.sh                        # Launcher (dependency check, build, serve)
├── README.md
├── package.json                    # Workspace-level scripts
│
├── server/                         # Fastify backend (TypeScript, ESM)
│   ├── src/
│   │   ├── index.ts                # Server entry — composes route modules
│   │   ├── types.ts                # Shared type definitions (LogEntry, etc.)
│   │   ├── utils.ts                # readFileOr, parseJsonl
│   │   └── routes/
│   │       ├── project.ts          # /api/project, /api/progress, /api/tasks, /api/sorry-count
│   │       ├── logs.ts             # /api/logs (tree), /api/logs/*, /api/log-stream/*
│   │       ├── snapshots.ts        # /api/snapshot-files, timeline, file content
│   │       ├── iterations.ts       # /api/iterations, /api/iterations/:id, .../provers/:file
│   │       ├── journal.ts          # /api/journal/sessions, milestones, all-milestones
│   │       └── summary.ts          # /api/summary (aggregated cost/token stats)
│   ├── package.json
│   └── tsconfig.json
│
└── client/                         # React SPA (Vite + TypeScript)
    ├── src/
    │   ├── App.tsx                 # Router + connection error banner
    │   ├── views/
    │   │   ├── Overview.tsx        # Stage progress, sorry count, tasks
    │   │   ├── DiffPlayback.tsx    # File-centric snapshot playback / diff viewer
    │   │   ├── LogViewer.tsx       # Iteration sidebar + log viewer
    │   │   └── Journal.tsx         # Milestones + Targets
    │   ├── components/
    │   │   ├── DiffView.tsx        # Unified diff renderer
    │   │   ├── DiffStructurePanel.tsx # Structure navigation for long diffs/files
    │   │   ├── LeanCodeLine.tsx    # Lightweight Lean syntax highlighting
    │   │   ├── LogEntryLine.tsx    # Single log entry renderer
    │   │   ├── log-details/        # Modular event detail renderers
    │   │   ├── MilestoneCard.tsx   # Journal milestone display
    │   │   ├── AttemptCard.tsx     # Proof attempt detail
    │   │   └── MarkdownBlock.tsx   # Lightweight markdown renderer
    │   ├── hooks/
    │   │   ├── useApi.ts           # React Query hooks for REST endpoints
    │   │   ├── useSnapshots.ts     # Snapshot timeline/file queries
    │   │   ├── useDiffUrlState.ts  # Diffs URL state synchronization
    │   │   ├── useLogDeepLink.ts   # One-shot Logs deep-link handling
    │   │   ├── useDiffStructureNavigation.ts # Jump/highlight state
    │   │   └── useLogStream.ts     # WebSocket streaming + REST poll fallback
    │   ├── types/index.ts          # Client-side type definitions
    │   ├── utils/
    │   │   ├── format.ts           # Duration, number formatting
    │   │   ├── aggregate.ts        # Cross-session target aggregation
    │   │   ├── markdown.ts         # Markdown → HTML conversion
    │   │   ├── diffStructure.ts    # Diff structure extraction
    │   │   ├── leanStructure.ts    # Lean declaration extraction
    │   │   ├── leanHighlight.ts    # Lightweight syntax highlighting
    │   │   └── constants.ts        # Shared constants
    │   └── styles/global.css       # CSS variables, base styles
    ├── package.json
    └── tsconfig.json
```

## Data Sources

The server reads directly from the project's `.archon/` directory:

```text
.archon/
├── PROGRESS.md                     # Stage + objectives (Overview)
├── PROJECT_STATUS.md               # Project status summary
├── task_pending.md / task_done.md  # Task lists (Overview)
├── logs/
│   ├── iter-001/
│   │   ├── meta.json               # Phase status, timing, prover states
│   │   ├── plan.jsonl              # Plan agent log
│   │   ├── prover.jsonl            # Prover log (serial mode)
│   │   ├── provers/                # Parallel prover logs
│   │   │   ├── File_A.jsonl
│   │   │   └── ...
│   │   ├── review.jsonl            # Review agent log
│   │   └── snapshots/              # Recorded code snapshots used by Diffs
│   │       ├── File_A/
│   │       │   ├── baseline.lean
│   │       │   ├── step-001.lean
│   │       │   └── ...
│   │       └── ...
│   └── iter-002/
│       └── ...
└── proof-journal/
    └── sessions/
        └── session_1/
            ├── milestones.jsonl
            ├── summary.md
            └── recommendations.md
```

Both the serial layout (`iter-NNN/prover.jsonl`) and parallel layout (`iter-NNN/provers/*.jsonl`) are supported.

## API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/project` | GET | Project name and paths |
| `/api/progress` | GET | Current stage, objectives, checklist |
| `/api/tasks` | GET | Pending and completed tasks |
| `/api/sorry-count` | GET | Project-level sorry count across .lean files |
| `/api/snapshot-files` | GET | File-centric snapshot summary for Diffs |
| `/api/snapshot-files/:slug/timeline` | GET | Timeline of snapshot steps for one file |
| `/api/snapshot-files/:slug/:iter/:file` | GET | Retrieve file content for a snapshot step |
| `/api/logs` | GET | Tree-structured log listing (`{ flat, groups }`) |
| `/api/logs/*` | GET | Parse a specific .jsonl file |
| `/api/log-stream/*` | WS | Real-time log streaming via WebSocket |
| `/api/iterations` | GET | All iteration summaries from meta.json |
| `/api/iterations/:id` | GET | Single iteration detail + prover file list |
| `/api/iterations/:id/provers/:file` | GET | Single prover log entries |
| `/api/journal/sessions` | GET | List sessions with content availability flags |
| `/api/journal/sessions/:id/milestones` | GET | Proof milestones for a session |
| `/api/journal/sessions/:id/summary` | GET | Session summary (markdown) |
| `/api/journal/sessions/:id/recommendations` | GET | Recommendations (markdown) |
| `/api/journal/all-milestones` | GET | All milestones across all sessions |
| `/api/journal/status` | GET | PROJECT_STATUS.md content |
| `/api/summary` | GET | Aggregated cost, tokens, duration across all logs |

## start.sh Options

```text
bash ui/start.sh --project PATH [OPTIONS]

--project PATH    Lean project path (required, must contain .archon/)
--port PORT       Server port (default: 8080)
--dev             Dev mode: tsx watch + vite dev server on :5173
--build           Build client only, don't start server
--open            Open browser after starting
-h, --help        Show help
```

Port detection works on macOS (`lsof`), Linux (`ss`), and minimal containers (`/proc/net/tcp`). In normal mode, instance tracking is project-scoped, so restarting one project does not kill another project's dashboard.
