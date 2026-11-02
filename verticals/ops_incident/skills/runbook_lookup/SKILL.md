---
id: runbook_lookup
name: Runbook Lookup
description: Find the on-call runbook passage that matches a page and quote it
---

# Runbook Lookup

Search the tenant knowledge base before you answer. Return the best matching runbook title, a short quote, and the source file. If nothing matches, say the knowledge base does not cover it. Do not paraphrase a step that is not in the retrieved passage.

`run.py` ranks bundled fixture passages, or passages you pass in, for the offline demo. A live agent should still call `kb_search`.
