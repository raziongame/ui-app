# Test fixtures

| File | What it is |
|---|---|
| `note_3778530_excerpt.txt` | Golden Q10: excerpt of SAP Security Note 3778530 (CVE-2026-76964), used exactly as the operator pasted it. |
| `SYNTHETIC_note_9900001_java_only.txt` | **SYNTHETIC, not a real SAP Note** (operator 2026-11-18). An AS Java-only note: it must not apply to this ABAP system. |
| `SYNTHETIC_note_9900002_needs_profile_parameter.txt` | **SYNTHETIC, not a real SAP Note** (operator 2026-11-18). A kernel note that applies only if an ICM profile parameter is set. Profile parameters aren't available for this system, so the answer can't confirm it and must say what to check. |

Files named `SYNTHETIC_…` are made up for tests: their note numbers (99xxxxx), components and patch
levels don't come from SAP. Never treat them as real notes or quote them outside the tests.
