# ATS form fixtures

Captured real application-form DOMs (OC-Q3 corpus): per posting, `dom.html`, `fields.json`,
`page.png`, `meta.json`. Read-only captures; nothing was typed or submitted.

Sanitization rule: captured DOMs must carry no key-shaped strings, even third-party
client-side ones (Greenhouse embeds its own Google Maps browser key in every page); redact
on capture with the pattern set in this rule, currently `AIza[0-9A-Za-z_-]{35}` plus common
token prefixes (`sk-`, `ghp_`, `AKIA`, `xox`). Personal data is prohibited outright.
