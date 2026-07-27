# Release Operator Onboarding (Remotion)

Motion tutorial for new Release Operators. Content tracks
`docs/release-workflow.md`, ADR-0013, ADR-0017, and the README onboarding
section.

## Preview

```bash
cd release-operator-onboarding
npm i
npm run dev
```

Studio opens on composition `ReleaseOperatorOnboarding` (1920×1080, 30 fps,
~58 s with fade transitions).

## Render

```bash
npx remotion render ReleaseOperatorOnboarding out/release-operator-onboarding.mp4
```

## Scenes

1. Title — edge-deploy-core onboarding
2. Role — who publishes Autobench / Dispatch
3. Postures — five workstation postures (ADR-0013)
4. Both-vpns first — no firewall-off during onboard
5. Bootstrap — immutable engine tag + `.[dev]`
6. Private config — YAML outside Git, `BB_TOKEN` in env
7. Onboard stages — prerequisites → complete
8. Training isolation — practice ≠ production
9. GitHub write probe — red in both-vpns is expected
10. First guided release — separate boundary after onboard
11. Everyday commands — `status`, `preflight`, `transport-smoke`
12. Outro — docs pointers

Built with Remotion best practices (`TransitionSeries`, premounted sequences,
`interpolate` + Bézier easing, Google Fonts via `@remotion/google-fonts`).
