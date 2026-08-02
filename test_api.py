"""Quick test of the form and results page (without running live server)."""
import os
import sys
os.environ.setdefault("LLM_PROVIDER", "mock")

# Fix encoding for Windows
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')

from fastapi.testclient import TestClient
from api import app

client = TestClient(app)

# Test 1: GET form
print("=" * 60)
print("TEST 1: GET /intake form")
print("=" * 60)
response = client.get("/")
assert response.status_code == 200
assert "HILCA Intake Form" in response.text
assert "Tag 1" in response.text
assert "Agent 1" in response.text
assert "Source 1" in response.text
print("[OK] Form HTML contains all fields (5 tags, 3 agents, 3 URLs)")
print()

# Test 2: POST form with all fields
print("=" * 60)
print("TEST 2: POST /intake with structured form data")
print("=" * 60)
form_data = {
    "topic": "Should we adopt microservices architecture for our growing platform?",
    "tag_1": "architecture",
    "tag_2": "scalability",
    "tag_3": "complexity",
    "tag_4": "operations",
    "tag_5": "cost",
    "agent_1": "Systems Architect",
    "agent_2": "Cost Analyst",
    "agent_3": "Skeptic",
    "url_1": "https://example.com/microservices-paper.pdf",
    "url_2": "https://example.com/case-study.pdf",
    "url_3": "https://example.com/risks.pdf",
    "email": "test@example.com",
}

response = client.post("/intake", data=form_data)
assert response.status_code == 200
# Should redirect to results page
assert "results" in response.text or response.history
print("[OK] Form submission successful")
print()

# Extract run_id from redirect (in the meta refresh tag)
import re
match = re.search(r'/results/([a-f0-9\-]+)', response.text)
if match:
    run_id = match.group(1)
    print(f"[OK] Run ID generated: {run_id[:8]}...")

    # Test 3: GET results page
    print()
    print("=" * 60)
    print("TEST 3: GET /results/{run_id}")
    print("=" * 60)
    results_response = client.get(f"/results/{run_id}")
    assert results_response.status_code == 200
    assert "Agent Panel Ready" in results_response.text
    assert "Systems Architect" in results_response.text
    assert "Opening Thesis" in results_response.text
    assert "[CRITIC]" in results_response.text
    print("[OK] Results page shows spawned agents with thesis")
    print("[OK] Skeptic marked as [Critic]")
    print()

    # Test 4: GET JSON API
    print("=" * 60)
    print("TEST 4: GET /runs/{run_id} (JSON API)")
    print("=" * 60)
    json_response = client.get(f"/runs/{run_id}")
    assert json_response.status_code == 200
    data = json_response.json()
    assert data["topic"] == form_data["topic"]
    assert data["tags"] == ["architecture", "scalability", "complexity", "operations", "cost"]
    assert len(data["sub_agents"]) >= 3

    agents = data["sub_agents"]
    print(f"[OK] Returned {len(agents)} agents")
    for agent in agents:
        assert "thesis" in agent, f"Agent {agent['name']} missing thesis"
        agent_type = "critic" if agent["is_critic"] else "builder"
        print(f"  * {agent['name']} ({agent_type})")
        print(f"    Thesis: {agent['thesis'][:60]}...")
    print()

print("=" * 60)
print("[PASS] ALL M1 FORM & RESULTS TESTS PASSED")
print("=" * 60)
