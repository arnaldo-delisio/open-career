# Corpus case: wrong-work-authorization

- **Class**: wrong-work-authorization
- **Broken element**: summary: appended sentence "Requires visa sponsorship to work in the Netherlands." The profile in the snapshot (and the policy snapshot projection built from it) carries authorized_in_country: "yes" and needs_sponsorship: "no", so this authorization-class assertion matches no allowed form
- **Counterfactual pair**: derives from `clean-base`; byte-identical except the broken element.
- **Expected catching layer**: stage zero, rule `work-authorization` (deterministic; no judge runs, disposition `fail`)

Every word grounds lexically via the profile's work_authorization_note; the contradiction is against the closed yes/no authorization fields, which is exactly the deterministic projection's job.
