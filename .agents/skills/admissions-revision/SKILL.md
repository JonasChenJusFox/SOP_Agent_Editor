---
name: admissions-revision
description: Prepare and carry out a full application-essay revision in admissions-editor-agent from reviewed findings and explicit author decisions, preserving evidence, voice, and version-bound final acceptance. Use for SOP, personal-statement, or short-answer revisions in this repository.
---

# Admissions revision

Read the [revision and skills guide](../../../docs/skills.md) and current CLI/API
commands in [README](../../../README.md). This skill guides Codex in this
repository. The application separately loads its packaged, versioned prompts;
it does not read personal Codex skills.

## Review and plan

Start from a completed reviewed run and the same frozen original. Preserve the
original and review artifacts. Prepare a hash-bound revision plan with an
explicit accept, reject, or defer decision and reason for every stable finding
ID. Record the applicant's actual confirmed answers, facts, evidence, preferences,
and optional voice sample. Do not invent answers, treat suggestions as evidence,
or silently accept every finding. Explain missing decisions and conflicting
requirements so the applicant can resolve them.

Repository-local knowledge is the default source. Inspect the current source
status and claim-specific retrieved passages in knowledge.json, including repo,
commit/snapshot, file and line range. Refresh stale indexes and prepare a new
plan when used source content changes. Keep source repositories read-only; code
existence alone does not prove the applicant authored it or achieved a metric.
Never replace missing evidence with unrelated code or copy an entire repository
into a model request. Local configuration and population instructions are in
[the repository knowledge guide](../../../docs/knowledge.md).

## Revise and inspect

Use the repository's staged revision workflow: substantive SOP revision,
restrained language polish, Humanizer, then read-only verification. The actual
application prompt, genre, and limit govern the draft. A writing sample supplies
style only; preserve factual boundaries, individual versus team attribution,
rejected material, protected spans, and unanswered questions. Before writing, inspect the extracted technical terms, methods, models, metrics,
data sources, libraries, system components, and contribution claims with their
source passages. Use the optional applicant glossary for terminology only. Preserve
retrieval versus ranking, embeddings versus fine-tuning, interface versus model
development, metric/data scopes, and applicant/team ownership. Specialized terms
are not evidence of AI writing. Keep original wording and flag uncertain edits.
The Humanizer is the last writing pass. The final read-only verification includes
technical accuracy; inspect technical-context.json and technical-accuracy.json.
Any later manual edit needs verification again.

Inspect the entire candidate, original-to-candidate diff, finding trace,
verification report, and unresolved questions. Missing information, stale hashes,
conflicting decisions, unsupported claims, and failed required checks keep the
result a candidate. Do not bypass a blocked finalize operation or treat narrow
patch approval as acceptance of a separately rewritten essay.

## Finalize

Request the applicant's final acceptance of the exact inspected candidate through
the repository's revision acceptance command or UI. Preserve the source and write
a separate final artifact with its receipt. A model's successful verification
does not constitute the applicant's acceptance.

Use synthetic materials for tests and existing ignored locations for private
artifacts. Consult the guide for source commits, license notices, prompt hashes,
and the intentional adaptation of upstream guidance. No detector scoring,
artificial errors, invented personality, or recursive upstream skill pipelines.
This skill supplies no additional remote-service or deployment permissions.
