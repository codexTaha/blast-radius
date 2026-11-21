---
name: hackathon-research
description: >-
  Enforces rigorous, evidence-based research protocols for hackathons and engineering competitions.
  Use this skill whenever conducting research on competition rules, track requirements, evaluators,
  scoring formulas, and baseline repositories to ensure accuracy, traceability, and quality control.
---

# Hackathon Research & Quality Control Protocol

This skill governs all competition research to guarantee strict accuracy, source traceability, and evidence-backed decision-making.

## 1. Hierarchy of Authority

Always evaluate findings using this strict source hierarchy:
1. **Official Evaluator & Code:** The actual scoring script, test harness, Dockerfile, and baseline code (what is *actually enforced*).
2. **Official Competition Rules & Portal:** The official website rules, terms, and track specification documents.
3. **Official Organizer Announcements:** Direct maintainer/organizer announcements, official issue responses, and FAQs.
4. **Platform Documentation:** Base platform/OS/library specifications (Docker, Python, SQLGlot, etc.).
5. **Community / Secondary Sources:** Unofficial discussion forums, blogs, or search snippets (advisory only; never treated as binding rules).

> [!IMPORTANT]
> If written documentation contradicts the actual evaluator implementation, **do not silently reconcile them**. Flag the discrepancy explicitly, document both behaviors, and treat the evaluator code as authoritative for scoring mechanics.

---

## 2. Research Verification & Labeling Standard

Every finding and claim must be classified using explicit tags:

- `[CONFIRMED]`: Directly verified against an authoritative source (quote, file path, line number, or exact URL recorded).
- `[INTERPRETATION]`: A logical deduction or engineering implication derived from confirmed facts. Must explain the reasoning.
- `[ASSUMPTION]`: A working hypothesis where direct evidence is not yet available. Must be marked for empirical verification.
- `[WARNING]`: A critical rule, constraint, or boundary where violation leads to disqualification, build failure, or scoring penalties.
- `[UNKNOWN]`: Explicitly missing or inaccessible information. Never invent or hallucinate missing parameters.

---

## 3. Evidence & URL Tracking Protocol

For every technical claim, record:
- **Source Identifier:** Canonical URL or repository path.
- **Reference Anchor:** Section name, git commit hash, file name, or line range (`#L...-L...`).
- **Verbatim Text / Constant:** Exact numeric constants, environment limits, exit codes, and formulas.
- **Verification Timestamp / Version:** Pinned version of the code or date of the documentation.

---

## 4. Key Focus Areas for Investigation

When investigating the competition track:

### A. Evaluator Mechanics vs. Rule Descriptions
- Check how runtimes and timeouts are measured (wall clock vs. CPU time vs. process time).
- Verify penalty trigger conditions (e.g., exact definition of a "missed failing test", import failures, errors).
- Identify edge cases in worker scheduling (empty worker lists, test duplication, worker count limits).

### B. Environment & Runtime Boundaries
- Memory limits, process limits, CPU thread limits, writable disk space.
- Exact user ID (`nobody` / UID 65534) and filesystem permissions.
- Network access availability (build time vs. runtime).

### C. Compliance & Integrity Checkpoints
- Prohibited techniques (e.g., hardcoded patch-to-test lookup tables, team-controlled hosting).
- Repository size caps and dependency packaging rules.
- Generalization requirements (performance on unseen patches).
