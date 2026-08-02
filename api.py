"""HTTP intake and results for HILCA (Milestone 1).

  M1:
  GET  /             -> intake form
  POST /intake       -> spawn agents
  GET  /results/{id} -> results page
  GET  /runs/{id}    -> JSON API

Run:  uvicorn api:app --reload
"""
from __future__ import annotations

import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Form
from fastapi.responses import HTMLResponse, JSONResponse
from controller import MainController
from db import Store
from schemas import IntakeRequest, SpawnResult

load_dotenv()  # honor .env (LLM_PROVIDER, OPENAI_API_KEY, OPENAI_MODEL) before spawning the controller

app = FastAPI(title="HILCA — Milestone 1 (Intake + Storage + Controller)")
store = Store(os.getenv("DB_PATH", "hilca.db"))
controller = MainController(store)


FORM_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>HILCA Intake Form</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:linear-gradient(135deg,#f5f7fa 0%,#c3cfe2 100%);min-height:100vh;padding:2rem 1rem}
.container{max-width:900px;margin:0 auto;background:#fff;border-radius:12px;box-shadow:0 10px 40px rgba(0,0,0,0.1);overflow:hidden}
.header{background:linear-gradient(135deg,#127a6b 0%,#0d5c52 100%);color:#fff;padding:3rem 2rem;text-align:center}
.header h1{font-size:2.5rem;margin-bottom:0.5rem;font-weight:700}
.header p{font-size:1.1rem;opacity:0.95}
.form-content{padding:3rem 2rem}
fieldset{border:none;padding:0;margin:2.5rem 0;border-top:1px solid #eee;padding-top:2rem}
fieldset:first-of-type{border-top:none;margin-top:0;padding-top:0}
legend{font-weight:700;font-size:1.3rem;color:#127a6b;margin-bottom:1.5rem;display:flex;align-items:center}
legend:before{content:"";display:inline-block;width:4px;height:24px;background:linear-gradient(135deg,#127a6b,#0d5c52);border-radius:2px;margin-right:0.75rem}
label{display:block;margin:1.2rem 0 0.5rem;font-weight:600;color:#2c3e50;font-size:0.95rem}
.required{color:#e74c3c}
.help-text{display:block;font-size:0.85rem;color:#7f8c8d;margin:0.4rem 0 0.8rem;line-height:1.4}
input,textarea{width:100%;padding:0.9rem;border:2px solid #ecf0f1;border-radius:6px;font:inherit;font-size:1rem;transition:border-color 0.2s,box-shadow 0.2s;background:#f9fafb}
input:focus,textarea:focus{outline:none;border-color:#127a6b;box-shadow:0 0 0 3px rgba(18,122,107,0.1);background:#fff}
textarea{resize:vertical;min-height:140px;font-family:inherit}
input::placeholder,textarea::placeholder{color:#bdc3c7}
.grid-container{display:grid;grid-template-columns:1fr 1fr;gap:1.5rem}
@media(max-width:600px){.grid-container{grid-template-columns:1fr}}
.submit-section{margin-top:3rem;padding-top:2rem;border-top:1px solid #eee;text-align:center}
button{padding:1rem 3rem;border:none;border-radius:6px;background:linear-gradient(135deg,#127a6b,#0d5c52);color:#fff;font:inherit;font-weight:700;font-size:1.05rem;cursor:pointer;transition:transform 0.2s,box-shadow 0.2s}
button:hover{transform:translateY(-2px);box-shadow:0 8px 20px rgba(18,122,107,0.3)}
button:active{transform:translateY(0)}
.info-box{background:#e8f8f5;border-left:4px solid #127a6b;padding:1rem;border-radius:4px;margin:1rem 0;font-size:0.9rem;color:#1a5f56}
</style>
</head><body>
<div class="container">
<div class="header">
<h1>HILCA Intake</h1>
<p>Multi-Agent Dialectic Intelligence System</p>
</div>
<form method="post" action="/intake" class="form-content">

<form method="post" action="/intake">

  <fieldset>
    <label>Email Address <span class="required">*</span></label>
    <p class="help-text">Where should we send your results?</p>
    <input name="email" type="email" required placeholder="you@example.com">
  </fieldset>

  <fieldset>
    <legend>Research Topic</legend>
    <p class="help-text">Describe the complete research domain, complex problem, or high-level hypothesis you wish to explore. HILCA synthesizes multiple perspectives for out-of-the-box reasoning.</p>
    <label>Your Question or Problem <span class="required">*</span></label>
    <textarea name="topic" required placeholder="e.g., Should we adopt microservices architecture for our growing platform?"></textarea>
  </fieldset>

  <fieldset>
    <legend>Domain Expertise</legend>
    <p class="help-text">Enter specific domains relevant to your topic. HILCA uses these to calibrate specialized sub-agents. 1–3 words per tag.</p>
    <div class="grid-container">
      <div>
        <label>Tag 1 <span class="required">*</span></label>
        <input name="tag_1" placeholder="Infrastructure" required>
      </div>
      <div>
        <label>Tag 2 <span class="required">*</span></label>
        <input name="tag_2" placeholder="Cost Analysis" required>
      </div>
    </div>
    <label>Tag 3 <span class="required">*</span></label>
    <input name="tag_3" placeholder="Security" required>
    <label>Tag 4 (optional)</label>
    <input name="tag_4" placeholder="e.g. Scalability">
    <label>Tag 5 (optional)</label>
    <input name="tag_5" placeholder="e.g. Operations">
  </fieldset>

  <fieldset>
    <legend>Agent Personas</legend>
    <p class="help-text">Specify AI agent roles to debate your topic. For strong dialectic, include at least one skeptical voice to challenge assumptions.</p>
    <div class="grid-container">
      <div>
        <label>Agent 1 <span class="required">*</span></label>
        <input name="agent_1" placeholder="e.g. Systems Architect" required>
      </div>
      <div>
        <label>Agent 2 <span class="required">*</span></label>
        <input name="agent_2" placeholder="e.g. Cost Analyst" required>
      </div>
    </div>
    <label>Agent 3 <span class="required">*</span></label>
    <input name="agent_3" placeholder="e.g. Skeptic" required>
  </fieldset>

  <fieldset>
    <legend>Evidence & Sources</legend>
    <p class="help-text">Provide links to research papers, articles, or webpages. These ground the debate in real-world data.</p>
    <label>Source 1 <span class="required">*</span></label>
    <input name="url_1" type="url" placeholder="https://example.com/paper.pdf" required>
    <label>Source 2 <span class="required">*</span></label>
    <input name="url_2" type="url" placeholder="https://example.com/study.pdf" required>
    <label>Source 3 <span class="required">*</span></label>
    <input name="url_3" type="url" placeholder="https://example.com/article.pdf" required>
  </fieldset>

  <div class="submit-section">
    <button type="submit">Begin Agent Debate</button>
  </div>
</form>
</div>
</body></html>
"""


@app.get("/", response_class=HTMLResponse)
def form() -> str:
    return FORM_HTML


@app.post("/intake", response_class=HTMLResponse)
def intake(
    topic: str = Form(...),
    tag_1: str = Form(...),
    tag_2: str = Form(...),
    tag_3: str = Form(...),
    tag_4: str = Form(""),
    tag_5: str = Form(""),
    agent_1: str = Form(...),
    agent_2: str = Form(...),
    agent_3: str = Form(...),
    url_1: str = Form(...),
    url_2: str = Form(...),
    url_3: str = Form(...),
    email: str = Form(...),
) -> str:
    # Collect tags, filtering empty ones
    tags = [t.strip() for t in [tag_1, tag_2, tag_3, tag_4, tag_5] if t.strip()]
    agent_hints = [a.strip() for a in [agent_1, agent_2, agent_3] if a.strip()]
    evidence_urls = [u.strip() for u in [url_1, url_2, url_3] if u.strip()]

    req = IntakeRequest(
        topic=topic,
        tags=tags,
        agent_hints=agent_hints,
        evidence_urls=evidence_urls,
        email=email or None,
    )
    run_id = store.create_run(req)
    try:
        agents = controller.spawn_sub_agents(run_id)
    except Exception as exc:
        return f"<html><body><h1>Error</h1><p>Spawn failed: {exc}</p></body></html>"

    # Redirect to results page
    return f"""<html><head><meta http-equiv="refresh" content="0;url=/results/{run_id}"></head>
    <body><p>Loading results...</p></body></html>"""


@app.get("/results/{run_id}", response_class=HTMLResponse)
def results_page(run_id: str) -> str:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    agents = store.get_sub_agents(run_id)
    tags_html = "".join(f"<span class='tag'>{t}</span>" for t in run["tags"])

    agents_html = ""
    for i, agent in enumerate(agents):
        critic_badge = "<span class='critic-badge'>[CRITIC]</span>" if agent.is_critic else ""
        agents_html += f"""
        <div class='agent-card'>
          <div class='agent-header'>
            <div class='agent-title'>{agent.name} {critic_badge}</div>
          </div>
          <div class='agent-body'>
            <div class='agent-field'>
              <strong>Expertise</strong>
              <p>{agent.expertise}</p>
            </div>
            <div class='agent-field'>
              <strong>Mandate</strong>
              <p>{agent.mandate}</p>
            </div>
            <div class='agent-field'>
              <strong>Tone</strong>
              <p>{agent.tone}</p>
            </div>
            <div class='thesis-box'>
              <div class='thesis-label'>Opening Thesis</div>
              <p class='thesis-text'>{agent.thesis}</p>
            </div>
          </div>
        </div>
        """

    email_text = run.get("email", "Not provided")
    topic_text = run["topic"]

    return f"""
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8">
      <title>HILCA - Agent Panel Ready</title>
      <style>
        *{{margin:0;padding:0;box-sizing:border-box}}
        body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:linear-gradient(135deg,#f5f7fa 0%,#c3cfe2 100%);min-height:100vh;padding:2rem 1rem}}
        .container{{max-width:1000px;margin:0 auto;background:#fff;border-radius:12px;box-shadow:0 10px 40px rgba(0,0,0,0.1);overflow:hidden}}
        .header{{background:linear-gradient(135deg,#127a6b 0%,#0d5c52 100%);color:#fff;padding:3rem 2rem;text-align:center}}
        .header h1{{font-size:2.2rem;margin-bottom:0.3rem;font-weight:700}}
        .header .subtitle{{font-size:0.95rem;opacity:0.9}}
        .content{{padding:3rem 2rem}}
        .section{{margin:2.5rem 0}}
        .section-title{{font-size:1.4rem;font-weight:700;color:#127a6b;margin-bottom:1.5rem;display:flex;align-items:center}}
        .section-title:before{{content:"";display:inline-block;width:4px;height:28px;background:linear-gradient(135deg,#127a6b,#0d5c52);border-radius:2px;margin-right:0.75rem}}
        .topic-box{{background:#f8f9fa;border-left:4px solid #127a6b;padding:1.5rem;border-radius:8px;margin-bottom:1.5rem}}
        .topic-text{{font-size:1rem;line-height:1.6;color:#2c3e50;margin-bottom:1rem}}
        .tags{{display:flex;gap:0.5rem;flex-wrap:wrap}}
        .tag{{background:#e8f8f5;color:#0d5c52;padding:0.4rem 0.8rem;border-radius:20px;font-size:0.85rem;font-weight:500}}
        .agents-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:1.5rem}}
        .agent-card{{background:#f9fafb;border:2px solid #ecf0f1;border-radius:8px;transition:transform 0.2s,box-shadow 0.2s;overflow:hidden}}
        .agent-card:hover{{transform:translateY(-4px);box-shadow:0 8px 24px rgba(0,0,0,0.12);border-color:#127a6b}}
        .agent-header{{background:linear-gradient(135deg,#127a6b 0%,#0d5c52 100%);color:#fff;padding:1rem;border-bottom:2px solid #0d5c52}}
        .agent-title{{font-size:1.2rem;font-weight:700;display:flex;align-items:center;gap:0.5rem}}
        .critic-badge{{background:rgba(255,255,255,0.3);padding:0.2rem 0.6rem;border-radius:4px;font-size:0.75rem;font-weight:700}}
        .agent-body{{padding:1.2rem}}
        .agent-field{{margin-bottom:1rem}}
        .agent-field strong{{color:#127a6b;display:block;margin-bottom:0.4rem;font-size:0.9rem;text-transform:uppercase;letter-spacing:0.5px}}
        .agent-field p{{color:#555;font-size:0.95rem;line-height:1.5}}
        .thesis-box{{background:#fff;border:2px solid #e8f8f5;border-radius:6px;padding:1rem;margin-top:1.2rem}}
        .thesis-label{{font-weight:700;color:#127a6b;font-size:0.85rem;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:0.6rem}}
        .thesis-text{{font-style:italic;color:#2c3e50;line-height:1.6;font-size:0.95rem}}
        .confirmation{{background:linear-gradient(135deg,#e8f8f5 0%,#c8e6c9 100%);border-left:4px solid #2e7d32;padding:1.5rem;border-radius:8px;margin:2rem 0;font-size:0.95rem}}
        .confirmation strong{{color:#0d5c52}}
        .action-section{{margin-top:2.5rem;padding-top:2rem;border-top:1px solid #ecf0f1;text-align:center}}
        .run-id{{background:#ecf0f1;border-radius:6px;padding:0.6rem 1rem;font-family:monospace;font-size:0.9rem;display:inline-block;margin:1rem 0;color:#127a6b;font-weight:600;cursor:pointer}}
        .run-id:hover{{background:#dce4e8}}
        button{{padding:0.9rem 2rem;border:none;border-radius:6px;background:linear-gradient(135deg,#127a6b,#0d5c52);color:#fff;font-weight:700;cursor:pointer;transition:transform 0.2s,box-shadow 0.2s;font-size:1rem}}
        button:hover{{transform:translateY(-2px);box-shadow:0 6px 16px rgba(18,122,107,0.3)}}
      </style>
    </head>
    <body>
    <div class="container">
      <div class="header">
        <h1>Agent Panel Ready</h1>
        <p class="subtitle">Your dialectic debate team has been assembled</p>
      </div>

      <div class="content">
        <div class="section">
          <div class="section-title">Research Question</div>
          <div class="topic-box">
            <p class="topic-text">{topic_text}</p>
            <div class="tags">{tags_html}</div>
          </div>
        </div>

        <div class="section">
          <div class="section-title">Your Debate Team ({len(agents)} Agents)</div>
          <div class="agents-grid">
            {agents_html}
          </div>
        </div>

        <div class="confirmation">
          <strong>Confirmation:</strong> Results and synthesis will be sent to <strong>{email_text}</strong>
        </div>

        <div class="action-section">
          <div>Run ID: <span class="run-id" title="Click to copy">{run_id}</span></div>
          <button onclick="window.location='/'">← New Topic</button>
        </div>
      </div>
    </div>

    <script>
      document.querySelector('.run-id').addEventListener('click', function(){{
        navigator.clipboard.writeText(this.textContent.split(': ')[1]);
        this.style.background = '#2e7d32';
        setTimeout(() => this.style.background = '#ecf0f1', 300);
      }});
    </script>
    </body>
    </html>
    """


@app.get("/runs/{run_id}", response_class=JSONResponse)
def get_run_json(run_id: str) -> JSONResponse:
    """JSON API endpoint for programmatic access."""
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    run["sub_agents"] = [a.model_dump() for a in store.get_sub_agents(run_id)]
    return JSONResponse(run)
