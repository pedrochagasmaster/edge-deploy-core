import React from "react";
import {
  AbsoluteFill,
  Easing,
  interpolate,
  useCurrentFrame,
} from "remotion";
import { colors, fonts } from "../theme";

export const SceneBackground: React.FC = () => {
  const frame = useCurrentFrame();
  const drift = interpolate(frame, [0, 180], [0, 40], {
    extrapolateRight: "extend",
  });

  return (
    <AbsoluteFill
      style={{
        background: `linear-gradient(160deg, ${colors.bgTop} 0%, ${colors.bgBottom} 100%)`,
      }}
    >
      <AbsoluteFill
        style={{
          opacity: 0.18,
          backgroundImage: `
            linear-gradient(${colors.ink} 1px, transparent 1px),
            linear-gradient(90deg, ${colors.ink} 1px, transparent 1px)
          `,
          backgroundSize: "72px 72px",
          translate: `${drift * 0.15}px ${drift * 0.08}px`,
        }}
      />
      <AbsoluteFill
        style={{
          background: `radial-gradient(ellipse at 18% 12%, ${colors.accentSoft} 0%, transparent 42%),
            radial-gradient(ellipse at 88% 78%, rgba(255,255,255,0.35) 0%, transparent 40%)`,
        }}
      />
    </AbsoluteFill>
  );
};

type SceneShellProps = {
  readonly children: React.ReactNode;
  readonly step?: string;
};

export const SceneShell: React.FC<SceneShellProps> = ({
  children,
  step,
}) => {
  return (
    <AbsoluteFill>
      <SceneBackground />
      <AbsoluteFill
        style={{
          padding: "100px 120px",
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          fontFamily: fonts.body,
          color: colors.ink,
        }}
      >
        {step ? (
          <div
            style={{
              fontFamily: fonts.mono,
              fontSize: 28,
              letterSpacing: "0.14em",
              textTransform: "uppercase",
              color: colors.accent,
              marginBottom: 28,
              fontWeight: 500,
            }}
          >
            {step}
          </div>
        ) : null}
        {children}
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

type FadeSlideProps = {
  readonly children: React.ReactNode;
  readonly delay?: number;
  readonly fromY?: number;
};

export const FadeSlide: React.FC<FadeSlideProps> = ({
  children,
  delay = 0,
  fromY = 28,
}) => {
  const frame = useCurrentFrame();
  const progress = interpolate(frame, [delay, delay + 22], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.bezier(0.16, 1, 0.3, 1),
  });

  return (
    <div
      style={{
        opacity: progress,
        translate: `0px ${interpolate(progress, [0, 1], [fromY, 0])}px`,
      }}
    >
      {children}
    </div>
  );
};

export const Headline: React.FC<{ readonly children: React.ReactNode }> = ({
  children,
}) => (
  <h1
    style={{
      fontFamily: fonts.display,
      fontSize: 92,
      lineHeight: 1.05,
      fontWeight: 800,
      margin: 0,
      letterSpacing: "-0.03em",
      maxWidth: 1500,
    }}
  >
    {children}
  </h1>
);

export const Subhead: React.FC<{ readonly children: React.ReactNode }> = ({
  children,
}) => (
  <p
    style={{
      fontFamily: fonts.body,
      fontSize: 40,
      lineHeight: 1.35,
      color: colors.inkMuted,
      margin: "28px 0 0",
      maxWidth: 1400,
      fontWeight: 400,
    }}
  >
    {children}
  </p>
);

export const CodeBlock: React.FC<{
  readonly lines: readonly string[];
  readonly delay?: number;
}> = ({ lines, delay = 8 }) => {
  const frame = useCurrentFrame();
  const progress = interpolate(frame, [delay, delay + 24], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.bezier(0.16, 1, 0.3, 1),
  });

  return (
    <div
      style={{
        marginTop: 40,
        background: colors.codeBg,
        color: colors.codeFg,
        borderRadius: 18,
        padding: "36px 44px",
        fontFamily: fonts.mono,
        fontSize: 34,
        lineHeight: 1.55,
        opacity: progress,
        scale: interpolate(progress, [0, 1], [0.97, 1]),
        boxShadow: "0 24px 60px rgba(11, 31, 42, 0.28)",
        maxWidth: 1600,
      }}
    >
      {lines.map((line) => (
        <div key={line}>
          <span style={{ color: colors.codeAccent, marginRight: 16 }}>$</span>
          {line}
        </div>
      ))}
    </div>
  );
};

export const PillRow: React.FC<{
  readonly items: readonly { label: string; tone?: "neutral" | "ok" | "warn" }[];
  readonly delay?: number;
}> = ({ items, delay = 12 }) => {
  const frame = useCurrentFrame();

  return (
    <div
      style={{
        display: "flex",
        flexWrap: "wrap",
        gap: 16,
        marginTop: 36,
      }}
    >
      {items.map((item, index) => {
        const local = interpolate(
          frame,
          [delay + index * 6, delay + index * 6 + 18],
          [0, 1],
          {
            extrapolateLeft: "clamp",
            extrapolateRight: "clamp",
            easing: Easing.bezier(0.16, 1, 0.3, 1),
          },
        );
        const bg =
          item.tone === "ok"
            ? colors.okSoft
            : item.tone === "warn"
              ? colors.warnSoft
              : colors.panel;
        const fg =
          item.tone === "ok"
            ? colors.ok
            : item.tone === "warn"
              ? colors.warn
              : colors.ink;
        return (
          <div
            key={item.label}
            style={{
              opacity: local,
              translate: `0px ${interpolate(local, [0, 1], [16, 0])}px`,
              background: bg,
              color: fg,
              border: `1px solid ${colors.panelBorder}`,
              borderRadius: 14,
              padding: "16px 22px",
              fontSize: 30,
              fontWeight: 600,
              fontFamily: fonts.body,
            }}
          >
            {item.label}
          </div>
        );
      })}
    </div>
  );
};
