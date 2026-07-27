import { loadFont as loadSyne } from "@remotion/google-fonts/Syne";
import { loadFont as loadIBMPlexSans } from "@remotion/google-fonts/IBMPlexSans";
import { loadFont as loadIBMPlexMono } from "@remotion/google-fonts/IBMPlexMono";

const syne = loadSyne("normal", {
  weights: ["600", "700", "800"],
  subsets: ["latin"],
});

const plexSans = loadIBMPlexSans("normal", {
  weights: ["400", "500", "600"],
  subsets: ["latin"],
});

const plexMono = loadIBMPlexMono("normal", {
  weights: ["400", "500"],
  subsets: ["latin"],
});

export const fonts = {
  display: syne.fontFamily,
  body: plexSans.fontFamily,
  mono: plexMono.fontFamily,
};

export const colors = {
  bgTop: "#C9D6E2",
  bgBottom: "#8FA8BC",
  ink: "#0B1F2A",
  inkMuted: "#3A5260",
  accent: "#0B6E63",
  accentSoft: "#D4F0EC",
  warn: "#A15C00",
  warnSoft: "#F5E6C8",
  ok: "#1B6B45",
  okSoft: "#D4EDDF",
  codeBg: "#0B1F2A",
  codeFg: "#E7F3F1",
  codeAccent: "#5EEAD4",
  panel: "rgba(255, 255, 255, 0.55)",
  panelBorder: "rgba(11, 31, 42, 0.12)",
  white: "#F7FAFC",
};

export const FPS = 30;
export const WIDTH = 1920;
export const HEIGHT = 1080;

export const TRANSITION_FRAMES = 15;

export const sceneDurations = {
  title: 100,
  role: 140,
  postures: 210,
  bothVpns: 150,
  bootstrap: 180,
  config: 180,
  stages: 210,
  training: 150,
  writeProbe: 150,
  guided: 180,
  commands: 180,
  outro: 100,
} as const;

export const totalDurationInFrames =
  Object.values(sceneDurations).reduce((a, b) => a + b, 0) -
  TRANSITION_FRAMES * (Object.keys(sceneDurations).length - 1);
