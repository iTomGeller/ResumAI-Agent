# Evidence record

Represent each item with these fields:

| Field | Required | Meaning |
| --- | --- | --- |
| `claimId` | yes | Stable claim identifier within a revision. |
| `claimText` | yes | Atomic statement being checked. |
| `sourceType` | yes | `resume_text`, `user_statement`, `rag_chunk`, `jd_text`, or `external_tool`. |
| `sourceRef` | yes for usable evidence | Page/line, chunk provenance, or public URL. |
| `quote` | yes for textual evidence | Short passage that directly supports or conflicts with the claim. |
| `retrievedAt` | external only | Retrieval timestamp for changeable public data. |
| `toolStatus` | external only | `success`, `unavailable`, `failed`, or `not_called`. |
| `identityLinkage` | profile evidence | `explicit_resume_link`, `user_confirmed`, or `unknown`. |

Treat a RAG chunk and its source resume passage as one source. Keep claim truth, account identity, tool health, and job relevance as separate decisions.
