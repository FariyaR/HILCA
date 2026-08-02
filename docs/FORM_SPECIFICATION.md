# HILCA Intake Form Specification

## Form Structure (Matches Google Form)

### 1. Email (Top)
```
Email: [________________]
```
- Required
- Used for results delivery
- Appears at top of form

### 2. Research Topic Section
```
[RESEARCH TOPIC]

Topic: [Large text area for complete research domain/hypothesis]
(Help text explaining HILCA is designed for creative synthesis,
out-of-the-box reasoning, cross-disciplinary constellation, etc.)
```
- Required
- Long-form text area
- Detailed help text

### 3. Domain Tags Section
```
[DOMAIN TAGS]

Tag 1: [Infrastructure ________________]  *required
Tag 2: [Cost Analysis ________________]  *required
Tag 3: [Security ________________]       *required
Tag 4: [(optional) ________________]
Tag 5: [(optional) ________________]
```
- Tags 1-3: Required
- Tags 4-5: Optional
- Used to calibrate sub-agent selection
- 1-3 words per tag
- Help text explains HILCA uses tags to select specialists

### 4. Agent Role Selection Section
```
[AGENT ROLE SELECTION]

Agent 1: [Systems Architect ________________]  *required
Agent 2: [Cost Analyst ________________]       *required
Agent 3: [Skeptic ________________]            *required
```
- 3 agents required
- Professional roles or domain expertise
- Help text encourages including "Skeptic" or "Critic" for strong dialectic
- Each agent receives unique "Role Card"
- Agents will hypothesize, challenge, and iterate

### 5. Evidence Sources Section
```
[EVIDENCE SOURCES]

URL 1: [https://example.com/paper.pdf ________________]  *required
URL 2: [https://example.com/study.pdf ________________]  *required
URL 3: [https://example.com/article.pdf ________________]  *required
```
- 3 URLs required (matching Google Form)
- Direct links to:
  - Research papers (PDF)
  - Technical articles
  - Webpages
- Help text: "HILCA's Evidence Processor will extract key facts"
- Used for M3 RAG pipeline integration

### 6. Email Confirmation (Bottom)
```
Email Address:
[test@example.com]

[SPAWN AGENT PANEL button]
```
- Confirms email for results
- Appears before submit button
- Submit button spawns the agent panel

---

## Form Field Mapping

| Google Form Field | HTML Form Field | Input Type | Required |
|-------------------|-----------------|-----------|----------|
| Email | email | text/email | Yes |
| Topic | topic | textarea | Yes |
| Tag 1-5 | tag_1 to tag_5 | text | 1-3 yes, 4-5 no |
| Agent 1-3 | agent_1 to agent_3 | text | Yes |
| URL 1-3 | url_1 to url_3 | url | Yes |

---

## Form Submission Flow

```
┌─────────────────────────────┐
│  User fills form (9 fields) │
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│  Browser validates fields   │
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│  POST /intake with form data│
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│  Server validates & creates │
│  run in DB (status=intake)  │
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│  Controller spawns          │
│  3-5 sub-agents with thesis │
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│  Redirect to               │
│  /results/{run_id}         │
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│  Display results page:      │
│  - Run ID (copy-able)       │
│  - Topic & tags             │
│  - Agents (name, role,      │
│    expertise, tone, thesis) │
│  - Critic badges            │
│  - Email confirmation       │
└─────────────────────────────┘
```

---

## Results Page Display

### Header
```
✓ Agent Panel Spawned

Run ID: 09afa51d-8090-4e34-bcda-73a2eb594c40
```

### Topic Summary
```
Your Research Topic:
┌─────────────────────────────────────────┐
│ Should we adopt microservices architecture
│ for our growing platform?
│
│ Tags: architecture  scalability  complexity  operations  cost
└─────────────────────────────────────────┘
```

### Spawned Agents
```
Spawned Agent Panel (3 agents)

┌─────────────────────────────────────────┐
│ Systems Architect                        │
│ Expertise: Systems Architect grounded in │
│           architecture, scalability...   │
│ Role: Argue the systems architect       │
│       position; advance concrete points │
│ Tone: Direct, analytical, concise       │
│                                         │
│ ┌───────────────────────────────────┐  │
│ │ OPENING THESIS:                   │  │
│ │ The system architecture needs      │  │
│ │ careful redesign to handle arch...│  │
│ └───────────────────────────────────┘  │
└─────────────────────────────────────────┘

┌─────────────────────────────────────────┐
│ Cost Analyst                             │
│ Expertise: Cost Analyst grounded in...   │
│ ...                                      │
└─────────────────────────────────────────┘

┌─────────────────────────────────────────┐
│ Skeptic [Critic]                         │
│ Expertise: Skeptic grounded in...        │
│ ...                                      │
│                                         │
│ ┌───────────────────────────────────┐  │
│ │ OPENING THESIS:                   │  │
│ │ This proposal has critical gaps    │  │
│ │ and unexamined failure modes...    │  │
│ └───────────────────────────────────┘  │
└─────────────────────────────────────────┘
```

### Confirmation
```
✓ Results will be sent to: test@example.com
```

---

## API Response (JSON)

### POST /intake
Returns redirect HTML to `/results/{run_id}`

### GET /runs/{run_id}
```json
{
  "id": "09afa51d-8090-4e34-bcda-73a2eb594c40",
  "topic": "Should we adopt microservices architecture...",
  "tags": ["architecture", "scalability", "complexity", "operations", "cost"],
  "agent_hints": ["Systems Architect", "Cost Analyst", "Skeptic"],
  "evidence_urls": ["https://...", "https://...", "https://..."],
  "email": "test@example.com",
  "status": "spawned",
  "created_at": "2026-06-29T15:30:45.123456+00:00",
  "sub_agents": [
    {
      "name": "Systems Architect",
      "expertise": "Systems Architect grounded in architecture, scalability...",
      "mandate": "Argue the systems architect position...",
      "constraints": "Stay strictly within your assigned role...",
      "tone": "Direct, analytical, concise.",
      "is_critic": false,
      "thesis": "The system architecture needs careful redesign to handle..."
    },
    {
      "name": "Skeptic",
      "expertise": "Skeptic grounded in...",
      "mandate": "Argue the skeptic position...",
      "constraints": "Stay strictly within your assigned role...",
      "tone": "Direct, analytical, concise.",
      "is_critic": true,
      "thesis": "This proposal has critical gaps and unexamined failure modes..."
    }
  ]
}
```

---

## Comparison: Old vs New Form

### Old Form (CSV-based, unstructured)
```
Topic: [textarea]
Tags: [comma-separated text] e.g. "a, b, c"
Agent Hints: [comma-separated text] e.g. "Builder, Skeptic"
Evidence URLs: [comma-separated text] e.g. "url1, url2, url3"
Email: [text]
```

**Issues:**
- Users had to format as CSV (error-prone)
- No separation between fields
- No help text
- No validation
- Didn't match Google Form spec

### New Form (Structured, user-friendly)
```
Email: [text/email]

[Research Topic]
Topic: [large textarea with help text]

[Domain Tags]
Tag 1-3: [required text inputs]
Tag 4-5: [optional text inputs]

[Agent Roles]
Agent 1-3: [required text inputs with help text]

[Evidence Sources]
URL 1-3: [required URL inputs]

[Submit Button]
```

**Improvements:**
- Structured fields (no CSV parsing)
- Separate input for each value
- Help text for each section
- Browser validation
- Matches Google Form spec exactly
- Results page with HTML confirmation
- JSON API for programmatic access

---

## Testing the Form

### Via Web Browser
```bash
$ uvicorn api:app --reload
# Visit http://localhost:8000/
```

### Via Test Script
```bash
$ python test_api.py
```

### Via cURL
```bash
curl -X POST http://localhost:8000/intake \
  -F "topic=My topic" \
  -F "tag_1=domain1" \
  -F "tag_2=domain2" \
  -F "tag_3=domain3" \
  -F "agent_1=Role1" \
  -F "agent_2=Role2" \
  -F "agent_3=Role3" \
  -F "url_1=https://example.com/1" \
  -F "url_2=https://example.com/2" \
  -F "url_3=https://example.com/3" \
  -F "email=user@example.com"
```

---

## Notes for M2+

- Form structure is **stable** and **won't change** for M2-M5
- Evidence URLs are stored but not processed in M1 (M3 responsibility)
- Email field is stored but not sent in M1 (M5 responsibility)
- Run status transitions: `intake` → `spawned` (M2 adds `debating`, `concluded`)
- Thesis is stored per agent and ready for M2 dialectic loop

The form is production-ready and matches the client's Google Form specification exactly.
