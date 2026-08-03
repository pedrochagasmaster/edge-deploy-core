import React from "react";
import { AbsoluteFill, Easing, interpolate, useCurrentFrame } from "remotion";
import {
  CodeBlock,
  FadeSlide,
  Headline,
  PillRow,
  SceneShell,
  Subhead,
} from "../components/SceneChrome";
import { colors, fonts } from "../theme";

export const TitleScene: React.FC = () => {
  const frame = useCurrentFrame();
  const brand = interpolate(frame, [0, 28], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.bezier(0.16, 1, 0.3, 1),
  });

  return (
    <SceneShell>
      <div
        style={{
          opacity: brand,
          translate: `0px ${interpolate(brand, [0, 1], [24, 0])}px`,
          fontFamily: fonts.display,
          fontSize: 42,
          fontWeight: 700,
          letterSpacing: "0.08em",
          textTransform: "uppercase",
          color: colors.accent,
          marginBottom: 24,
        }}
      >
        edge-deploy-core
      </div>
      <FadeSlide delay={8}>
        <Headline>Release Operator onboarding</Headline>
      </FadeSlide>
      <FadeSlide delay={18}>
        <Subhead>
          A zero-state walkthrough: both-vpns, bootstrap the engine, private
          config, onboard stages, then your first guided release.
        </Subhead>
      </FadeSlide>
    </SceneShell>
  );
};

export const RoleScene: React.FC = () => (
  <SceneShell step="01 · Role">
    <FadeSlide>
      <Headline>Only Release Operators publish</Headline>
    </FadeSlide>
    <FadeSlide delay={10}>
      <Subhead>
        You publish reviewed Autobench and Dispatch commits to Bitbucket and
        deploy them to Edge nodes. Contributors stay on GitHub PRs — no Edge,
        SSH, or Kerberos required for normal development.
      </Subhead>
    </FadeSlide>
    <PillRow
      delay={22}
      items={[
        { label: "Autobench", tone: "neutral" },
        { label: "Dispatch / robocop", tone: "neutral" },
        { label: "Windows PS 5.1 controller", tone: "ok" },
      ]}
    />
  </SceneShell>
);

const POSTURES = [
  { name: "baseline", gh: "R", bb: "—", edge: "—" },
  { name: "edge-vpn", gh: "R", bb: "—", edge: "✓" },
  { name: "bitbucket-vpn", gh: "R", bb: "✓", edge: "—" },
  { name: "both-vpns", gh: "R", bb: "✓", edge: "✓" },
  { name: "firewall-off", gh: "W", bb: "—", edge: "—" },
] as const;

export const PosturesScene: React.FC = () => {
  const frame = useCurrentFrame();

  return (
    <SceneShell step="02 · Postures · ADR-0013">
      <FadeSlide>
        <Headline>Five workstation postures</Headline>
      </FadeSlide>
      <FadeSlide delay={8}>
        <Subhead>
          GitHub read works everywhere. GitHub write needs firewall-off — which
          drops both VPNs. Never hold write and Bitbucket/Edge at once.
        </Subhead>
      </FadeSlide>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "2fr 1fr 1fr 1fr",
          gap: 14,
          marginTop: 40,
          maxWidth: 1500,
        }}
      >
        {["Posture", "GitHub", "Bitbucket", "Edge"].map((h) => (
          <div
            key={h}
            style={{
              fontFamily: fonts.mono,
              fontSize: 24,
              letterSpacing: "0.08em",
              textTransform: "uppercase",
              color: colors.inkMuted,
              paddingBottom: 8,
            }}
          >
            {h}
          </div>
        ))}
        {POSTURES.map((row, index) => {
          const local = interpolate(
            frame,
            [18 + index * 8, 18 + index * 8 + 16],
            [0, 1],
            {
              extrapolateLeft: "clamp",
              extrapolateRight: "clamp",
              easing: Easing.bezier(0.16, 1, 0.3, 1),
            },
          );
          const highlight = row.name === "both-vpns";
          const cellStyle: React.CSSProperties = {
            opacity: local,
            translate: `0px ${interpolate(local, [0, 1], [12, 0])}px`,
            background: highlight ? colors.accentSoft : colors.panel,
            border: `1px solid ${colors.panelBorder}`,
            borderRadius: 12,
            padding: "18px 20px",
            fontSize: 30,
            fontWeight: highlight ? 700 : 500,
            fontFamily: fonts.body,
            color: highlight ? colors.accent : colors.ink,
          };
          return (
            <React.Fragment key={row.name}>
              <div style={cellStyle}>{row.name}</div>
              <div style={cellStyle}>{row.gh}</div>
              <div style={cellStyle}>{row.bb}</div>
              <div style={cellStyle}>{row.edge}</div>
            </React.Fragment>
          );
        })}
      </div>
    </SceneShell>
  );
};

export const BothVpnsScene: React.FC = () => (
  <SceneShell step="03 · First rule">
    <FadeSlide>
      <Headline>Connect both-vpns first</Headline>
    </FadeSlide>
    <FadeSlide delay={10}>
      <Subhead>
        Bitbucket + Edge VPNs stay on for the entire onboard flow. Do not switch
        to firewall-off during routine onboarding — red GitHub write in
        both-vpns is expected.
      </Subhead>
    </FadeSlide>
    <PillRow
      delay={20}
      items={[
        { label: "Bitbucket VPN", tone: "ok" },
        { label: "Edge VPN", tone: "ok" },
        { label: "No firewall-off yet", tone: "warn" },
      ]}
    />
  </SceneShell>
);

export const BootstrapScene: React.FC = () => (
  <SceneShell step="04 · Bootstrap">
    <FadeSlide>
      <Headline>Install the approved engine tag</Headline>
    </FadeSlide>
    <FadeSlide delay={8}>
      <Subhead>
        Clone core, check out the immutable tag matching the package version,
        then install editable with the dev extra only.
      </Subhead>
    </FadeSlide>
    <CodeBlock
      delay={16}
      lines={[
        "git clone …/edge-deploy-core.git && cd edge-deploy-core",
        "git checkout v1.6.0",
        'py -m pip install -e ".[dev]"',
      ]}
    />
  </SceneShell>
);

export const ConfigScene: React.FC = () => (
  <SceneShell step="05 · Private config">
    <FadeSlide>
      <Headline>Keep operator YAML outside Git</Headline>
    </FadeSlide>
    <FadeSlide delay={8}>
      <Subhead>
        Require operator_email, nodes, bitbucket_remotes.core, and one remote
        per tool (autobench / robocop). Put BB_TOKEN in the environment only —
        never commit secrets.
      </Subhead>
    </FadeSlide>
    <CodeBlock
      delay={18}
      lines={[
        "py -m edge_deploy onboard --config C:\\secure\\operator.yaml",
        "py -m edge_deploy onboard --config … --tool autobench --tool dispatch",
      ]}
    />
  </SceneShell>
);

const STAGES = [
  "prerequisites",
  "config",
  "repositories",
  "readiness",
  "practice",
  "complete",
] as const;

export const StagesScene: React.FC = () => {
  const frame = useCurrentFrame();

  return (
    <SceneShell step="06 · Onboard stages · ADR-0017">
      <FadeSlide>
        <Headline>Resumable stages, atomic state</Headline>
      </FadeSlide>
      <FadeSlide delay={8}>
        <Subhead>
          Progress lives under %APPDATA%\edge-deploy\ as fingerprints only.
          Use --check for diagnostics, --restart to drop evidence and keep
          checkouts.
        </Subhead>
      </FadeSlide>
      <div
        style={{
          display: "flex",
          gap: 14,
          marginTop: 44,
          flexWrap: "wrap",
        }}
      >
        {STAGES.map((stage, index) => {
          const local = interpolate(
            frame,
            [16 + index * 7, 16 + index * 7 + 16],
            [0, 1],
            {
              extrapolateLeft: "clamp",
              extrapolateRight: "clamp",
              easing: Easing.bezier(0.16, 1, 0.3, 1),
            },
          );
          return (
            <div
              key={stage}
              style={{
                opacity: local,
                scale: interpolate(local, [0, 1], [0.92, 1]),
                background: colors.codeBg,
                color: colors.codeFg,
                borderRadius: 14,
                padding: "22px 26px",
                fontFamily: fonts.mono,
                fontSize: 28,
                fontWeight: 500,
              }}
            >
              <span style={{ color: colors.codeAccent, marginRight: 10 }}>
                {String(index + 1).padStart(2, "0")}
              </span>
              {stage}
            </div>
          );
        })}
      </div>
    </SceneShell>
  );
};

export const TrainingScene: React.FC = () => (
  <SceneShell step="07 · Training isolation">
    <FadeSlide>
      <Headline>Practice is not a release</Headline>
    </FadeSlide>
    <FadeSlide delay={10}>
      <Subhead>
        Training ledgers live under{" "}
        <span style={{ fontFamily: fonts.mono, fontSize: 36 }}>
          {"%APPDATA%\\edge-deploy\\training\\<tool>\\"}
        </span>{" "}
        with kind=training and training=true. Production commands reject them.
        The console training rail is simulated — never treat it as a real
        posture switch.
      </Subhead>
    </FadeSlide>
    <PillRow
      delay={22}
      items={[
        { label: "Isolated practice ledgers", tone: "ok" },
        { label: "No production phase import", tone: "ok" },
        { label: "Console rail = simulated", tone: "warn" },
      ]}
    />
  </SceneShell>
);

export const WriteProbeScene: React.FC = () => (
  <SceneShell step="08 · GitHub write probe">
    <FadeSlide>
      <Headline>Red write in both-vpns is OK</Headline>
    </FadeSlide>
    <FadeSlide delay={10}>
      <Subhead>
        Aggregate green means every write-root git push --dry-run passed. Red
        or unavailable during onboarding does not fail the flow — you learn the
        both-vpns → firewall-off boundary in simulation only.
      </Subhead>
    </FadeSlide>
    <PillRow
      delay={22}
      items={[
        { label: "gh auth ≠ write probe", tone: "neutral" },
        { label: "push --dry-run", tone: "ok" },
        { label: "Red expected here", tone: "warn" },
      ]}
    />
  </SceneShell>
);

export const GuidedScene: React.FC = () => (
  <SceneShell step="09 · First real release">
    <FadeSlide>
      <Headline>Onboard complete ≠ release</Headline>
    </FadeSlide>
    <FadeSlide delay={8}>
      <Subhead>
        After onboarding, the first guided release is a separate boundary.
        Work from a clean tool main, install .[dev,release], then run guided
        release and switch posture only when prompted.
      </Subhead>
    </FadeSlide>
    <CodeBlock
      delay={18}
      lines={[
        'py -m pip install -e ".[dev,release]"',
        "py -m edge_deploy release --guided --tool autobench",
      ]}
    />
  </SceneShell>
);

export const CommandsScene: React.FC = () => (
  <SceneShell step="10 · Everyday commands">
    <FadeSlide>
      <Headline>Status tells you the next step</Headline>
    </FadeSlide>
    <FadeSlide delay={8}>
      <Subhead>
        Phases are short and idempotent. status prints per-phase state and the
        exact next command with the required posture.
      </Subhead>
    </FadeSlide>
    <CodeBlock
      delay={16}
      lines={[
        "py -m edge_deploy status",
        "py -m edge_deploy preflight --node node03",
        "py -m edge_deploy transport-smoke --node node03",
      ]}
    />
  </SceneShell>
);

export const OutroScene: React.FC = () => (
  <AbsoluteFill>
    <SceneShell>
      <FadeSlide>
        <div
          style={{
            fontFamily: fonts.display,
            fontSize: 42,
            fontWeight: 700,
            letterSpacing: "0.08em",
            textTransform: "uppercase",
            color: colors.accent,
            marginBottom: 24,
          }}
        >
          Next reading
        </div>
      </FadeSlide>
      <FadeSlide delay={8}>
        <Headline>You are ready to practice</Headline>
      </FadeSlide>
      <FadeSlide delay={16}>
        <Subhead>
          docs/release-workflow.md · ADR-0013 · ADR-0017 · README onboarding
          section. Never commit reports, tokens, or operator config to GitHub.
        </Subhead>
      </FadeSlide>
    </SceneShell>
  </AbsoluteFill>
);
