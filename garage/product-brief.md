# DRAFT proposal — drafted only from README.md; no new product decisions.

Ambient-home is a local voice-assistant hub. It connects device plugins through
`ambient-device-sdk`, routes audio through a wake-gated conversation, and
provides a local status UI, job board, and usage estimates. Device connectors
are maintained separately in ambient-devices.

Structured events are stored locally. Transcripts, audio, Live payloads, and
request text are not logged. An optional local Garage service can receive jobs
for configured repositories. Devin receives a job only when one is dispatched.

Engineering jobs are PR-only: they must not merge, push directly to
`main`/`master`, deploy, or change branch protection or CI security settings.
The dashboard estimates Live audio and worker ACU costs, and configured Live
session limits and a daily audio budget constrain use.
