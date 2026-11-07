from conftest import make


def test_fresh_install_can_start_a_thread_without_creating_a_project(tmp_path, agents):
    _, c, tok = make(tmp_path, agents)
    h = {"Authorization": f"Bearer {tok['owner']}"}
    projects = c.get("/api/home", headers=h).json()["projects"]
    assert [p["id"] for p in projects] == ["general"]
    r = c.post("/api/threads", headers=h, json={"title": "First question", "project_id": "general"})
    assert r.status_code == 200
