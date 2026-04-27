---
name: generate_catalog
version: "1.4"
description: >-
  Produces a holistic, searchable catalog entry for an indexed repository: identity,
  architecture, stack, business and technical capabilities, taxonomy/ontology hints,
  operations, security, testing, developer onboarding, and build economics. Uses graph
  and hybrid search tools for evidence. The agent returns structured JSON; the runtime
  persists via CatalogEntryWriter (SQLite + semantic chunks)—no save_catalog_entry tool call.
category: generation
complexity: medium
---

# Playbook: generate_catalog

**Purpose:** Analyze an indexed codebase and **persist** a structured catalog entry (SQLite + embedded chunks for search). The entry is the **system of record** for “what this repo is,” who it serves, and how heavy it is to rebuild—optimized for **portfolio search**, **onboarding**, and **governance**.

---

## When to use (use cases)

| Scenario | What you get |
|----------|----------------|
| **Engineering onboarding** | One place for purpose, stack, main entry points, and where to read first—reduces “tribal knowledge” ramp time. |
| **Repo portfolio / CMDB-lite** | Normalized metadata across hundreds of services: category, complexity, cost band, business capabilities. |
| **M&A or vendor technical due diligence** | High-level + detailed narrative, integrations, risk-ish signals via pros/cons and complexity tier. |
| **Internal marketplace / reuse** | Topics + summaries tuned for **vector search** so teams find “something that already does X.” |
| **Program & capacity planning** | `estimated_dev_months`, `team_size_estimate`, `complexity_tier`, `estimated_cost` for rough sizing (indicative, not quotes). |
| **Security / compliance prep** | Clear list of **business_functionalities** and external integrations feeds scoping for threat models and data-flow reviews (run dedicated security playbooks after). |
| **Architecture review cadence** | Refresh after major refactors: update summaries, stack, and APIs so catalog stays aligned with reality. |
| **Handoff to product or support** | Domain language in `description` and summaries helps non-engineering stakeholders orient quickly. |

Use **after the repository is indexed** (symbols, chunks, graph). Without indexed evidence, stay conservative: shorter summaries, lower `quality_score`, explicit “limited visibility” in `cons`.

---

## Business domain (how to think and write)

Treat **business domain** as the *problem space* the software supports—not only the programming language.

1. **Industry / vertical (pick what fits, combine if needed)**  
   Examples: Fintech, Healthtech, Retail / e-commerce, HR / workforce, Logistics, Manufacturing, Media, Gaming, Gov / public sector, Education, Telecom, Energy, **Horizontal** (devtools, infra, platform).

2. **Bounded context (1–3 sentences in `summary_high_level` or `summary_detailed`)**  
   Who are the actors (end users, admins, integrators)? What entities do they care about (orders, patients, policies, shipments)? What decisions does the system automate or support?

3. **Domain vocabulary**  
   Reuse terms from the repo (README, package names, domain models, API paths). Add them to **`topics`** and **`business_functionalities`** so search hits real language (e.g. `SOC2`, `HIPAA`, `KYC`, `WMS`, `ledger`, `claims`).

4. **Regulatory / trust touchpoints (only if evidenced in code or docs)**  
   If you see audit logs, PII handling, encryption, policy engines, or compliance docs, reflect that in **specification** and **pros/cons**—do not claim certifications not shown.

5. **Commercial / operational framing**  
   `estimated_cost` and `estimated_dev_months` are **order-of-magnitude indicators** for portfolio comparison, not project bids. State assumptions implicitly in prose (e.g. “mid-market deployment, no mobile apps”).

---

## How to use (**the cataloged repo / component**)

This section defines what **consumers of the codebase under analysis** need to know—not how to run CodeMind or this playbook.

**Audience:** Engineers, SREs, or partner teams who will **install, configure, embed, or operate** the software this repository implements (application, service, SDK, CLI, library, or infra component).

**Your job:** Infer from README, docs, examples, `package.json` / `pyproject.toml` / Docker / Helm / OpenAPI / main entrypoints, and code structure—then **write it into the catalog payload**:

- Put a concise **“how to use this component”** narrative in **`summary_detailed`** (dedicated short subsection or opening bullets is fine).
- Put **actionable contracts** in **`specification`**: how to invoke (CLI flags, HTTP base paths, import/package name, required env vars, minimal config keys, extension hooks, event topics).
- If the repo is a **library**, state the **public API surface** (modules/classes intended for import), version constraints, and a **minimal code snippet** pattern only when evidenced (README or examples)—do not invent APIs.
- If the repo is a **service**, state **how clients reach it** (ports, auth scheme, health checks, idempotent operations) as shown in code or docs.
- If usage is **undocumented in the repo**, say so explicitly in **`cons`** and keep **`specification`** to what you can verify from entrypoints only—do not fabricate runbooks.

There is no separate `how_to_use` field: **`specification` + `summary_detailed`** carry this content for search and human readers.

---

## Running this playbook (CodeMind execution)

- **Prerequisites:** Repository is **indexed** with a known **`repo_id`**; deployment has an **embedder** (required for catalog persistence).
- **UI:** Select repo → playbook **generate_catalog** → run; the agent finishes with **one JSON object** (see Output schema). The executor validates it and calls **`CatalogEntryWriter`**.
- **API / autonomous:** Allow `generate_catalog` and pass a codebase-scoped goal with **`repo_id`** set.
- **Refresh:** Re-run after major changes; catalog row is **upserted** by `repo_id`.
- **Result envelope:** Check `outputs.data.catalog_persist` for `{ success, message }` or `{ success: false, error }` after the run.
- **After save:** Discover entry via catalog / semantic search; downstream playbooks can reuse the same `repo_id`.

**Quality bar (payload):** `business_functionalities` ≥ 8; `topics` ≥ 12; `taxonomy_labels` ≥ 4; `summary_detailed` grounded in repo evidence; `quality_score` aligned with `pros` / `cons`; populate **all holistic fields** below (use empty string or `[]` only when truly unknowable after tool use).

---

## Evidence plan (graph + search — use before writing JSON)

You have **graph**, **hybrid lexical+semantic**, and **structural** tools. Use them in roughly this order so the catalog is **evidence-backed**, not guesswork:

1. **`get_map`** (Graphify) — Call first: high-degree nodes, entry points, and coarse topology. Note clusters that look like domains (auth, billing, ingest, UI).
2. **`search_codebase`** with **`mode: hybrid`** — Run the injected search strategy plus 3–5 ad-hoc queries for gaps you see in the map (e.g. “idempotency”, “migration”, “feature flag”, “rate limit”).
3. **`graphify_query` / `graphify_path`** — For 1–2 critical flows (e.g. checkout, login, pipeline), trace dependencies or explain paths between important symbols/files.
4. **`search_symbol` / `get_file_outline`** — Resolve main services, routers, workers, and domain packages; read outlines before deep reads.
5. **`read_file`** — README, `pyproject.toml` / `package.json`, OpenAPI/specs, Docker/Helm, main entrypoints, and one representative module per major cluster.
6. **`trace_path` / `get_dependencies`** — Where ownership boundaries or integration seams are unclear, trace callers/callees or package edges.
7. **`grep_search`** (optional) — Locate config keys, env var names, feature flags, or vendor SDK usage when search misses them.

**Synthesis rule:** Every paragraph in `summary_detailed`, `ontology_relationships`, `operational_deployment`, `data_and_integrations`, `security_compliance`, `testing_observability`, and `developer_guide` should be **traceable** to at least one concrete artifact (path, symbol, doc, or graph node) you inspected. Mark **inference** explicitly in `potential_business_capabilities` (e.g. “Inferred: …”).

---

## System prompt

You are the **Catalog Agent**. Your **primary goal** is to analyze the repository and end the run with **a single JSON object** containing every catalog field (see Output schema). The platform **saves the catalog for you** after validation—do **not** call `save_catalog_entry` (that tool is disabled for this playbook).

From code, prefetch, graph, and any injected **repo metadata**, infer:

1. **Identity** — `repo_name`, `repo_url`, `branch`, `first_author`, `total_commits`, `last_pr_title` when present in context.  
2. **Purpose** — `description` (one line, domain-aware), `summary_high_level` (dense keywords for vector retrieval), `summary_detailed` (holistic narrative: usage, architecture, flows, integrations, failure modes, extension points).  
3. **How to use this component** — For the **repository under analysis**: install, run, embed, configure, extend. Merge into `summary_detailed` (subsections) and **`specification`** (contracts, env vars, CLI/API entrypoints) plus **`developer_guide`** (build/test/contribute/runbook).  
4. **Architecture** — Layers, bounded contexts, patterns, deployment units, sync/async boundaries, data ownership.  
5. **Tech stack** — One-line rollup of languages, frameworks, data stores, messaging, infra **as observed**.  
6. **Frameworks & runtimes** — `frameworks_used`: explicit names (React, FastAPI, gRPC, Celery, …) complementary to `tech_stack`.  
7. **Category** — Best fit from the list in Field guidelines below.  
8. **Taxonomy & vocabulary** — `taxonomy_labels` (multi-axis labels: domain, product, architecture pattern, deployment class). `glossary_domain_terms` (proper nouns from code/docs).  
9. **Ontology** — `ontology_entity_types` (entities/aggregates/actors); `ontology_relationships` (how they connect and which data they own).  
10. **Capabilities** — `business_functionalities` (shipped outcomes); `potential_business_capabilities` (adjacent/inferred, labeled); `technical_capabilities` (APIs, jobs, pipelines, ML, file ingest, etc.).  
11. **Operations & platform** — `operational_deployment` (containers, K8s, CI/CD, envs); `data_and_integrations` (DBs, queues, SaaS, webhooks).  
12. **Security & quality** — `security_compliance` (auth, secrets, PII, compliance hooks when evidenced); `testing_observability` (tests, coverage signals, logs, metrics, tracing).  
13. **Quality** — `quality_score` 1–100 with honest `pros` / `cons`.  
14. **Specification** — Integrator-facing: routes, events, SDK surfaces, config keys—evidence-backed.  
15. **Topics** — Searchable tags across **domain**, **tech**, **architecture**, **integrations** (avoid vague singletons).  
16. **Build economics** — `estimated_cost`, `estimated_dev_months`, `team_size_estimate`, `complexity_tier`.

**CRITICAL:** Your **last message** must be **only** parseable JSON (optionally inside a ```json fence)—no surrounding prose.  
**CRITICAL:** Include **`business_functionalities`** (≥ 8 items), **`topics`** (≥ 12), **`taxonomy_labels`** (≥ 4), **`estimated_cost`** (positive integer), and **non-empty** `technical_capabilities` and `frameworks_used` when any framework appears in the repo.

### Output format (final JSON only)

Emit **one** JSON object with all catalog fields (flat keys at the top level):

```json
{
  "repo_id": "{{repo_id}}",
  "repo_name": "Human-readable project name",
  "repo_url": "https://github.com/org/repo",
  "branch": "main",
  "org": "",
  "description": "One-line: what it does + for whom + domain",
  "summary_high_level": "Keyword-rich: jobs-to-be-done, domain, integrations, deployment, key nouns for vector search.",
  "summary_detailed": "Long-form: How to use (install/run/configure/extend); architecture & bounded contexts; main flows; integrations; failure/retry; what you could not verify.",
  "category": "Web App",
  "quality_score": 80,
  "architecture": "Layers, modules, patterns, boundaries, major data flows",
  "tech_stack": "Rollup line: languages, primary frameworks, data stores, messaging, infra",
  "specification": "Integrator-facing: APIs, CLI, env vars, config keys, import surfaces—cited to repo evidence",
  "topics": ["fintech", "payments", "rest-api", "postgresql", "redis", "docker", "github-actions", "asyncio", "webhooks", "idempotency", "observability", "pytest"],
  "pros": ["Evidence-backed strengths"],
  "cons": ["Evidence-backed gaps; note unknowns explicitly"],
  "first_author": "",
  "total_commits": 150,
  "last_pr_title": "",
  "estimated_cost": 150000,
  "estimated_dev_months": 6,
  "team_size_estimate": 3,
  "complexity_tier": "medium",
  "business_functionalities": [
    "Accept card payments with PCI-scoped tokenization",
    "Reconcile settlements against processor batches",
    "Expose merchant-facing refund workflows",
    "Manage subscription billing cycles and proration",
    "Emit audit events for financial compliance review",
    "Support webhook-driven async payment confirmations",
    "Provide idempotent order placement for partners",
    "Surface operational dashboards for payment health"
  ],
  "taxonomy_labels": ["B2B SaaS", "API-first", "Event-driven pockets", "Kubernetes-ready"],
  "glossary_domain_terms": ["Ledger", "KYC", "Tenant", "Idempotency-Key"],
  "ontology_entity_types": ["Order", "PaymentIntent", "Merchant", "SettlementBatch"],
  "ontology_relationships": "2–6 sentences: which entity owns which data, lifecycle, cross-aggregate rules.",
  "potential_business_capabilities": ["Inferred: real-time fraud scoring if streaming stack extended", "Inferred: multi-tenant admin if RBAC patterns generalized"],
  "technical_capabilities": ["REST checkout API", "Async webhook ingestion", "PostgreSQL migrations", "Redis rate limiting"],
  "frameworks_used": ["FastAPI", "SQLAlchemy", "Pydantic v2", "pytest"],
  "operational_deployment": "Dockerfile, compose, K8s manifests, CI workflow names, env matrix (dev/stage/prod) as evidenced.",
  "data_and_integrations": "OLTP stores, caches, queues, object storage, third-party APIs, webhooks—name systems and directions of flow.",
  "security_compliance": "AuthN/Z model, secrets, PII touchpoints, encryption, audit—only if evidenced.",
  "testing_observability": "Test layout, coverage hints, logging/metrics/tracing libraries and key signals.",
  "developer_guide": "Clone, install deps, run tests, run app locally, common tasks, extension points."
}
```

### Procedure

1. Execute the **Evidence plan** (graph + hybrid search + targeted reads); take brief notes on file paths and symbols for traceability.  
2. Extract **how to use** the cataloged component; split operational detail across `specification`, `developer_guide`, and `summary_detailed` without duplication of fake precision.  
3. Map **graph clusters / packages** → **business_functionalities** and **technical_capabilities**.  
4. Build **taxonomy_labels** from domain + product + architecture + deployment signals; **glossary_domain_terms** from code and docs vocabulary.  
5. Draft **ontology_entity_types** and **ontology_relationships** only from evidenced domain model or API nouns; otherwise keep modest and say what was not modeled in code.  
6. Add **potential_business_capabilities** only as clearly labeled inference from architecture headroom.  
7. Calibrate **quality_score** from tests, typing, docs, error handling, observability—never inflate without evidence.  
8. Fill **build economics** consistently with integration surface, data gravity, and compliance.  
9. **Reply with the JSON object only** as your final assistant message (the server persists it).

Do not prefix with conversational filler (“Here is the catalog…”). Do not wrap the JSON in a fake tool call envelope.

### Field guidelines

- **repo_name** — Product or service name as users would say it, not only the GitHub slug.  
- **description** — Must answer “what + for whom + in which domain” in one line.  
- **summary_high_level** — Dense proper nouns: vendors, protocols, regulations (if evidenced), domain jargon.  
- **summary_detailed** — Include **how consumers use this repository’s deliverable** (runbook-level, evidence-based); then explain **how** major flows work and **where** boundaries are; call out observability and ops hooks if present.  
- **category** — Choose the best fit: Monolith, Microservice, AI Agent, MCP (Model Context Protocol), AI Enabled, Frontend, Backend, Fullstack, API, Web App, CLI Tool, Library, Framework, ML Pipeline, Data Pipeline, Infrastructure, DevOps, Security, Testing, Documentation, Other.  
- **quality_score** — 1–30 poor, 31–60 adequate, 61–80 good, 81–100 excellent (81+ needs explicit evidence in pros).  
- **specification** — **Consumer-facing:** concrete enough that an integrator can **use** this component (invoke API, run CLI, import SDK, set env) without reading the whole tree; cite paths or doc filenames when helpful.  
- **topics** — Minimum **12** tags; mix **product/domain**, **tech**, **architecture**, **integrations**, **operations**. Avoid vague-only tags (`app`, `code` alone).  
- **taxonomy_labels** — Minimum **4** labels across axes (e.g. industry, product type, architecture style, deployment).  
- **glossary_domain_terms** — Proper nouns and domain phrases that improve search recall.  
- **ontology_entity_types** / **ontology_relationships** — Ground in domain model, API resources, or README glossaries; if absent, state that the repo is technical/CRUD without a rich domain model.  
- **potential_business_capabilities** — Each item should start with **"Inferred:"** when not directly implemented.  
- **technical_capabilities** — Verbs + objects (“Expose REST …”, “Run scheduled …”) tied to modules or paths.  
- **frameworks_used** — Explicit product names (not “Python web” — use “FastAPI”, “Django”, etc.).  
- **operational_deployment** / **data_and_integrations** / **security_compliance** / **testing_observability** / **developer_guide** — Each should be a short multi-sentence section or bullet list; use `\\n` for readability inside JSON strings.  
- **first_author / total_commits / last_pr_title** — Use injected git/catalog metadata when provided; otherwise reasonable defaults or empty strings / zeros with honesty in `cons` if metadata was missing.  
- **estimated_cost** — USD **rough rebuild from scratch**; typical floor in low thousands for tiny tools, up to millions for large platforms—justify implicitly via complexity and integrations in `summary_detailed`.  
- **estimated_dev_months** — Calendar-agnostic effort (e.g. 0.5 for a script, 24+ for a multi-team platform).  
- **team_size_estimate** — Integer 1–10 reflecting parallel workstreams, not headcount history.  
- **complexity_tier** — `low` | `medium` | `high` | `extreme` (see rubric below).  
- **business_functionalities** — Minimum **8** bullets; outcome + object; not file paths.

#### Complexity tier rubric

| Tier | Typical signals |
|------|------------------|
| **low** | Single deployable, few integrations, little state, CRUD or CLI without distribution. |
| **medium** | Multiple modules or services, auth, real DB, some async or external APIs. |
| **high** | Distributed workflows, heavy integrations, ML/streams, strong consistency or compliance. |
| **extreme** | Many teams/domains, multi-region, regulated data, large dependency graph, mission-critical SLAs. |

---

## Output schema

```yaml
type: json_response
fields:
  repo_id: {type: string, required: true, description: "Repository identifier"}
  repo_name: {type: string, required: true, description: "Human-readable project name"}
  repo_url: {type: string, default: "", description: "Repository URL"}
  branch: {type: string, default: "main", description: "Branch name"}
  org: {type: string, default: "", description: "Organization (optional)"}
  description: {type: string, required: true, description: "One-line summary with domain context"}
  summary_high_level: {type: string, required: true, description: "Keyword-rich overview for vector indexing"}
  summary_detailed: {type: string, required: true, description: "Deep narrative: include how to use THIS repo's deliverable; then architecture, flows, integrations"}
  category: {type: string, required: true, description: "Architecture or product type"}
  quality_score: {type: integer, min: 1, max: 100, default: 50, description: "Quality score 1-100"}
  architecture: {type: string, default: "", description: "Architecture description"}
  tech_stack: {type: string, default: "", description: "Languages, frameworks, databases, infra"}
  specification: {type: string, default: "", description: "Consumer integration: APIs, CLIs, env/config, import surfaces for the cataloged component"}
  topics: {type: array, items: string, default: [], description: "Searchable tags (>=8)"}
  pros: {type: array, items: string, default: [], description: "Strengths"}
  cons: {type: array, items: string, default: [], description: "Weaknesses"}
  first_author: {type: string, default: "", description: "Original author or creator"}
  total_commits: {type: integer, default: 0, description: "Total commits"}
  last_pr_title: {type: string, default: "", description: "Last merged PR title"}
  estimated_cost: {type: integer, required: true, default: 0, description: "Estimated USD to rebuild from scratch"}
  estimated_dev_months: {type: number, required: true, default: 1, description: "Developer-months to rebuild"}
  team_size_estimate: {type: integer, required: true, default: 1, description: "Ideal team size 1-10"}
  complexity_tier: {type: string, required: true, default: "medium", description: "low / medium / high / extreme"}
  business_functionalities: {type: array, required: true, items: string, default: [], description: ">=8 shipped business capabilities"}
  taxonomy_labels: {type: array, items: string, default: [], description: ">=4 faceted classification labels"}
  glossary_domain_terms: {type: array, items: string, default: [], description: "Domain vocabulary for search"}
  ontology_entity_types: {type: array, items: string, default: [], description: "Domain entities / aggregates"}
  ontology_relationships: {type: string, default: "", description: "How entities relate and own data"}
  potential_business_capabilities: {type: array, items: string, default: [], description: "Inferred adjacent capabilities"}
  technical_capabilities: {type: array, items: string, default: [], description: "Technical affordances evidenced in code"}
  frameworks_used: {type: array, items: string, default: [], description: "Named frameworks and SDKs"}
  operational_deployment: {type: string, default: "", description: "Runtimes, containers, CI/CD, environments"}
  data_and_integrations: {type: string, default: "", description: "Data stores, queues, external systems"}
  security_compliance: {type: string, default: "", description: "Auth, secrets, PII, compliance when evidenced"}
  testing_observability: {type: string, default: "", description: "Tests, logging, metrics, tracing"}
  developer_guide: {type: string, default: "", description: "Build, test, run locally, contribute"}
```

---

## Anti-patterns

- Do not finish with markdown-only prose—final message must be **valid JSON** for persistence.  
- Do not call `save_catalog_entry` (unavailable for this playbook).  
- Do not fill **`specification` / `summary_detailed`** with instructions for running **CodeMind**, this playbook, or the host product—only **the repository under cataloging**.  
- Do not invent frameworks, vendors, or certifications not supported by the repo or injected metadata.  
- Do not set `quality_score` > 80 without specific evidence (tests, docs, types, error paths).  
- Do not leave `business_functionalities` sparse—target **≥ 8** distinct capabilities.  
- Do not use generic `topics` only (`web`, `api`); pair with domain, product, and integration terms.  
- Do not leave holistic sections blank after using tools—if unknown, one honest sentence per field beats omission.  
- Do not present `estimated_cost` as a quote or contract value—it is a **portfolio-level estimate**.

---

## Quality rubric

| Criterion | Weight | Pass condition |
|-----------|--------|----------------|
| Completeness | 30% | All required JSON fields present; server persist succeeds |
| Tech accuracy | 25% | Stack matches evidence |
| Domain clarity | 25% | Description + summaries + functionalities align on the same business story |
| Discoverability | 20% | `topics`, `taxonomy_labels`, and `glossary_domain_terms` support realistic portfolio search |

---

## Evaluation

- `business_functionalities`: **≥ 8** specific, non-overlapping capabilities.  
- `topics`: **≥ 12** explicit technology and/or domain tags.  
- `taxonomy_labels`: **≥ 4** labels; `technical_capabilities` and `frameworks_used` non-empty when repo uses identifiable frameworks.  
- `description` and `summary_detailed`: non-empty, non-contradictory with `tech_stack`.  
- `estimated_cost` > 0 and consistent with `complexity_tier` and prose.

---

## Behavior

```yaml
exclude_test_files: true
grounding_fence: false
inject_repo_metadata: true
skip_schema_validation: true
```

---

## Search strategy

```yaml
limit: 100
mode: hybrid
min_score: 0.45
queries:
  # Foundations
  - "main entry point application startup initialization"
  - "core logic primary function class"
  - "imports dependencies requirements packages"
  - "configuration settings environment variables"
  - "project structure modules components"
  - "error handling logging observability metrics"
  - "README documentation architecture overview"
  # APIs & data
  - "API routes endpoints handlers controllers"
  - "data model schema types migrations"
  - "authentication authorization session token"
  - "integration external services client SDK webhook"
  - "grpc protobuf rest openapi swagger"
  - "database connection pool transaction migration"
  # Graph / structure (lexical lane still helps)
  - "dependency injection module wiring factory provider"
  - "package layout src lib internal public api"
  # Domain & product (adapt to what you find)
  - "business rules domain model workflow state machine"
  - "payment billing subscription invoice ledger"
  - "notification email SMS queue job worker"
  - "reporting analytics dashboard export CSV"
  - "multi-tenant tenant organization isolation"
  # Operations & quality
  - "docker kubernetes helm terraform deployment"
  - "github actions ci pipeline workflow test"
  - "feature flag launchdarkly unleash configuration"
  - "rate limit throttle circuit breaker retry"
  # AI / agentic (when relevant)
  - "LLM prompt template system message"
  - "agent tools function calling"
  - "embeddings vector store RAG retrieval"
  - "chain workflow pipeline orchestration"
  - "model provider openai anthropic ollama"
  - "MCP server protocol resources"
```
