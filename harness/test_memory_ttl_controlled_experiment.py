from workflow.scripts.ecs_memory_ttl_experiment import current_fixtures, select_ttl


def test_select_ttl_uses_shortest_candidate_covering_boundary() -> None:
    decision = select_ttl(
        "EPISODIC", [30, 60, 90, 180], [1, 30, 60, 89], 0.02, 0.0)

    assert decision["selectedTtlDays"] == 90
    assert [row["coversHorizon"] for row in decision["candidates"]] == [
        False, False, True, True]


def test_current_fixtures_require_exact_producer_and_promote_strategy_shape() -> None:
    case = {
        "caseId": "java",
        "resume": "Java Spring Redis",
        "jd": "Java backend",
    }
    payload = {
        "entries": [
            {
                "memoryId": "old-semantic",
                "type": "SEMANTIC",
                "ownerScope": "CONVERSATION",
                "source": "candidate_fact",
                "producerVersion": "old",
                "content": "Java Spring Redis",
            },
            {
                "memoryId": "new-semantic",
                "type": "SEMANTIC",
                "ownerScope": "CONVERSATION",
                "source": "candidate_fact",
                "producerVersion": "build-1",
                "status": "ACTIVE",
                "content": "Java Spring Redis",
            },
            {
                "memoryId": "new-episode",
                "type": "EPISODIC",
                "ownerScope": "USER",
                "source": "cross_candidate_anchor",
                "producerVersion": "build-1",
                "status": "ACTIVE",
                "content": "Java backend comparison",
            },
            {
                "memoryId": "staged-strategy",
                "type": "WORKING",
                "ownerScope": "RUN",
                "source": "runtime_strategy",
                "producerVersion": "build-1",
                "status": "ARCHIVED",
                "content": "current execution strategy",
            },
        ]
    }

    fixtures = current_fixtures(payload, case, "build-1")

    assert fixtures["SEMANTIC"]["_sourceMemoryId"] == "new-semantic"
    assert fixtures["PROCEDURAL"]["type"] == "PROCEDURAL"
    assert fixtures["PROCEDURAL"]["ownerScope"] == "USER"
    assert fixtures["PROCEDURAL"]["_sourceMemoryId"] == "staged-strategy"
