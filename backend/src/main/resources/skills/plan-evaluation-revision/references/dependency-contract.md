# Dependency contract

Use these artifact names in `changedArtifacts`:

| Artifact | Earliest invalidated node | Reason |
| --- | --- | --- |
| `resume` | `intent` | Every evaluation consumes resume facts directly or indirectly. |
| `jd` | `jd_match` | Parsing the resume remains reusable; job-conditioned work does not. |
| `target_role` | `jd_match` | Target selection changes matching and downstream scoring. |
| `preferences` | `intent` | Preferences can alter routing, comparison, and reporting priorities. |
| `evaluation_focus` | `intent` | Evaluation strategy and all downstream rubrics may change. |
| `external_evidence` | `tech_eval` | Parsing and JD matching do not consume external profile evidence. |
| `rubric` | `tech_eval` | Evidence extraction remains reusable, but evaluation and reporting change. |
| `conversation_only` | none | A side question that changes no artifact reuses the checkpoint unchanged. |

The runtime dependency graph is authoritative when it differs from this default. Pass only changes confirmed by hashes or an explicit user correction. Do not classify a wording-only conversation turn as an artifact change.
